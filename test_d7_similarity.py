import asyncio
import math
import numpy as np

from app.llm import embed_with_ollama


# 第一部分：手写余弦相似度
def cosine_manual(a, b):
    if len(a) != len(b) or not a:
        raise ValueError("向量为空或维度不一致")

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0 or norm_b == 0:
        raise ValueError("不能计算零向量的余弦相似度")

    return dot / (norm_a * norm_b)


# 第二部分：NumPy 版本
def cosine_numpy(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    return float(
        np.dot(a, b) /
        (np.linalg.norm(a) * np.linalg.norm(b))
    )


async def main():

    sentences = [
        "我买的商品不满意，如何申请退货？",
        "购买后不想要了，怎样办理退货？",
        "公司规定符合条件的商品可以申请退货。",
        "库存不足时如何触发补货预警？",
        "如何使用 Python 配置数据库连接池？"
    ]

    print("=== 开始批量向量化 ===")

    # 一次调用，将5句话全部转换为向量
    vectors = await embed_with_ollama(sentences)

    print("向量数量：", len(vectors))
    print("向量维度：", len(vectors[0]))

    assert len(vectors) == 5
    assert all(len(v) == 1024 for v in vectors)

    print("\n=== 与第一句话的相似度 ===")

    for i in range(1, len(sentences)):
        score1 = cosine_manual(vectors[0], vectors[i])
        score2 = cosine_numpy(vectors[0], vectors[i])

        assert abs(score1 - score2) < 1e-6

        print(f"A 与 {chr(65+i)}：{score1:.4f}")
        print(f"  {sentences[i]}")

    print("\n=== 完整相似度矩阵 ===")

    print("       A      B      C      D      E")

    for i in range(len(vectors)):
        scores = [
            cosine_manual(vectors[i], vectors[j])
            for j in range(len(vectors))
        ]

        row = " ".join(f"{s:.3f}" for s in scores)
        print(f"{chr(65+i)}   {row}")


asyncio.run(main())
