"""RAG 知识库路由 — 文档切分入库与检索."""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from geo_review.auth.schemas import UserResponse

from .deps import get_current_user

router = APIRouter()


class RagQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    document_id: Optional[str] = None


@router.get("/api/v1/rag/stats", tags=["RAG 知识库"])
async def rag_stats(request: Request, current_user: UserResponse = Depends(get_current_user)):
    svc = request.app.state._rag_service
    return svc.get_stats()


@router.get("/api/v1/rag/documents", tags=["RAG 知识库"])
async def rag_list_documents(request: Request, current_user: UserResponse = Depends(get_current_user)):
    svc = request.app.state._rag_service
    return {"data": svc.list_documents()}


@router.post("/api/v1/rag/documents/upload", tags=["RAG 知识库"])
async def rag_upload_document(
    request: Request,
    file: UploadFile = File(..., description="txt/md/docx/pdf"),
    title: Optional[str] = Form(None),
    chunk_size: int = Form(400, ge=100, le=2000),
    chunk_overlap: int = Form(50, ge=0, le=500),
    current_user: UserResponse = Depends(get_current_user),
):
    """上传文档并执行文本切分（RAG 入库框架）."""
    svc = request.app.state._rag_service
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        doc = svc.ingest_file(
            content,
            file.filename or "upload.txt",
            title=title,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            created_by=current_user.username,
        )
        return {"status": "ok", "document": doc}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"入库失败: {e}")


@router.delete("/api/v1/rag/documents/{doc_id}", tags=["RAG 知识库"])
async def rag_delete_document(
    doc_id: str,
    request: Request,
    current_user: UserResponse = Depends(get_current_user),
):
    svc = request.app.state._rag_service
    if not svc.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"status": "deleted", "id": doc_id}


@router.post("/api/v1/rag/query", tags=["RAG 知识库"])
async def rag_query(
    body: RagQueryRequest,
    request: Request,
    current_user: UserResponse = Depends(get_current_user),
):
    svc = request.app.state._rag_service
    hits = svc.query(body.query, top_k=body.top_k, document_id=body.document_id)
    return {"query": body.query, "results": hits, "count": len(hits)}
