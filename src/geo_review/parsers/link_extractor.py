"""总文档链接提取器 — 从飞书表格/文档或 Excel 文件中提取子文档超链接.

使用场景：
    用户将待审核文章的链接汇总在一个"总文档"中（飞书表格/飞书文档/Excel），
    系统先解析总文档，提取其中的子文档链接，再逐个抓取正文进行批量审核。

支持来源：
    1. Excel 文件（.xlsx/.xls）— 提取单元格超链接 + 单元格文本中的 URL
    2. 飞书/网页总文档链接 — 抓取 HTML，提取 <a href> 超链接 + 文本中的 URL

安全：
    - 所有提取的 URL 均做格式校验（仅 http/https）
    - 去重 + 上限截断（默认 30 条）
"""

import io
import logging
import re
from typing import List, Optional, Tuple

from geo_review.parsers.url_fetcher import URLDocumentFetcher

logger = logging.getLogger(__name__)

# 匹配文本中的 http(s) 链接（排除常见结尾标点）
_URL_RE = re.compile(r'https?://[^\s<>"\'）】」，。；、]+')

# 提取链接时需要过滤掉的非文档类 URL（导航、静态资源、登录页等）
_EXCLUDE_PATTERNS = re.compile(
    r'('
    r'\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|map)(?:\?|$)'  # 静态资源
    r'|/login|/logout|/signin|/register'                            # 认证页面
    r'|javascript:|mailto:|tel:|#'                                  # 伪协议/锚点
    r'| Passport|/accounts?'                                        # 账号体系
    r')',
    re.IGNORECASE,
)

# 文档类链接特征（优先保留）：飞书文档/表格/Wiki/通用文档路径
_DOC_HINT_RE = re.compile(
    r'('
    r'feishu\.cn/(?:docx|docs|doc|wiki|sheets|sheet|base|file|minutes)'
    r'|larksuite\.com/(?:docx|docs|doc|wiki|sheets|sheet|base|file)'
    r'|/docx/|/docs/|/wiki/|/sheets/|/base/'
    r'|\.pdf|\.docx?|\.xlsx?|\.pptx?'
    r')',
    re.IGNORECASE,
)


def _normalize_and_filter(urls: List[str], max_links: int) -> List[str]:
    """清洗链接列表：去重、过滤无效/非文档链接、截断上限."""
    seen = set()
    result: List[str] = []
    for url in urls:
        url = url.strip().rstrip(".,;，。；、)）】」'\"")
        if not url or not url.startswith(("http://", "https://")):
            continue
        if _EXCLUDE_PATTERNS.search(url):
            continue
        # 去掉追踪参数之外的碎片，避免同一文档因 anchor 不同重复
        url = url.split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= max_links:
            break
    return result


def extract_links_from_text(text: str, max_links: int = 30) -> List[str]:
    """从纯文本中提取所有 http(s) 链接."""
    if not text:
        return []
    return _normalize_and_filter(_URL_RE.findall(text), max_links)


def extract_links_from_excel(
    content: bytes,
    filename: str = "",
    max_links: int = 30,
) -> List[str]:
    """从 Excel 文件中提取子文档链接.

    提取两个来源：
        1. 单元格超链接（cell.hyperlink.target）— 显示文本为标题、链接隐藏的情况
        2. 单元格文本中直接粘贴的 URL

    Args:
        content: Excel 文件二进制内容
        filename: 文件名（用于日志）
        max_links: 最多提取链接数

    Returns:
        去重后的链接列表
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ValueError("缺少 openpyxl 依赖，无法解析 Excel 文件")

    try:
        wb = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
    except Exception as e:
        raise ValueError(f"Excel 文件解析失败: {e}")

    hyperlinks: List[str] = []   # 超链接（高优先级，排前面）
    text_urls: List[str] = []    # 文本中的 URL

    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                # 1. 单元格超链接
                if cell.hyperlink and cell.hyperlink.target:
                    target = str(cell.hyperlink.target).strip()
                    if target.startswith(("http://", "https://")):
                        hyperlinks.append(target)
                # 2. 单元格文本中的 URL
                value = cell.value
                if isinstance(value, str) and "http" in value:
                    text_urls.extend(_URL_RE.findall(value))

    wb.close()

    # 超链接优先，其次文本 URL；文档类特征链接优先保留
    all_urls = hyperlinks + text_urls
    doc_urls = [u for u in all_urls if _DOC_HINT_RE.search(u)]
    other_urls = [u for u in all_urls if not _DOC_HINT_RE.search(u)]

    result = _normalize_and_filter(doc_urls + other_urls, max_links)
    logger.info(f"链接提取[Excel]: {filename} → {len(result)} 条链接")
    return result


def extract_links_from_master_url(
    url: str,
    max_links: int = 30,
    timeout: int = 30,
) -> Tuple[List[str], Optional[str]]:
    """从总文档链接（飞书表格/文档、通用网页）中提取子文档链接.

    流程：抓取总文档 HTML → 提取 <a href> 超链接 + 页面文本中的 URL →
          过滤非文档链接（导航/静态资源）→ 去重 → 截断

    Args:
        url: 总文档链接
        max_links: 最多提取链接数
        timeout: 抓取超时秒数

    Returns:
        (链接列表, 总文档标题)

    Raises:
        ValueError: 抓取失败或未提取到链接
    """
    from bs4 import BeautifulSoup

    html, title = URLDocumentFetcher.fetch_html(url, timeout=timeout)
    if not html:
        raise ValueError("总文档内容为空")

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    href_links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.startswith(("http://", "https://")):
            href_links.append(href)

    # 页面纯文本中直接粘贴的 URL（飞书表格单元格常为纯文本 URL）
    text = soup.get_text(separator=" ", strip=True)
    text_links = _URL_RE.findall(text)

    # 文档类链接优先
    all_urls = href_links + text_links
    doc_urls = [u for u in all_urls if _DOC_HINT_RE.search(u)]
    other_urls = [u for u in all_urls if not _DOC_HINT_RE.search(u)]

    result = _normalize_and_filter(doc_urls + other_urls, max_links)

    # 排除总文档自身链接（自引用）
    self_key = url.split("#")[0].rstrip("/")
    result = [u for u in result if u.rstrip("/") != self_key]

    logger.info(f"链接提取[总文档]: {url} → {len(result)} 条链接")
    return result, title
