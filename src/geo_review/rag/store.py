"""RAG 知识库存储 — SQLite 持久化文档与切分片段."""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from geo_review.utils.time import now as beijing_now


class RagStore:
    """文档 + 文本块存储（框架层：检索使用关键词打分，可后续换向量库）."""

    def __init__(self, db_path: str = "./rag_knowledge.db"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    filename TEXT,
                    source_type TEXT,
                    chunk_size INTEGER,
                    chunk_overlap INTEGER,
                    chunk_count INTEGER DEFAULT 0,
                    char_count INTEGER DEFAULT 0,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    meta_json TEXT
                );
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    token_json TEXT,
                    FOREIGN KEY(document_id) REFERENCES rag_documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(document_id);
                """
            )

    def add_document(
        self,
        *,
        title: str,
        filename: Optional[str],
        chunks: List[str],
        chunk_size: int,
        chunk_overlap: int,
        created_by: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        doc_id = str(uuid4())
        now = beijing_now().isoformat()
        char_count = sum(len(c) for c in chunks)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rag_documents
                (id, title, filename, source_type, chunk_size, chunk_overlap,
                 chunk_count, char_count, created_by, created_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    title,
                    filename,
                    (meta or {}).get("source_type"),
                    chunk_size,
                    chunk_overlap,
                    len(chunks),
                    char_count,
                    created_by,
                    now,
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            from geo_review.rag.chunker import tokenize_for_search

            for idx, content in enumerate(chunks):
                conn.execute(
                    """
                    INSERT INTO rag_chunks (id, document_id, chunk_index, content, token_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        doc_id,
                        idx,
                        content,
                        json.dumps(tokenize_for_search(content), ensure_ascii=False),
                    ),
                )
        return self.get_document(doc_id) or {}

    def list_documents(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rag_documents ORDER BY created_at DESC"
            ).fetchall()
        return [self._doc_row_to_dict(r) for r in rows]

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rag_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return self._doc_row_to_dict(row) if row else None

    def delete_document(self, doc_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM rag_documents WHERE id = ?", (doc_id,))
            conn.execute("DELETE FROM rag_chunks WHERE document_id = ?", (doc_id,))
        return cur.rowcount > 0

    def get_chunks(self, document_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if document_id:
                rows = conn.execute(
                    "SELECT * FROM rag_chunks WHERE document_id = ? ORDER BY chunk_index",
                    (document_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rag_chunks ORDER BY document_id, chunk_index"
                ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _doc_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        meta = {}
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"])
            except json.JSONDecodeError:
                meta = {}
        return {
            "id": row["id"],
            "title": row["title"],
            "filename": row["filename"],
            "source_type": row["source_type"],
            "chunk_size": row["chunk_size"],
            "chunk_overlap": row["chunk_overlap"],
            "chunk_count": row["chunk_count"],
            "char_count": row["char_count"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "meta": meta,
        }
