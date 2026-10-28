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

-- 会话进度不跟随全局角色卡变化；旧会话默认未设置，检索采取保守边界。
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS story_progress jsonb;
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS summary_through_id bigint NOT NULL DEFAULT 0;
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS summary_revision integer NOT NULL DEFAULT 0;
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS summary_origin text NOT NULL DEFAULT 'auto';
ALTER TABLE conversation ADD COLUMN IF NOT EXISTS subject_key text NOT NULL DEFAULT 'web:owner';
CREATE INDEX IF NOT EXISTS conversation_subject_idx ON conversation(subject_key,updated_at DESC);

-- 每个用户在私聊/不同群聊保留独立上下文，画像按QQ用户跨会话累计。
CREATE TABLE IF NOT EXISTS qq_session (
    bot_id text NOT NULL,
    user_id text NOT NULL,
    group_id text NOT NULL DEFAULT '',
    conversation_id uuid NOT NULL UNIQUE REFERENCES conversation(id) ON DELETE CASCADE,
    PRIMARY KEY(bot_id,user_id,group_id)
);
-- 消息处理状态持久化，重启/重投不重复调用模型或累计画像。
CREATE TABLE IF NOT EXISTS qq_request (
    bot_id text NOT NULL,
    message_id text NOT NULL,
    subject_key text NOT NULL,
    fingerprint text NOT NULL,
    state text NOT NULL CHECK(state IN ('running','completed','failed')),
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(bot_id,message_id)
);

CREATE TABLE IF NOT EXISTS story_chapter (
    script_name text PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('common', 'hiyori')),
    max_entry integer NOT NULL CHECK (max_entry >= 0),
    source_hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS story_edge (
    source_script text NOT NULL REFERENCES story_chapter(script_name),
    source_entry integer NOT NULL,
    target_script text NOT NULL REFERENCES story_chapter(script_name),
    condition text NOT NULL DEFAULT '',
    via text NOT NULL DEFAULT '',
    PRIMARY KEY (source_script, source_entry, target_script, condition, via)
);
ALTER TABLE story_chapter ADD COLUMN IF NOT EXISTS uncertain_from integer;

ALTER TABLE message ADD COLUMN IF NOT EXISTS reasoning text;
-- 保存本轮模型工具消息，供下次聊天回传和网页查看调用记录。
ALTER TABLE message ADD COLUMN IF NOT EXISTS agent_messages jsonb;
ALTER TABLE message ADD COLUMN IF NOT EXISTS recalled_context jsonb;

CREATE INDEX IF NOT EXISTS message_conversation_idx ON message (conversation_id, id DESC);

-- 仅记录迁移后成功保存的完整问答；旧消息不回填，画像启用另设起算游标。
CREATE TABLE IF NOT EXISTS completed_turn (
    id bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    user_message_id bigint NOT NULL UNIQUE REFERENCES message(id) ON DELETE CASCADE,
    assistant_message_id bigint NOT NULL UNIQUE REFERENCES message(id) ON DELETE CASCADE,
    CHECK (user_message_id < assistant_message_id)
);
ALTER TABLE completed_turn ADD COLUMN IF NOT EXISTS subject_key text NOT NULL DEFAULT 'web:owner';
CREATE INDEX IF NOT EXISTS completed_turn_subject_idx ON completed_turn(subject_key,id);

CREATE TABLE IF NOT EXISTS user_profile (
    subject_key text NOT NULL,
    character_id integer NOT NULL REFERENCES character(id),
    baseline_turn_id bigint NOT NULL,
    through_turn_id bigint NOT NULL,
    revision integer NOT NULL DEFAULT 0,
    affinity smallint NOT NULL DEFAULT 50 CHECK (affinity BETWEEN -100 AND 100),
    profile jsonb NOT NULL,
    stage text NOT NULL DEFAULT 'idle' CHECK (stage IN ('idle','queued','running','failed')),
    error text,
    export_error text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_key, character_id)
);

CREATE TABLE IF NOT EXISTS user_profile_history (
    subject_key text NOT NULL,
    character_id integer NOT NULL,
    revision integer NOT NULL,
    through_turn_id bigint NOT NULL,
    source_message_ids bigint[] NOT NULL,
    profile jsonb NOT NULL,
    old_affinity smallint NOT NULL CHECK (old_affinity BETWEEN -100 AND 100),
    delta smallint NOT NULL CHECK (delta BETWEEN -5 AND 5),
    new_affinity smallint NOT NULL CHECK (new_affinity BETWEEN -100 AND 100),
    reason text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_key, character_id, revision),
    UNIQUE (subject_key, character_id, through_turn_id),
    FOREIGN KEY (subject_key, character_id) REFERENCES user_profile(subject_key,character_id)
);

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

-- 旧记忆保留但进入待确认；自动提取不直接当作真实事实。
ALTER TABLE memory ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'real'
    CHECK (kind IN ('real', 'roleplay', 'hypothetical'));
