
import asyncio
import time

import numpy as np
import torch
from sentence_transformers import CrossEncoder

from app.llm import embed_with_ollama


async def main():

    query = "我买的商品不满意，如何申请退货？"

    documents = [
        "购买后不想要了，怎样办理退货？",
        "公司规定符合条件的商品可以申请退货。",
        "库存不足时如何触发补货预警？",
        "如何使用 Python 配置数据库连接池？",
        "退货操作步骤：进入订单详情，点击申请售后，选择退货，填写原因后提交。",
        "商品退货后，客服根据验收情况处理退款。"
    ]

    # 第一阶段：BGE-M3
    print("=== BGE-M3 向量检索 ===")

    start = time.perf_counter()

    vectors = await embed_with_ollama(
        [query] + documents
    )

    a = np.array(vectors[0])
    scores = []

    for vector in vectors[1:]:
        b = np.array(vector)

        score = np.dot(a, b) / (
            np.linalg.norm(a) *
            np.linalg.norm(b)
        )
        scores.append(float(score))

    embedding_rank = sorted(
        range(len(documents)),
        key=lambda i: scores[i],
        reverse=True
    )

    for rank, i in enumerate(embedding_rank, 1):
        print(f"{rank}. [{i+1}] {scores[i]:.4f} {documents[i]}")

    print(f"Embedding耗时：{time.perf_counter()-start:.3f}s")

    # 第二阶段：BGE-Reranker
    print("\n=== BGE-Reranker 精排 ===")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("运行设备：", device)

    reranker = CrossEncoder(
        "BAAI/bge-reranker-v2-m3",
        device=device,
        max_length=512
    )

    # 小型实验保留全部6条候选，便于比较
    candidates = embedding_rank

    pairs = [
        (query, documents[i])
        for i in candidates
    ]

    start = time.perf_counter()

    rerank_scores = reranker.predict(
        pairs,
        batch_size=4
    )

    rerank_time = time.perf_counter() - start

    results = sorted(
        zip(candidates, rerank_scores),
        key=lambda item: float(item[1]),
        reverse=True
    )

    print("\n=== 排名对比 ===")

    for new_rank, (i, score) in enumerate(results, 1):
        old_rank = embedding_rank.index(i) + 1

        print(
            f"文档{i+1} | "
            f"原排名:{old_rank} | "
            f"新排名:{new_rank} | "
            f"余弦:{scores[i]:.4f} | "
            f"Rerank:{float(score):.4f}"
        )
        print(" ", documents[i])

    print(f"\nReranker推理耗时：{rerank_time:.3f}s")
    print("\n实验完成！")


if __name__ == "__main__":
    asyncio.run(main())
