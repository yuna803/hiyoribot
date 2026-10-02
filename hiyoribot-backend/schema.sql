CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS character (
    id integer PRIMARY KEY CHECK (id = 1),
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    personality text NOT NULL DEFAULT '',
    background text NOT NULL DEFAULT '',
    speaking_style text NOT NULL DEFAULT '',
    system_prompt text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE character ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '';
ALTER TABLE character ADD COLUMN IF NOT EXISTS personality text NOT NULL DEFAULT '';
ALTER TABLE character ADD COLUMN IF NOT EXISTS background text NOT NULL DEFAULT '';
ALTER TABLE character ADD COLUMN IF NOT EXISTS speaking_style text NOT NULL DEFAULT '';

INSERT INTO character (id, name, system_prompt)
VALUES (1, 'HiyoriBot', '你是 HiyoriBot。请用自然、友好的中文交流；不知道的事情坦诚说明。')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS conversation (
    id uuid PRIMARY KEY,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS message (
    id bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    reasoning text,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE message ADD COLUMN IF NOT EXISTS reasoning text;
-- 保存本轮模型工具消息，供下次聊天回传和网页查看调用记录。
ALTER TABLE message ADD COLUMN IF NOT EXISTS agent_messages jsonb;

CREATE INDEX IF NOT EXISTS message_conversation_idx ON message (conversation_id, id DESC);

CREATE TABLE IF NOT EXISTS memory (
    id bigserial PRIMARY KEY,
    content text NOT NULL UNIQUE,
    importance smallint NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    embedding vector(512) NOT NULL,
    source_message_id bigint REFERENCES message(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_embedding_idx
    ON memory USING hnsw (embedding vector_cosine_ops);

-- 原作角色资料与用户长期记忆分开存放；角色名称避免切换角色后串用资料。
CREATE TABLE IF NOT EXISTS role_knowledge (
    source_key text PRIMARY KEY,
    character_name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('fact', 'style', 'scene')),
    content text NOT NULL,
    embedding vector(512) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS role_knowledge_embedding_idx
    ON role_knowledge USING hnsw (embedding vector_cosine_ops);

-- 原作台词逐条留存，检索用分块索引；不与用户事实记忆混用。
CREATE TABLE IF NOT EXISTS game_dialogue_line (
    script_name text NOT NULL,
    entry_no integer NOT NULL,
    scope text NOT NULL CHECK (scope IN ('common', 'hiyori')),
    speaker text,
    content text NOT NULL,
    source_hash text NOT NULL,
    PRIMARY KEY (script_name, entry_no)
);

CREATE TABLE IF NOT EXISTS game_dialogue_chunk (
    script_name text NOT NULL,
    first_entry integer NOT NULL,
    last_entry integer NOT NULL,
    scope text NOT NULL CHECK (scope IN ('common', 'hiyori')),
    content text NOT NULL,
    embedding vector(512) NOT NULL,
    PRIMARY KEY (script_name, first_entry),
    CHECK (last_entry >= first_entry)
);

CREATE INDEX IF NOT EXISTS game_dialogue_embedding_idx
    ON game_dialogue_chunk USING hnsw (embedding vector_cosine_ops);
