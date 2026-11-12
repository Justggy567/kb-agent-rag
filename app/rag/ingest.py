"""文档接入：解析 -> 分块 -> 二元组 -> 向量化 -> 入库。

三个决定检索质量的地方，别改：
1. **按语义边界切**：法律文本按"第 X 条"切，Markdown 按标题切，跨块不切断一条
2. **每个 chunk 必须带 page_no 和 section_path**：没有它们，引用溯源就是假的
3. **中文用二元组做关键词检索**：PG 自带的 simple 分词器不认中文，整句会被当成一个词
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from docx import Document as DocxDocument

from app.config import settings
from app.db import pool
from app.llm import embed_with_ollama

CHUNK_SIZE = 480          # 目标字符数
CHUNK_OVERLAP = 80        # 相邻块重叠
MAX_CHARS_PER_REQUEST = 6000

ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+条")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


# ---------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------
def to_bigram(text: str) -> str:
    """把中文切成二元组，英文/数字按词保留。

    "退货窗口是三十天" -> "退货 货窗 窗口 口是 是三 三十 十天"
    这样 tsvector('simple') 才能建出有效的倒排索引。
    """
    out: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_.\-]+", text):
        if re.match(r"[\u4e00-\u9fff]", token):
            if len(token) == 1:
                out.append(token)
            else:
                out.extend(token[i : i + 2] for i in range(len(token) - 1))
        else:
            out.append(token.lower())
    return " ".join(out)


def _blocks_from_text(text: str, page_no: int | None = None) -> list[dict]:
    """把文本切成 (标题路径, 页, 段落) 的块序列。"""
    blocks: list[dict] = []
    section = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if m := HEADING_RE.match(line):
            section = m.group(2).strip()
            blocks.append({"section": section, "page": page_no, "text": line})
            continue
        blocks.append({"section": section, "page": page_no, "text": line})
    return blocks


def parse_pdf(path: Path) -> list[dict]:
    blocks: list[dict] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            blocks.extend(_blocks_from_text(page.get_text("text"), page_no=i))
    return blocks


def parse_docx(path: Path) -> list[dict]:
    doc = DocxDocument(str(path))
    blocks: list[dict] = []
    section = ""
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if style.startswith("heading") or style.startswith("标题"):
            section = text
        blocks.append({"section": section, "page": None, "text": text})
    return blocks


def parse_plain(path: Path) -> list[dict]:
    return _blocks_from_text(path.read_text(encoding="utf-8", errors="ignore"))


def parse(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix in (".md", ".txt", ".markdown"):
        return parse_plain(path)
    raise ValueError(f"暂不支持的文件类型：{suffix}")


def chunk_blocks(blocks: list[dict]) -> list[dict]:
    """按语义边界合并成目标长度的块。法律条文整条不切，超过上限才硬切。"""
    chunks: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    cur_section = ""
    cur_page: int | None = None

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n".join(buf).strip()
        if text:
            chunks.append(
                {"content": text, "page_no": cur_page, "section_path": cur_section}
            )
        buf, buf_len = [], 0

    for block in blocks:
        text = block["text"]
        # 新的一"条"或新标题：能放下就放下，放不下就先收口
        is_boundary = bool(ARTICLE_RE.match(text)) or text.startswith("#")
        if is_boundary and buf_len >= CHUNK_SIZE * 0.6:
            flush()

        if buf_len + len(text) > CHUNK_SIZE and buf:
            flush()
            # 重叠：把上一块尾部 80 字带进来，避免答案正好跨在接缝上
            if chunks:
                tail = chunks[-1]["content"][-CHUNK_OVERLAP:]
                if tail:
                    buf.append(tail)
                    buf_len = len(tail)

        if not buf:
            cur_section = block["section"]
            cur_page = block["page"]
        buf.append(text)
        buf_len += len(text) + 1

        if buf_len >= CHUNK_SIZE * 1.6:   # 单条超长，硬切
            flush()

    flush()
    return chunks


# ---------------------------------------------------------------
# 入库
# ---------------------------------------------------------------
async def ingest_file(path: Path, title: str | None = None) -> dict:
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    title = title or path.stem

    async with pool().acquire() as conn:
        existed = await conn.fetchval(
            "SELECT id FROM documents WHERE file_hash = $1", file_hash
        )
        if existed:
            return {"doc_id": existed, "skipped": True, "reason": "文件已存在（sha256 相同）"}

        doc_id = await conn.fetchval(
            """INSERT INTO documents (title, source_type, file_path, file_hash,
                                      file_size, status)
               VALUES ($1, $2, $3, $4, $5, 'parsing') RETURNING id""",
            title, path.suffix.lstrip(".").lower(), str(path), file_hash, len(raw),
        )

    try:
        blocks = parse(path)
        chunks = chunk_blocks(blocks)
        if not chunks:
            raise ValueError("解析后没有提取到文本，可能是扫描件（需要 OCR）")

        # 分批 embedding，避免一次请求太大
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE documents SET status='indexing', page_count=$1 WHERE id=$2",
                max((c["page_no"] or 0) for c in chunks) or None,
                doc_id,
            )

        batch_size = 16
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            texts = [c["content"][:MAX_CHARS_PER_REQUEST] for c in batch]
            vectors = await embed_with_ollama(texts)
            rows = [
                (
                    doc_id,
                    start + i,
                    c["content"],
                    to_bigram(c["content"]),
                    str(vectors[i]),
                    c["page_no"],
                    c["section_path"],
                    len(c["content"]),
                )
                for i, c in enumerate(batch)
            ]
            async with pool().acquire() as conn:
                await conn.executemany(
                    """INSERT INTO chunks (doc_id, seq, content, content_bigram, embedding,
                                           page_no, section_path, token_count)
                       VALUES ($1, $2, $3, $4, $5::vector,
                               $6, $7, $8)""",
                    rows,
                )
                await conn.execute(
                    """UPDATE chunks SET content_tsv = to_tsvector('simple', content_bigram)
                       WHERE doc_id = $1 AND content_tsv IS NULL""",
                    doc_id,
                )

        async with pool().acquire() as conn:
            await conn.execute(
                """UPDATE documents SET status='ready', chunk_count=$1, updated_at=now()
                   WHERE id=$2""",
                len(chunks),
                doc_id,
            )
        return {"doc_id": doc_id, "chunks": len(chunks), "title": title}

    except Exception as exc:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE documents SET status='failed', error_msg=$1 WHERE id=$2",
                str(exc)[:500], doc_id,
            )
        raise


async def ingest_dir(dir_path: Path) -> list[dict]:
    results = []
    for path in sorted(dir_path.iterdir()):
        if path.suffix.lower() not in (".pdf", ".docx", ".md", ".txt", ".markdown"):
            continue
        try:
            results.append(await ingest_file(path))
            print(f"✓ {path.name}")
        except Exception as exc:
            print(f"✗ {path.name}: {exc}")
            results.append({"file": path.name, "error": str(exc)})
    return results


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("knowledge_base")
    print(asyncio.run(ingest_dir(target)))
