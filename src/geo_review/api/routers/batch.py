"""Batch review routes — submit, progress, result, cancel, WS, upload, URLs."""

import asyncio as _asyncio
import base64
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from geo_review.agent.models import BatchProgress
from geo_review.agent import BatchReviewRequest
from geo_review.parsers.url_fetcher import URLDocumentFetcher
from geo_review.middleware.rate_limit import limiter, LIMIT_BATCH
from geo_review.auth.schemas import UserResponse

from .deps import get_current_user
from .helpers import get_file_extension
from .schemas import APIBatchReviewRequest

logger = logging.getLogger(__name__)

router = APIRouter()


# ================================================================
# 批量审核 — JSON
# ================================================================

@router.post("/api/v1/review/batch", tags=["批量审核"])
@limiter.limit(LIMIT_BATCH)
async def batch_review(body: APIBatchReviewRequest, request: Request, current_user: UserResponse = Depends(get_current_user)):
    """提交批量审核请求（JSON 方式）.

    支持：
    - 共享提报表（所有项共用）
    - 共享审核规则
    - 自定义审核选项
    - 最多 30 个项/批
    """
    batch_service = request.app.state._batch_service
    try:
        request_data = body.model_dump(exclude_none=False)
        batch_request = BatchReviewRequest(**request_data)
        progress = await batch_service.submit_batch(batch_request)
        return JSONResponse(content=progress.model_dump())
    except ValueError as e:
        # 模型校验失败（含批量上限 30 篇）→ 422
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量审核提交失败: {str(e)}")


@router.get("/api/v1/review/batch/{batch_id}/progress", tags=["批量审核"])
async def get_batch_progress(batch_id: str, request: Request, current_user: UserResponse = Depends(get_current_user)):
    """查询批量审核进度."""
    batch_service = request.app.state._batch_service
    progress = await batch_service.get_progress(batch_id)
    if not progress:
        raise HTTPException(status_code=404, detail=f"批量任务 '{batch_id}' 不存在")
    return JSONResponse(content=progress.model_dump())


@router.get("/api/v1/review/batch/{batch_id}/result", tags=["批量审核"])
async def get_batch_result(batch_id: str, request: Request, current_user: UserResponse = Depends(get_current_user)):
    """获取批量审核完整结果."""
    batch_service = request.app.state._batch_service
    result = await batch_service.get_result(batch_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"批量任务 '{batch_id}' 不存在")
    return JSONResponse(content=result.model_dump())


@router.post("/api/v1/review/batch/{batch_id}/cancel", tags=["批量审核"])
async def cancel_batch(batch_id: str, request: Request, current_user: UserResponse = Depends(get_current_user)):
    """取消批量审核任务."""
    batch_service = request.app.state._batch_service
    success = await batch_service.cancel_batch(batch_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"批量任务 '{batch_id}' 不存在或已完成，无法取消",
        )
    return {"status": "cancelled", "batch_id": batch_id}


