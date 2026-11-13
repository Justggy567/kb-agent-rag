import asyncio

from app.llm import (
    chat,
    chat_json,
    chat_stream
)


async def main():

    # 测试1：普通问答
    print("=== 普通问答 ===")

    answer, usage = await chat(
        "用一句话解释什么是RAG",
        temperature=0.1
    )

    print("回答：", answer)
    print("Token：", usage)

    # 测试2：JSON 输出
    print("\n=== JSON输出 ===")

    result, usage = await chat_json(
        "请分析用户意图：查询上个月销售额。"
        "返回JSON对象，包含intent和need_database两个字段。"
    )

    print("解析结果：", result)
    print("Token：", usage)

    # 测试3：流式输出
    print("\n=== 流式输出 ===")

    async for text in chat_stream(
        "介绍一下企业知识库的作用"
    ):
        print(text, end="", flush=True)

    print("\n\n全部测试结束")


asyncio.run(main())