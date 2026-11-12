# 企业知识库与 Agent 智能问答平台

FastAPI + LangGraph + PostgreSQL(pgvector) + DeepSeek，一个能讲清原理的知识库问答 + 经营数据查询 Agent。

**它不是"文档问答 Demo"，而是两条工具链的路由系统：**

| 用户问 | 走哪条路 | 用到什么 |
|---|---|---|
| "A 级客户的退货窗口是多少天" | `kb` | 混合检索 + Rerank |
| "上月销售额最高的 5 个品类" | `sql` | 指标语义层 + 只读执行 |
| "按折扣规范，哪些订单需要总监审批" | `both` | 文档给阈值 15%，库里查实际折扣 |
| "你好" | `direct` | 不走检索，省 token |

---

## 一、15 分钟跑起来

### 0. 前置

```bash
# 1) Ollama（只用来做 embedding，本地免费）
ollama pull bge-m3

# 2) Python 3.11+
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 1. 配置

```bash
cp .env.example .env
# 打开 .env，填入 DEEPSEEK_API_KEY
```

### 2. 起数据库

```bash
docker compose up -d db
```

首次启动会自动执行 `scripts/init.sql`（建表 + 建 pgvector 扩展 + 建只读账号）。

### 3. 导入 Northwind + 平移日期

```bash
python -m scripts.load_northwind
```

它做三件事：下载官方 dump 导入 `public`、把日期整体平移 28 年 6 个月（让"上月"有数据）、建 `nw.*` 视图（字段统一 snake_case）。

输出应该是：

```
平移后订单区间：2025-01-04 ~ 2026-11-06
  nw.orders  (11 列)
  ...
完成：订单 830 / 明细 2155 / 商品 77 / 客户 91
```

### 4. 建指标 + 灌知识库

```bash
python -m scripts.seed_metrics           # 12 个指标写进 metrics 表
python -m app.rag.ingest knowledge_base  # 4 份制度文档切块入库
```

> 知识库还差 6 份法律文本，按 `knowledge_base/知识库清单与准备指南.md` 下载后放进 `knowledge_base/` 再跑一次。

### 5. 启动

```bash
uvicorn app.main:app --reload --port 8000
```

打开 http://localhost:8000 。接口文档在 http://localhost:8000/docs 。

---

## 二、目录结构

```
app/
├── main.py                 # 启动、挂载静态、图编译
├── config.py               # 配置（pydantic-settings）
├── db.py                   # 两个连接池：写库 + 只读库
├── llm.py                  # DeepSeek chat/stream + Ollama embed
├── api/
│   ├── chat.py             # SSE 流式问答 + 会话
│   └── documents.py        # 上传 / 列表 / 删除
├── rag/
│   ├── ingest.py           # 解析→分块→二元组→向量化→入库
│   └── retriever.py        # 向量+关键词 → RRF → Rerank
├── semantic/
│   └── metrics.py          # 指标语义层（域/维度/指标定义）
└── agent/
    ├── state.py            # AgentState
    ├── prompts.py          # 全部提示词
    ├── nodes.py            # 8 个节点
    ├── graph.py            # 图的组装
    └── tools/
        ├── metric_tool.py  # 指标匹配 → 槽位抽取 → 编译 SQL → 执行
        └── sql_guard.py    # sqlglot AST 白名单校验
web/                        # 原生 HTML/CSS/JS，无构建
scripts/
├── init.sql                # 建表（容器首次启动自动执行）
├── load_northwind.py       # 导入 + 日期平移 + 建视图
├── seed_metrics.py         # 指标写入数据库
├── qa.jsonl                # 40 道测试集
└── run_eval.py             # 评测，输出四张表
```

---

## 三、四个值得在面试里讲的设计

### 1. 指标语义层：把幻觉空间从"整条 SQL"压到"选错指标"

`app/semantic/metrics.py` 里每个指标是预定义的聚合表达式 + 允许的维度：

```python
Metric(code="gmv", name="销售额", domain="order",
       expr="SUM(i.line_amount)",
       allowed_dims=["月份", "品类", "客户", "区域", ...])
```

LLM 只输出 `{"metric_code":"gmv","dims":["月份"],"start_date":"2026-10-01"}`，**SQL 由 Python 拼**。模型没有机会写 `DELETE`，也不会把 `UnitPrice` 和商品表的标准价搞混。

### 2. 三道 SQL 防线

| 层 | 手段 | 能挡住什么 |
|---|---|---|
| 1 | 只能从白名单选指标 + 维度 | 幻觉出来的表和字段 |
| 2 | `sqlglot` AST 校验：只许 SELECT、必须 `nw.*`、强制 LIMIT | `DELETE/**/FROM`、跨库读 |
| 3 | 执行用 `nw_readonly` 账号（只有 SELECT 权限） | 前两层都失效时的兜底 |

ChatBI 那个生产项目用的是正则黑名单，`DELETE/**/FROM` 能绕过去。**这一条是实打实的改进，面试可以对比着讲。**

### 3. 混合检索 + RRF + Rerank

- 中文没有天然分词，PG 的 `simple` 分词器会把整句当一个词 → 入库时用 `to_bigram()` 把中文切成二元组写进 `content_tsv`
- 向量召回 30 条 + 关键词召回 30 条 → RRF 融合（只看排名不看分数，免调权重）→ Rerank 取前 6
- 每个 chunk 都带 `page_no` 和 `section_path`，引用卡片才点得开

### 4. 图里有环

`grade` 节点判断召回分数，低于阈值就回到 `rewrite` 换说法重试，最多 2 次。这是 LangGraph 相对普通 Chain 的核心价值，也是"Agent 不是循环调工具"的具体证据。

```
rewrite → route ─┬─ kb/both → retrieve ─→ merge → grade ─┬─ retry → rewrite
                 ├─ sql ────→ text2sql ──→ merge ────────└─ pass → generate → verify → END
                 └─ direct ─────────────────────────────→ generate
```

`/api/graph` 会返回 Mermaid 源码，可以直接贴进 PPT。

---

## 四、评测

```bash
python -m scripts.run_eval --name baseline
python -m scripts.run_eval --name no-rerank --no-rerank
```

输出 `eval_report_*.md`，四张表：命中率、引用覆盖率、路由准确率、P50/P95 延迟。结果同时写进 `eval_runs` / `eval_cases`。

三组对照跑完，简历上"加 Rerank 后命中率从 xx% 提到 xx%"才有出处。

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| SSE 一次性吐出全部内容 | 前面挂了 Nginx 且开了 buffering | 加 `proxy_buffering off`，后端已带 `X-Accel-Buffering: no` |
| `Embedding 失败（HTTP 404）` | 没 pull 模型 | `ollama pull bge-m3` |
| 检索永远命中不了中文 | `content_tsv` 为空 | 入库时批量 update 那步没跑完，重新 ingest 该文档 |
| 重启后会话丢了 | checkpointer 降级成内存版 | 看启动日志里的 warning，多半是 `POSTGRES_HOST` 写成了 localhost（容器内应写 db） |
| 查询返回 0 行 | 日期没平移 | 重跑 `load_northwind.py --force` |
| `relation "nw.orders" does not exist` | 没导入 Northwind | 同上 |

---

## 六、下一步可以做的

- [ ] JWT 登录 + 多用户会话隔离（现在会话未绑定用户）
- [ ] 文档删除时同步清理 chunk（现在靠外键级联，已生效）
- [ ] 把 `sql_templates` 用起来，做成 Text2SQL 第二层兜底
- [ ] 前端引用的原文弹窗（现在只有卡片摘要）