@router.websocket("/api/v1/review/batch/{batch_id}/ws")
async def batch_progress_websocket(websocket: WebSocket, batch_id: str):
    """WebSocket 实时推送批量审核进度.

    连接后立即推送当前进度快照，之后每次进度变化时自动推送。
    当任务完成/取消/失败时推送最终状态并关闭连接。
    """
    # NOTE: WebSocket endpoints cannot use Depends() or Request for DI,
    # but the router is included in the app, so websocket.app gives us the app.
    batch_service = websocket.app.state._batch_service

    await websocket.accept()

    progress = await batch_service.get_progress(batch_id)
    if not progress:
        await websocket.send_json({"error": f"批量任务 '{batch_id}' 不存在"})
        await websocket.close()
        return

    await websocket.send_json(progress.model_dump())

    if progress.status in ("completed", "cancelled", "failed"):
        await websocket.close()
        return

    update_event = _asyncio.Event()

    async def on_progress(_batch_id: str, _progress: BatchProgress):
        if _batch_id == batch_id:
            update_event.set()

    batch_service.register_progress_callback(on_progress)
    consecutive_errors = 0
    max_errors = 5

    try:
        while True:
            try:
                await _asyncio.wait_for(update_event.wait(), timeout=30.0)
                update_event.clear()

                latest = await batch_service.get_progress(batch_id)
                if not latest:
                    await websocket.send_json({"error": "任务已不存在"})
                    break

                await websocket.send_json(latest.model_dump())
                consecutive_errors = 0

                if latest.status in ("completed", "cancelled", "failed"):
                    break

            except _asyncio.TimeoutError:
                latest = await batch_service.get_progress(batch_id)
                if not latest:
                    await websocket.send_json({"error": "任务已不存在"})
                    break
                await websocket.send_json(latest.model_dump())

                if latest.status in ("completed", "cancelled", "failed"):
                    break

            except WebSocketDisconnect:
                break

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    logger.error(f"WebSocket 推送连续失败 {max_errors} 次，断开连接: {e}")
                    break
                await _asyncio.sleep(1)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}", exc_info=True)
    finally:
        batch_service.unregister_progress_callback(on_progress)
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/api/v1/review/batch/upload", tags=["批量审核"])
@limiter.limit(LIMIT_BATCH)
async def batch_review_upload(
    request: Request,
    content_files: List[UploadFile] = File(..., description="待审正文文件（可多选，pdf/docx/doc/txt）"),
    submission_file: UploadFile = File(..., description="共享提报表文件（xlsx/json/txt），必填"),
    task_name: Optional[str] = Form(None, description="批量任务名称"),
    rule_template: Optional[str] = Form(None, description="规则模板名称"),
    official_urls: Optional[str] = Form(None, description="官网URL，逗号分隔"),
    output_format: str = Form("json", description="输出格式"),
    crawl_official_urls: str = Form("false", description="是否爬取官网"),
    use_llm: str = Form("false", description="是否启用LLM语义审核"),
    current_user: UserResponse = Depends(get_current_user),
):
    """批量文件上传方式提交审核.

    上传多个待审正文文件，必须上传提报表文件，进行批量审核。
    支持选择需要审核的文件（前端勾选后提交）。
    """
    crawl_official_urls = crawl_official_urls.lower() in ("true", "1", "yes")
    use_llm = use_llm.lower() in ("true", "1", "yes")
    config = request.app.state._config
    batch_service = request.app.state._batch_service

    try:
        if not content_files or len(content_files) == 0:
            raise HTTPException(status_code=400, detail="请至少上传一个待审文件")
        if len(content_files) > 100:
            raise HTTPException(status_code=400, detail="单次最多上传 100 个文件")

        # 构建共享提报表（必填）
        sub_bytes = await submission_file.read()
        sub_b64 = base64.b64encode(sub_bytes).decode("utf-8")
        sub_ext = get_file_extension(submission_file.filename)
        shared_submission = {
            "input_type": "file",
            "file": {
                "content_base64": sub_b64,
                "filename": submission_file.filename,
                "format": sub_ext,
            },
        }

        # 构建每个文件的审核项
        items = []
        for idx, cf in enumerate(content_files):
            content_bytes = await cf.read()
            content_b64 = base64.b64encode(content_bytes).decode("utf-8")
            content_ext = get_file_extension(cf.filename)
            items.append({
                "item_id": f"item-{idx + 1}",
                "item_name": cf.filename or f"文件{idx + 1}",
                "content": {
                    "input_type": "file",
                    "file": {
                        "content_base64": content_b64,
                        "filename": cf.filename,
                        "format": content_ext,
                    },
                },
            })

        # 构建请求
        request_data: Dict[str, Any] = {
            "task_name": task_name or f"批量审核 - {len(content_files)}个文件",
            "items": items,
            "options": {
                "crawl_official_urls": crawl_official_urls,
                "use_llm": use_llm,
                "rule_template": rule_template or "general",
                "output_format": output_format,
                # 接入 config.yaml 的 crawler 配置，替代 ReviewOptions 默认值(10页/30秒)
                "crawl_max_pages": config.crawler.max_pages,
                "crawl_timeout_seconds": config.crawler.timeout,
            },
        }
        if shared_submission:
            request_data["shared_submission"] = shared_submission

        # 处理官网URL
        if official_urls:
            urls = [u.strip() for u in official_urls.split(",") if u.strip()]
            if urls:
                request_data["shared_official_urls"] = urls

        batch_request = BatchReviewRequest(**request_data)
        progress = await batch_service.submit_batch(batch_request)
        return JSONResponse(content=progress.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量上传审核失败: {str(e)}")


# 批量抓取并发上限（避免同时启动过多浏览器）
_FETCH_CONCURRENCY = 4


async def _fetch_urls_to_items(urls: List[str]) -> tuple:
    """并行抓取 URL 列表正文，构建批量审核 items.

    Returns:
        (items, failed_urls): items 可直接用于 BatchReviewRequest；
        failed_urls 为 [{"url":..., "error":...}, ...]
    """
    async def _fetch_one(idx_url):
        idx, url = idx_url
        try:
            fetched = await _asyncio.to_thread(URLDocumentFetcher.fetch, url)
            return idx, {
                "item_id": f"item-{idx + 1}",
                "item_name": fetched.filename or url,
                "content": {
                    "input_type": "text",
                    "text": fetched.text,
                },
                "metadata": {
                    "document_url": url,
                    "document_title": fetched.filename or "",
                    "content_source": fetched.source,
                },
            }, None
        except Exception as e:
            return idx, None, {"url": url, "error": str(e)}

    sem = _asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_with_limit(idx_url):
        async with sem:
            return await _fetch_one(idx_url)

    results = await _asyncio.gather(*[_fetch_with_limit((i, u)) for i, u in enumerate(urls)])
    results.sort(key=lambda x: x[0])

    items = []
    failed_urls = []
    for idx, item_data, error in results:
        if item_data:
            items.append(item_data)
        else:
            failed_urls.append(error)
    return items, failed_urls


async def _submit_url_batch(
    request: Request,
    urls: List[str],
    submission_file: UploadFile,
    task_name: Optional[str],
    rule_template: Optional[str],
    official_urls: Optional[str],
    output_format: str,
    crawl_official_urls: bool,
    use_llm_val: bool,
) -> JSONResponse:
    """链接批量审核的共享提交流程：抓取正文 → 构建请求 → 提交批量任务."""
    config = request.app.state._config
    batch_service = request.app.state._batch_service

    # 构建共享提报表
    sub_bytes = await submission_file.read()
    sub_b64 = base64.b64encode(sub_bytes).decode("utf-8")
    sub_ext = get_file_extension(submission_file.filename)
    shared_submission = {
        "input_type": "file",
        "file": {
            "content_base64": sub_b64,
            "filename": submission_file.filename,
            "format": sub_ext,
        },
    }

    items, failed_urls = await _fetch_urls_to_items(urls)

    if not items:
        error_details = "; ".join([f"{f['url']}: {f['error']}" for f in failed_urls[:3]])
        raise HTTPException(
            status_code=400,
            detail=f"所有链接抓取失败: {error_details}",
        )

    # 构建请求
    request_data: Dict[str, Any] = {
        "task_name": task_name or f"批量审核 - {len(items)}个链接",
        "items": items,
        "options": {
            "crawl_official_urls": crawl_official_urls,
            "use_llm": use_llm_val,
            "rule_template": rule_template or "general",
            "output_format": output_format,
            # 接入 config.yaml 的 crawler 配置，替代 ReviewOptions 默认值(10页/30秒)
            "crawl_max_pages": config.crawler.max_pages,
            "crawl_timeout_seconds": config.crawler.timeout,
        },
        "shared_submission": shared_submission,
    }

    # 处理官网URL
    if official_urls:
        req_urls = [u.strip() for u in official_urls.split(",") if u.strip()]
        if req_urls:
            request_data["shared_official_urls"] = req_urls

    batch_request = BatchReviewRequest(**request_data)
    progress = await batch_service.submit_batch(batch_request)

    # 如果有部分失败，在响应中附加警告
    result = progress.model_dump()
    if failed_urls:
        result["warnings"] = [f"以下 {len(failed_urls)} 个链接抓取失败: " + ", ".join([f["url"] for f in failed_urls])]

    return JSONResponse(content=result)


@router.post("/api/v1/review/batch/urls", tags=["批量审核"])
@limiter.limit(LIMIT_BATCH)
async def batch_review_urls(
    request: Request,
    document_urls: str = Form(..., description="文档链接列表，每行一个或用分号分隔"),
    submission_file: UploadFile = File(..., description="共享提报表文件（xlsx/json/txt），必填"),
    task_name: Optional[str] = Form(None, description="批量任务名称"),
    rule_template: Optional[str] = Form(None, description="规则模板名称"),
    official_urls: Optional[str] = Form(None, description="官网URL，逗号分隔"),
    output_format: str = Form("json", description="输出格式"),
    crawl_official_urls: str = Form("false", description="是否爬取官网"),
    use_llm: str = Form("true", description="是否启用LLM语义审核"),
    current_user: UserResponse = Depends(get_current_user),
):
    """通过文档链接批量提交审核.

    粘贴多个文档链接（飞书链接等），系统自动抓取每个链接的内容，
    使用共享提报表进行批量审核。
    """
    crawl_official_urls_val = crawl_official_urls.lower() in ("true", "1", "yes")
    use_llm_val = use_llm.lower() in ("true", "1", "yes")

    try:
        # 解析 URL 列表（支持换行、分号、逗号分隔）
        raw_urls = document_urls.replace("\n", ";").replace(",", ";")
        urls = [u.strip() for u in raw_urls.split(";") if u.strip()]
        if not urls:
            raise HTTPException(status_code=400, detail="请至少输入一个文档链接")
        if len(urls) > BatchReviewRequest.MAX_BATCH_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"单次最多支持 {BatchReviewRequest.MAX_BATCH_ITEMS} 个链接",
            )

        return await _submit_url_batch(
            request, urls, submission_file, task_name, rule_template,
            official_urls, output_format, crawl_official_urls_val, use_llm_val,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量链接审核失败: {str(e)}")


@router.post("/api/v1/review/batch/from-master-doc", tags=["批量审核"])
@limiter.limit(LIMIT_BATCH)
async def batch_review_from_master_doc(
    request: Request,
    master_url: Optional[str] = Form(None, description="总文档链接（飞书表格/飞书文档/网页），与子文档 Excel 文件二选一"),
    master_file: Optional[UploadFile] = File(None, description="包含子文档链接的 Excel 文件（.xlsx/.xls），与总文档链接二选一"),
    submission_file: UploadFile = File(..., description="共享提报表文件（xlsx/json/txt），必填"),
    task_name: Optional[str] = Form(None, description="批量任务名称"),
    rule_template: Optional[str] = Form(None, description="规则模板名称"),
    official_urls: Optional[str] = Form(None, description="官网URL，逗号分隔"),
    output_format: str = Form("json", description="输出格式"),
    crawl_official_urls: str = Form("false", description="是否爬取官网"),
    use_llm: str = Form("true", description="是否启用LLM语义审核"),
    current_user: UserResponse = Depends(get_current_user),
):
    """通过总文档批量提交审核.

    用户将待审核文章的链接汇总在一个总文档中（飞书表格/飞书文档链接，
    或包含超链接的 Excel 文件），系统自动解析总文档、提取子文档链接、
    抓取正文后进行批量审核。最多提取 30 个子链接。
    """
    from geo_review.parsers.link_extractor import (
        extract_links_from_excel,
        extract_links_from_master_url,
    )

    crawl_official_urls_val = crawl_official_urls.lower() in ("true", "1", "yes")
    use_llm_val = use_llm.lower() in ("true", "1", "yes")
    max_links = BatchReviewRequest.MAX_BATCH_ITEMS

    try:
        # 二选一：总文档链接 或 Excel 文件
        has_url = bool(master_url and master_url.strip())
        has_file = bool(master_file and master_file.filename)
        if has_url == has_file:
            raise HTTPException(
                status_code=400,
                detail="请提供总文档链接或子文档 Excel 文件（二选一）",
            )

        # 提取子文档链接
        if has_url:
            try:
                urls, _title = await _asyncio.to_thread(
                    extract_links_from_master_url, master_url.strip(), max_links,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"总文档解析失败: {e}")
        else:
            file_bytes = await master_file.read()
            try:
                urls = await _asyncio.to_thread(
                    extract_links_from_excel, file_bytes, master_file.filename, max_links,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Excel 解析失败: {e}")

        if not urls:
            raise HTTPException(
                status_code=400,
                detail="未从总文档中提取到任何有效的子文档链接",
            )

        return await _submit_url_batch(
            request, urls, submission_file, task_name, rule_template,
            official_urls, output_format, crawl_official_urls_val, use_llm_val,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"总文档批量审核失败: {str(e)}")
