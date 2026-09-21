"""RAG 知识库服务 — 文档入库、切分、检索."""

import io
import logging
from typing import Any, Dict, List, Optional

from geo_review.rag.chunker import split_text, tokenize_for_search
from geo_review.rag.store import RagStore

logger = logging.getLogger(__name__)


class RagService:
    """规则库 RAG：上传文档 → 切分 → 关键词检索（可扩展为向量检索）."""

    def __init__(self, db_path: str = "./rag_knowledge.db"):
        self.store = RagStore(db_path=db_path)

    @staticmethod
    def extract_text(content: bytes, filename: str) -> str:
        ext = (filename or "").rsplit(".", 1)[-1].lower()
        if ext in ("txt", "md"):
            return content.decode("utf-8", errors="ignore")
        if ext == "docx":
            import docx2txt

            return docx2txt.process(io.BytesIO(content)) or ""
        if ext == "pdf":
            from PyPDF2 import PdfReader

            reader = PdfReader(io.BytesIO(content))
            parts = []
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
            return "\n".join(parts)
        raise ValueError(f"不支持的文件类型: .{ext}（支持 txt/md/docx/pdf）")

    def ingest_file(
        self,
        content: bytes,
        filename: str,
        *,
        title: Optional[str] = None,
        chunk_size: int = 400,
        chunk_overlap: int = 50,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = self.extract_text(content, filename).strip()
        if not text:
            raise ValueError("未能从文件中提取有效文本")
        chunks = split_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if not chunks:
            raise ValueError("切分后无有效片段")
        doc = self.store.add_document(
            title=title or filename or "未命名文档",
            filename=filename,
            chunks=chunks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            created_by=created_by,
            meta={"source_type": "upload"},
        )
        logger.info("RAG 入库: %s → %s 片段", filename, len(chunks))
        return doc

    def list_documents(self) -> List[Dict[str, Any]]:
        return self.store.list_documents()

    def delete_document(self, doc_id: str) -> bool:
        return self.store.delete_document(doc_id)

    def query(
        self,
        query: str,
        *,
        top_k: int = 5,
        document_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """关键词重叠检索（框架实现，后续可换 embedding + 向量库）."""
        q_tokens = set(tokenize_for_search(query))
        if not q_tokens:
            return []

        chunks = self.store.get_chunks(document_id)
        scored: List[tuple] = []
        for ch in chunks:
            try:
                import json

                tks = set(json.loads(ch.get("token_json") or "[]"))
            except Exception:
                tks = set(tokenize_for_search(ch.get("content", "")))
            overlap = len(q_tokens & tks)
            if overlap <= 0:
                continue
            score = overlap / max(len(q_tokens), 1)
            scored.append((score, ch))

        scored.sort(key=lambda x: (-x[0], x[1]["chunk_index"]))
        results = []
        for score, ch in scored[:top_k]:
            results.append(
                {
                    "score": round(score, 4),
                    "document_id": ch["document_id"],
                    "chunk_index": ch["chunk_index"],
                    "content": ch["content"],
                }
            )
        return results

    def get_stats(self) -> Dict[str, Any]:
        docs = self.store.list_documents()
        chunk_total = sum(d.get("chunk_count") or 0 for d in docs)
        return {
            "document_count": len(docs),
            "chunk_count": chunk_total,
            "retrieval_mode": "keyword_overlap",
            "vector_ready": False,
        }
