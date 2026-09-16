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
