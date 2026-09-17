-- 记忆系统权威数据表（设计依据 analysis/proposals/memory_plan_v0.2.md §5）。
-- 会话键：platform + conversation_id（如 qq + group:123456）；人键：platform + user_id（如 qq + 10001）。
-- 一切条目以会话为最小作用域；昵称/群名只作显示缓存，永不作为索引。

CREATE TABLE IF NOT EXISTS conversations (
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,              -- 类型:原生会话ID，如 group:123456
    title           TEXT NOT NULL DEFAULT '',   -- 群名/显示名缓存（仅显示）
    cursor          INTEGER NOT NULL DEFAULT 0, -- 已压缩到的消息位置（messages.id）
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (platform, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,              -- 平台消息 ID（与会话内唯一）
    user_id         TEXT NOT NULL,              -- 发送者
    display_name    TEXT NOT NULL DEFAULT '',   -- 发送时的昵称（供上下文展示）
    mentions        TEXT NOT NULL DEFAULT '[]', -- 被 @ 的人（JSON 数组，供“涉及人物”判定）
    text            TEXT NOT NULL,
    sent_at         TEXT NOT NULL,              -- 消息时间（UTC ISO8601）
    ingested_at     TEXT NOT NULL,              -- 入库时间
    UNIQUE (platform, conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (platform, conversation_id, id);

CREATE TABLE IF NOT EXISTS persons (
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    basic           TEXT NOT NULL DEFAULT '{}', -- 基本画像：五维 + 其他（JSON）
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (platform, conversation_id, user_id)
);

CREATE TABLE IF NOT EXISTS names (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- group_nickname（程序记录）| alias（LLM 追加）
    text            TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_names_person ON names (platform, conversation_id, user_id, kind, id);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_person ON entries (platform, conversation_id, user_id, id);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,           -- 第几段（每会话从 1 递增）
    msg_from        INTEGER NOT NULL,           -- 消息范围（messages.id）
    msg_to          INTEGER NOT NULL,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (platform, conversation_id, seq)
);

-- 知识库（GraphRAG-lite：词法召回 + 实体图扩展；设计见 docs/engine-spec.md §6）。
-- 文档 → 文本块 → 实体（多对多）→ 实体间关系边；检索时 BM25 召回 + 图扩展加权。

CREATE TABLE IF NOT EXISTS knowledge_docs (
    doc_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',      -- 来源标识（调研任务描述或文件路径）
    kind         TEXT NOT NULL DEFAULT 'knowledge',
    content      TEXT NOT NULL,                 -- 原文（Markdown）
    content_hash TEXT NOT NULL UNIQUE,          -- 去重（SHA-256）
    created_at   TEXT NOT NULL                  -- 资料生成时间（UTC ISO8601）
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL REFERENCES knowledge_docs (doc_id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    tokens    TEXT NOT NULL DEFAULT '[]'        -- 预分词（JSON 数组，检索用）
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks (doc_id);

CREATE TABLE IF NOT EXISTS knowledge_entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL UNIQUE              -- 归一化名（小写去空白，关联用）
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    edge_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id    INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
    dst_id    INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
    relation  TEXT NOT NULL DEFAULT '',
    doc_id    INTEGER NOT NULL REFERENCES knowledge_docs (doc_id) ON DELETE CASCADE,
    UNIQUE (src_id, dst_id, relation, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_src ON knowledge_edges (src_id);

CREATE TABLE IF NOT EXISTS knowledge_chunk_entities (
    chunk_id  INTEGER NOT NULL REFERENCES knowledge_chunks (chunk_id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES knowledge_entities (entity_id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_entities_entity ON knowledge_chunk_entities (entity_id);

-- 整合层：实体档案与主题综述（scope=entity/cluster；members 为成员实体 name_key 列表）。
-- digest = 来源块/成员签名，未变化则 consolidate 跳过不重生成。
CREATE TABLE IF NOT EXISTS knowledge_summaries (
    summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    target_key TEXT NOT NULL,              -- 实体：name_key；社区：成员签名
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    members    TEXT NOT NULL DEFAULT '[]',
    digest     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (scope, target_key)
);
