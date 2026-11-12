"""混合检索：向量 + 关键词 -> RRF 融合 -> Rerank。

为什么不是单路向量：
    用户问"第25条怎么规定的"这种带精确编号的问题，向量检索经常抓不准，
    关键词（二元组）能一击命中。两路互补，用 RRF 融合不需要调权重。
"""
from __future__ import annotations

import re

from app.config import settings
from app.db import pool
from app.llm import chat_json, embed_with_ollama
from app.rag.ingest import to_bigram


async def _vector_search(query: str, top_k: int) -> list[dict]:
    vectors = await embed_with_ollama([query])
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.doc_id, c.content, c.page_no, c.section_path,
                   1 - (c.embedding <=> $1::vector) AS vec_score
            FROM chunks c
            WHERE c.embedding IS NOT NULL
            ORDER BY c.embedding <=> $1::vector
            LIMIT $2
            """,
            str(vectors[0]),
            top_k,
        )
    return [dict(r) for r in rows]


async def _keyword_search(query: str, top_k: int) -> list[dict]:
    bigram = to_bigram(query)
    if not bigram.strip():
        return []
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.doc_id, c.content, c.page_no, c.section_path,
                   ts_rank(c.content_tsv, plainto_tsquery('simple', $1)) AS kw_score
            FROM chunks c
            WHERE c.content_tsv @@ plainto_tsquery('simple', $1)
            ORDER BY kw_score DESC
            LIMIT $2
            """,
            bigram,
            top_k,
        )
    return [dict(r) for r in rows]


def rrf_fuse(*ranked_lists: list[dict], k: int | None = None) -> list[dict]:
    """Reciprocal Rank Fusion：score = Σ 1/(k + rank)。

    好处：两路分数量纲完全不同（余弦相似度 0~1，ts_rank 无上界），
    RRF 只看排名不看分数，天然免疫量纲问题，也不需要权重调参。
    """
    k = k or settings.rrf_k
    fused: dict[int, dict] = {}

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            cid = item["id"]
            if cid not in fused:
                fused[cid] = {**item, "rrf": 0.0, "from": []}
            fused[cid]["rrf"] += 1.0 / (k + rank)
            fused[cid]["from"].append(rank)

    return sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)


RERANK_PROMPT = """给下面每个候选片段与问题的相关度打分（0-10 的整数）。

10 = 直接回答了问题
5  = 部分相关，提供了背景
0  = 完全不相关

【问题】{question}

{candidates}

只输出 JSON：{{"scores": [{{"id": 片段编号, "score": 分数}}]}}"""


async def _llm_rerank(question: str, candidates: list[dict]) -> list[dict]:
    """用大模型当 cross-encoder 打分。零额外依赖，缺点是费 token，所以只跑 TopN。"""
    if not candidates:
        return []
    listing = "\n\n".join(
        f"[{c['id']}] {c['content'][:400]}" for c in candidates[: settings.rerank_top_n * 4]
    )
    data, _ = await chat_json(
        RERANK_PROMPT.format(question=question, candidates=listing)
    )
    score_map = {
        int(item["id"]): float(item["score"])
        for item in data.get("scores", [])
        if str(item.get("id", "")).lstrip("-").isdigit()
    }
    for c in candidates:
        raw = score_map.get(c["id"], 0.0)
        c["rerank_score"] = max(0.0, min(1.0, raw / 10.0))
    return sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)


_BGE = None


async def _bge_rerank(question: str, candidates: list[dict]) -> list[dict]:
    """本地 cross-encoder（可选）。pip install FlagEmbedding 后自动生效。"""
    global _BGE
    if _BGE is None:
        from FlagEmbedding import FlagReranker  # noqa: PLC0415

        _BGE = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
    pairs = [[question, c["content"][:1000]] for c in candidates]
    scores = _BGE.compute_score(pairs, normalize=True)
    for c, s in zip(candidates, scores, strict=False):
        c["rerank_score"] = float(s)
    return sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)


async def retrieve(
    question: str,
    top_k: int | None = None,
    use_rerank: bool = True,
    vector_only: bool = False,
) -> list[dict]:
    """评测时用 vector_only / use_rerank 两个开关做三组对照，这是简历上那组数字的来源。"""
    top_k = top_k or settings.retrieve_top_k

    if vector_only:
        items = await _vector_search(question, top_k)
        fused = items
    else:
        vec, kw = await _gather(question)
        fused = rrf_fuse(vec, kw)

    candidates = fused[: max(top_k * 3, 12)]
    if use_rerank and settings.rerank_backend != "none":
        try:
            if settings.rerank_backend == "bge":
                candidates = await _bge_rerank(question, candidates)
            else:
                candidates = await _llm_rerank(question, candidates)
        except Exception:
            # Rerank 挂了不能影响主流程，降级为 RRF 顺序
            pass

    top = candidates[:top_k]

    # 补文档标题（引用卡片要显示"出自哪份文件"）
    if top:
        ids = [c["doc_id"] for c in top]
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title FROM documents WHERE id = ANY($1::bigint[])", ids
            )
        titles = {r["id"]: r["title"] for r in rows}
        for c in top:
            c["doc_name"] = titles.get(c["doc_id"], f"文档{c['doc_id']}")
            c.setdefault("score", c.get("rerank_score", c.get("rrf", 0.0)))
            c.setdefault("snippet", _snippet(c["content"], question))
    return top


async def _gather(question: str):
    import asyncio

    return await asyncio.gather(
        _vector_search(question, settings.vector_top_k),
        _keyword_search(question, settings.keyword_top_k),
    )


def _snippet(content: str, question: str, width: int = 160) -> str:
    """截一段最相关的原文做引用卡片正文。"""
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}", question)
    pos = 0
    for t in tokens:
        idx = content.find(t)
        if idx >= 0:
            pos = max(0, idx - width // 3)
            break
    return content[pos : pos + width].replace("\n", " ").strip()
