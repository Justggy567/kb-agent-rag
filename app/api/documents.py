"""文档管理：上传 -> 后台解析入库 -> 前端轮询状态。"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import settings
from app.db import pool
from app.rag.ingest import ingest_file
from typing import Annotated
from fastapi import Path
from app.db import ro_pool


router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED = {".pdf", ".docx", ".md", ".txt", ".markdown"}
MAX_BYTES = 30 * 1024 * 1024

_tasks: set[asyncio.Task] = set()


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or "x").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, f"只支持 {', '.join(sorted(ALLOWED))}")

    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(400, "文件超过 30MB")

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 用 uuid 前缀避免同名覆盖，同时保留原名方便排查
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    path = upload_dir / safe_name
    path.write_bytes(raw)

    async def _run():
        try:
            await ingest_file(path, title=Path(file.filename).stem)
        except Exception as exc:  # 失败状态已经写进 documents 表
            print(f"入库失败 {safe_name}: {exc}")

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return {"filename": safe_name, "size": len(raw), "status": "processing"}


@router.get("")
async def list_documents():
    async with ro_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, source_type, status, chunk_count, page_count,
                      error_msg, created_at
               FROM documents ORDER BY created_at DESC LIMIT 100"""
        )
    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "type": r["source_type"],
                "status": r["status"],
                "chunks": r["chunk_count"],
                "pages": r["page_count"],
                "error": r["error_msg"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.delete("/{doc_id}")
async def delete_document(doc_id: int):
    async with pool().acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM documents WHERE id = $1 RETURNING id", doc_id
        )
    if not deleted:
        raise HTTPException(404, "文档不存在")
    return {"deleted": doc_id}


@router.get("/stats")
async def stats():
    async with ro_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT count(*) AS docs,
                      coalesce(sum(chunk_count), 0) AS chunks
               FROM documents WHERE status = 'ready'"""
        )
    return {"documents": row["docs"], "chunks": row["chunks"]}



@router.get("/{doc_id}")
async def get_document(
    doc_id: Annotated[int, Path(gt=0)]
):
    async with ro_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                title,
                source_type,
                status,
                chunk_count,
                page_count,
                created_at
            FROM documents
            WHERE id = $1
            """,
            doc_id,
        )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="文档不存在",
        )

    return dict(row)
