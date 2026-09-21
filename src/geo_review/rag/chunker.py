"""文本切分 — RAG 知识库向量/检索前的文档分块."""

import re
from typing import List


def split_text(
    text: str,
    *,
    chunk_size: int = 400,
    chunk_overlap: int = 50,
) -> List[str]:
    """按字符长度切分文本（中文友好），带重叠窗口."""
    text = (text or "").strip()
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    overlap = max(0, min(chunk_overlap, chunk_size // 2))
    separators = ["\n\n", "\n", "。", "；", "！", "？", "，", " "]

    def _split_recursive(s: str, seps: List[str]) -> List[str]:
        if len(s) <= chunk_size:
            return [s] if s.strip() else []
        if not seps:
            return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size - overlap or 1)]
        sep = seps[0]
        parts = s.split(sep)
        chunks: List[str] = []
        buf = ""
        for i, part in enumerate(parts):
            piece = part if i == len(parts) - 1 else part + sep
            if len(buf) + len(piece) <= chunk_size:
                buf += piece
            else:
                if buf.strip():
                    chunks.append(buf.strip())
                if len(piece) > chunk_size:
                    chunks.extend(_split_recursive(piece, seps[1:]))
                    buf = ""
                else:
                    buf = piece
        if buf.strip():
            chunks.append(buf.strip())
        return chunks

    raw_chunks = _split_recursive(text, separators)
    if overlap <= 0 or len(raw_chunks) <= 1:
        return raw_chunks

    merged: List[str] = []
    prev_tail = ""
    for ch in raw_chunks:
        if prev_tail:
            combined = (prev_tail + ch)[: chunk_size + overlap]
            merged.append(combined)
        else:
            merged.append(ch[:chunk_size])
        prev_tail = ch[-overlap:] if len(ch) > overlap else ch
    return [c for c in merged if c.strip()]


def tokenize_for_search(text: str) -> List[str]:
    """简易分词：中文单字 + 英文/数字词."""
    text = (text or "").lower()
    tokens = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]", text)
    return tokens
