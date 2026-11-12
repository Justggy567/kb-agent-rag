"""跑一遍测试集，输出四张表 + 一条 eval_runs 记录。

用法：
    python -m scripts.run_eval --name baseline
    python -m scripts.run_eval --name no-rerank --no-rerank
    python -m scripts.run_eval --name vector-only --vector-only

三组对照跑完，简历上那句"命中率从 62% 提到 85%"才有出处。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.graph import build_graph  # noqa: E402
from app.api.chat import initial_state  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import close_pool, init_pool, pool  # noqa: E402


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def judge(case: dict, answer: str, route: str, citations: list) -> dict:
    keys = case.get("keys") or []
    hit = all(k in (answer or "") for k in keys) if keys else bool(answer)
    if not keys:
        hit = bool(answer) and "未找到" not in answer
    citation_ok = len(citations) > 0 if case["route"] in ("kb", "both") else None
    return {"hit": hit, "citation_ok": citation_ok}


def badcase_type(case: dict, answer: str, hit: bool, citations: list) -> str:
    if hit:
        return ""
    if case["route"] in ("kb", "both") and not citations:
        return "retrieve_miss"
    if "未找到" in (answer or "") and case["route"] != "unanswerable":
        return "refuse"
    return "hallucination"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qa", type=Path, default=Path("scripts/qa.jsonl"))
    parser.add_argument("--name", default="baseline")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--vector-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.no_rerank:
        settings.rerank_backend = "none"

    cases = load_cases(args.qa)
    if args.limit:
        cases = cases[: args.limit]

    await init_pool()
    graph = build_graph(checkpointer=None)

    results = []
    print(f"\n开始评测：{args.name}（{len(cases)} 题，rerank={settings.rerank_backend}）\n")

    for i, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        try:
            out = await graph.ainvoke(
                initial_state(case["q"], ""),
                config={"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 12},
            )
        except Exception as exc:  # noqa: BLE001
            out = {"answer": f"[ERROR] {exc}", "route": "", "citations": [], "usage": {}}
        latency = int((time.perf_counter() - t0) * 1000)

        answer = out.get("answer") or ""
        route = out.get("route") or ""
        citations = out.get("citations") or []
        verdict = judge(case, answer, route, citations)

        results.append(
            {
                "id": case["id"],
                "question": case["q"],
                "expect_route": case["route"],
                "actual_route": route,
                "answer": answer,
                "hit": verdict["hit"],
                "citation_ok": verdict["citation_ok"],
                "badcase": badcase_type(case, answer, verdict["hit"], citations),
                "latency_ms": latency,
                "token_in": (out.get("usage") or {}).get("token_in", 0),
                "token_out": (out.get("usage") or {}).get("token_out", 0),
                "retrieved_ids": [c.get("chunk_id") for c in citations],
            }
        )
        flag = "✓" if verdict["hit"] else "✗"
        print(f"{flag} [{i:02d}] {case['q'][:34]:<36} route={route:<6} {latency}ms")

    # ---- 汇总 ----
    n = len(results)
    hit_rate = sum(r["hit"] for r in results) / n
    cite_cases = [r for r in results if r["citation_ok"] is not None]
    citation_acc = (
        sum(1 for r in cite_cases if r["citation_ok"]) / len(cite_cases) if cite_cases else 0.0
    )
    route_acc = sum(1 for r in results if r["actual_route"] == r["expect_route"]) / n
    latencies = sorted(r["latency_ms"] for r in results)
    p50 = int(statistics.median(latencies))
    p95 = int(latencies[int(len(latencies) * 0.95) - 1]) if latencies else 0

    print("\n" + "=" * 52)
    print(f"{'命中率 (answer accuracy)':<28}{hit_rate:.1%}")
    print(f"{'引用覆盖率 (citation)':<28}{citation_acc:.1%}")
    print(f"{'路由准确率 (route)':<28}{route_acc:.1%}")
    print(f"{'P50 / P95 延迟':<28}{p50}ms / {p95}ms")
    print("=" * 52)

    bad = [r for r in results if r["badcase"]]
    if bad:
        print("\nBadcase：")
        for r in bad:
            print(f"  [{r['badcase']}] {r['question'][:40]} → {r['answer'][:60]}")

    # ---- 落库 ----
    async with pool().acquire() as conn:
        run_id = await conn.fetchval(
            """INSERT INTO eval_runs (name, config, hit_rate, mrr, citation_acc,
                                      route_acc, p50_ms, p95_ms,
                                      avg_token_in, avg_token_out)
               VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
            args.name,
            json.dumps(
                {
                    "rerank": settings.rerank_backend,
                    "vector_only": args.vector_only,
                    "top_k": settings.retrieve_top_k,
                    "rrf_k": settings.rrf_k,
                    "grade_threshold": settings.grade_threshold,
                    "at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            hit_rate,
            0.0,
            citation_acc,
            route_acc,
            p50,
            p95,
            int(statistics.mean([r["token_in"] for r in results])),
            int(statistics.mean([r["token_out"] for r in results])),
        )

        await conn.executemany(
            """INSERT INTO eval_cases (run_id, question, expect_route, actual_route,
                                       relevant_chunk_ids, retrieved_ids, answer, cited_ids,
                                       hit, reciprocal_rank, citation_ok, badcase_type,
                                       latency_ms, token_in, token_out)
               VALUES ($1,$2,$3,$4,NULL,$5::bigint[],$6,$5::bigint[],$7,0,$8,$9,$10,$11,$12)""",
            [
                (
                    run_id,
                    r["question"],
                    r["expect_route"],
                    r["actual_route"],
                    r["retrieved_ids"],
                    r["answer"],
                    r["hit"],
                    r["citation_ok"],
                    r["badcase"],
                    r["latency_ms"],
                    r["token_in"],
                    r["token_out"],
                )
                for r in results
            ],
        )

    report = Path(f"eval_report_{args.name}.md")
    report.write_text(
        "\n".join(
            [
                f"# 评测报告 · {args.name}",
                "",
                f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
                f"- 样本：{n} 题",
                f"- Rerank：{settings.rerank_backend}",
                "",
                "| 指标 | 数值 |",
                "|---|---|",
                f"| 命中率 | {hit_rate:.1%} |",
                f"| 引用覆盖率 | {citation_acc:.1%} |",
                f"| 路由准确率 | {route_acc:.1%} |",
                f"| P50 延迟 | {p50} ms |",
                f"| P95 延迟 | {p95} ms |",
                "",
                "## 逐题结果",
                "",
                "| # | 问题 | 期望路线 | 实际 | 命中 | 耗时 |",
                "|---|---|---|---|---|---|",
                *[
                    f"| {r['id']} | {r['question']} | {r['expect_route']} | "
                    f"{r['actual_route']} | {'✓' if r['hit'] else '✗'} | {r['latency_ms']}ms |"
                    for r in results
                ],
            ]
        ),
        encoding="utf-8",
    )
    print(f"\n报告已写入 {report.resolve()}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
