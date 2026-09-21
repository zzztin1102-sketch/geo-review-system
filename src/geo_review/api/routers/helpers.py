"""Shared helpers for API routers."""

import base64
from typing import Any, Dict, Optional

from fastapi import UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from geo_review.result.builder import ReviewResultFormatter
from geo_review.result.models import ReviewResponse


def format_response(result: ReviewResponse, output_format: str = "json"):
    """根据 output_format 返回对应的 FastAPI 响应.

    - json: 返回 JSONResponse（结构化审核结果）
    - markdown: 返回 PlainTextResponse（Markdown 报告）
    - html: 返回 HTMLResponse（可视化 HTML 报告）
    """
    fmt = (output_format or "json").lower()
    if fmt == "markdown":
        return PlainTextResponse(
            ReviewResultFormatter.to_markdown(result),
            media_type="text/markdown; charset=utf-8",
        )
    if fmt == "html":
        return HTMLResponse(ReviewResultFormatter.to_html(result))
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def get_file_extension(filename: Optional[str]) -> str:
    """Extract file extension without dot."""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def default_submission_payload() -> Dict[str, Any]:
    """无提报表时的默认 submission（与 JSON 审核接口一致）."""
    return {
        "input_type": "json",
        "data": {
            "task_name": "未指定任务",
            "company_name": "未指定公司",
            "product_or_service": ["未指定"],
            "core_topic": "未指定",
            "key_points": ["未指定"],
            "forbidden_claims": ["行业第一", "唯一", "100%", "最好", "最佳"],
            "official_urls": ["https://example.com"],
        },
    }


async def submission_from_upload(file: Optional[UploadFile]) -> Optional[Dict[str, Any]]:
    """将可选提报表文件转为 submission dict；未上传则返回 None."""
    if not file or not file.filename:
        return None
    sub_bytes = await file.read()
    if not sub_bytes:
        return None
    sub_b64 = base64.b64encode(sub_bytes).decode("utf-8")
    sub_ext = get_file_extension(file.filename)
    return {
        "input_type": "file",
        "file": {
            "content_base64": sub_b64,
            "filename": file.filename,
            "format": sub_ext,
        },
    }


def merge_submission(
    uploaded: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return uploaded if uploaded else default_submission_payload()


def is_admin_user(user) -> bool:
    return getattr(user, "role", None) == "admin"
