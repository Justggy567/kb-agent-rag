-- =============================================================
-- 01-init.sql  应用库初始化
-- 只在数据卷为空时执行一次；改了要重跑得 docker compose down -v
-- =============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- -------------------------------------------------------------
-- 文档
-- -------------------------------------------------------------
CREATE TABLE documents (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    source_type TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    file_hash   CHAR(64) NOT NULL,
    file_size   BIGINT,
    page_count  INT,
    status      TEXT NOT NULL DEFAULT 'pending',
    error_msg   TEXT,
    chunk_count INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_documents_status
        CHECK (status IN ('pending','parsing','indexing','ready','failed'))
);
CREATE UNIQUE INDEX ux_documents_hash ON documents (file_hash);

-- -------------------------------------------------------------
-- 分块：检索的最小单元
-- 关键：page_no + section_path 是"引用溯源"能不能做出来的前提
-- -------------------------------------------------------------
CREATE TABLE chunks (
    id             BIGSERIAL PRIMARY KEY,
    doc_id         BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq            INT NOT NULL,
    content        TEXT NOT NULL,
    content_bigram TEXT NOT NULL,          -- 中文按二元组切分，供 tsvector 用
    content_tsv    tsvector,
    embedding      vector(1024),
    page_no        INT,
    section_path   TEXT,                   -- "第二章 客户表 > 第五条 主要字段"
    token_count    INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_chunks_doc ON chunks (doc_id, seq);
CREATE INDEX ix_chunks_tsv ON chunks USING gin (content_tsv);
CREATE INDEX ix_chunks_vec ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- -------------------------------------------------------------
-- 会话 / 消息
-- -------------------------------------------------------------
CREATE TABLE sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant','tool')),
    content    TEXT,
    citations  JSONB,
    tool_calls JSONB,
    route      TEXT,
    token_in   INT,
    token_out  INT,
    latency_ms INT,
    trace_id   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_messages_session ON messages (session_id, created_at);

-- -------------------------------------------------------------
-- 指标语义层：Text2SQL 的"骨架"
-- LLM 只负责选指标 + 填参数，不负责写 SQL
-- -------------------------------------------------------------
CREATE TABLE metrics (
    code         TEXT PRIMARY KEY,          -- gmv / order_cnt ...
    name         TEXT NOT NULL,             -- 销售额
    aliases      JSONB NOT NULL DEFAULT '[]',
    domain       TEXT NOT NULL,             -- order / product
    expr         TEXT NOT NULL,             -- SQL 聚合表达式
    unit         TEXT,                      -- 元 / 单 / 件 / 天
    chart_hint   TEXT DEFAULT 'bar',        -- bar/line/pie/card/table
    allowed_dims JSONB NOT NULL DEFAULT '[]',
    description  TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------
-- SQL 模板库：三层兜底里的第二层（预留，v1 只用语义层）
-- -------------------------------------------------------------
CREATE TABLE sql_templates (
    id         BIGSERIAL PRIMARY KEY,
    question   TEXT NOT NULL,
    sql_text   TEXT NOT NULL,
    hit_count  INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------
-- 评测
-- -------------------------------------------------------------
CREATE TABLE eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    config        JSONB,
    hit_rate      NUMERIC(5,4),
    mrr           NUMERIC(5,4),
    citation_acc  NUMERIC(5,4),
    route_acc     NUMERIC(5,4),
    p50_ms        INT,
    p95_ms        INT,
    avg_token_in  INT,
    avg_token_out INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eval_cases (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    question           TEXT NOT NULL,
    expect_route       TEXT,
    actual_route       TEXT,
    ground_truth       TEXT,
    relevant_chunk_ids BIGINT[],
    retrieved_ids      BIGINT[],
    answer             TEXT,
    cited_ids          BIGINT[],
    hit                BOOLEAN,
    reciprocal_rank    NUMERIC(5,4),
    citation_ok        BOOLEAN,
    badcase_type       TEXT,
    latency_ms         INT,
    token_in           INT,
    token_out          INT
);

-- -------------------------------------------------------------
-- 只读账号：Text2SQL 执行时用它连库
-- 即使模型写出了 UPDATE，也是数据库层面直接拒绝，比任何正则都可靠
-- -------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nw_readonly') THEN
        CREATE ROLE nw_readonly LOGIN PASSWORD 'nw_readonly_pwd';
    END IF;
END $$;

GRANT CONNECT ON DATABASE ragdb TO nw_readonly;
GRANT USAGE ON SCHEMA public TO nw_readonly;