ALTER TABLE memory ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'confirmed', 'rejected', 'superseded'));
ALTER TABLE memory ADD COLUMN IF NOT EXISTS conversation_id uuid REFERENCES conversation(id);
ALTER TABLE memory ADD COLUMN IF NOT EXISTS fact_key text NOT NULL DEFAULT '';
ALTER TABLE memory ADD COLUMN IF NOT EXISTS dedup_key text UNIQUE;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS replaces_ids bigint[] NOT NULL DEFAULT '{}';
ALTER TABLE memory DROP CONSTRAINT IF EXISTS memory_content_key;
ALTER TABLE memory ADD COLUMN IF NOT EXISTS subject_key text NOT NULL DEFAULT 'web:owner';
CREATE INDEX IF NOT EXISTS memory_subject_idx ON memory(subject_key,status,kind);

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

-- 旁听不创建 completed_turn；原始群消息、发送许可与个人问答分别去重。
CREATE TABLE IF NOT EXISTS qq_group_state (
    bot_id text NOT NULL, group_id text NOT NULL, enabled boolean NOT NULL DEFAULT true,
    through_id bigint NOT NULL DEFAULT 0, generation bigint NOT NULL DEFAULT 0,
    summary text NOT NULL DEFAULT '', stage text NOT NULL DEFAULT 'idle', error text,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(bot_id,group_id)
);
CREATE TABLE IF NOT EXISTS qq_group_message (
    id bigserial PRIMARY KEY, bot_id text NOT NULL, group_id text NOT NULL,
    message_id text NOT NULL, user_id text NOT NULL, nickname text NOT NULL DEFAULT '',
    content text, plain_text text, quoted_message_id text, sent_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(), direct boolean NOT NULL DEFAULT false,
    is_bot boolean NOT NULL DEFAULT false, recalled boolean NOT NULL DEFAULT false,
    profile_eligible boolean NOT NULL DEFAULT false, text_hash text,
    UNIQUE(bot_id,group_id,message_id)
);
ALTER TABLE qq_group_state ADD COLUMN IF NOT EXISTS direct_inflight integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS qq_group_pending_idx ON qq_group_message(bot_id,group_id,id);
CREATE INDEX IF NOT EXISTS qq_group_person_idx ON qq_group_message(bot_id,user_id,id) WHERE profile_eligible;
CREATE TABLE IF NOT EXISTS qq_group_batch (
    id uuid PRIMARY KEY, bot_id text NOT NULL, group_id text NOT NULL,
    first_id bigint NOT NULL, last_id bigint NOT NULL, source_ids bigint[] NOT NULL,
    coverage jsonb NOT NULL DEFAULT '[]', state text NOT NULL DEFAULT 'running',
    decision jsonb, error text, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(bot_id,group_id,last_id)
);
CREATE TABLE IF NOT EXISTS qq_group_candidate (
    id uuid PRIMARY KEY, batch_id uuid NOT NULL UNIQUE REFERENCES qq_group_batch(id),
    bot_id text NOT NULL, group_id text NOT NULL, generation bigint NOT NULL,
    observed_id bigint NOT NULL, reply text NOT NULL, quote_message_id text,
    source_ids bigint[] NOT NULL, state text NOT NULL DEFAULT 'pending',
    reviewed boolean NOT NULL DEFAULT false, error text, message_id text,
    created_at timestamptz NOT NULL DEFAULT now(), attempted_at timestamptz, sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS qq_group_candidate_poll_idx ON qq_group_candidate(bot_id,state);
CREATE TABLE IF NOT EXISTS qq_passive_profile (
    bot_id text NOT NULL, user_id text NOT NULL, through_id bigint NOT NULL DEFAULT 0,
    updates integer NOT NULL DEFAULT 0, stage text NOT NULL DEFAULT 'idle', error text,
    PRIMARY KEY(bot_id,user_id)
);
-- 只保存画像实际引用的必要原话；撤回时同时撤销这些证据。
CREATE TABLE IF NOT EXISTS qq_group_evidence (
    message_id bigint PRIMARY KEY, bot_id text NOT NULL, group_id text NOT NULL,
    user_id text NOT NULL, quote text NOT NULL, recalled boolean NOT NULL DEFAULT false
);
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS direct_updates integer NOT NULL DEFAULT 0;
ALTER TABLE user_profile_history ADD COLUMN IF NOT EXISTS update_kind text NOT NULL DEFAULT 'direct';
ALTER TABLE user_profile_history DROP CONSTRAINT IF EXISTS user_profile_history_subject_key_character_id_through_turn_id_key;
-- PostgreSQL 自动生成长约束名时会缩短列名部分，兼容已部署数据库中的实际名称。
ALTER TABLE user_profile_history DROP CONSTRAINT IF EXISTS user_profile_history_subject_key_character_id_through_turn__key;
CREATE UNIQUE INDEX IF NOT EXISTS profile_direct_batch_idx
    ON user_profile_history(subject_key,character_id,through_turn_id) WHERE update_kind='direct';
UPDATE user_profile p SET direct_updates=h.n FROM
    (SELECT subject_key,character_id,count(*) AS n FROM user_profile_history WHERE update_kind='direct'
     GROUP BY subject_key,character_id) h
    WHERE p.subject_key=h.subject_key AND p.character_id=h.character_id AND p.direct_updates<h.n;
