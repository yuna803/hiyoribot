-- flash: SQLite only. No game, vector or scene tables.
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS conversation (
 id TEXT PRIMARY KEY, subject_key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
 summary TEXT NOT NULL DEFAULT '', summary_through_id INTEGER NOT NULL DEFAULT 0,
 summary_revision INTEGER NOT NULL DEFAULT 0, summary_error TEXT,
 created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS message (
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversation(id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 agent_messages TEXT, recalled_context TEXT, created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS message_conversation_idx ON message(conversation_id,id);
CREATE TABLE IF NOT EXISTS completed_turn (
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversation(id),
 user_message_id INTEGER NOT NULL UNIQUE REFERENCES message(id),
 assistant_message_id INTEGER NOT NULL UNIQUE REFERENCES message(id), subject_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turn_subject_idx ON completed_turn(subject_key,id);
CREATE TABLE IF NOT EXISTS user_profile (
 subject_key TEXT NOT NULL, character_id INTEGER NOT NULL DEFAULT 1,
 baseline_turn_id INTEGER NOT NULL, through_turn_id INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, direct_updates INTEGER NOT NULL DEFAULT 0,
 affinity INTEGER NOT NULL DEFAULT 50 CHECK(affinity BETWEEN -100 AND 100),
 profile TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'idle', error TEXT, export_error TEXT,
 updated_at REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(subject_key,character_id)
);
CREATE TABLE IF NOT EXISTS user_profile_history (
 subject_key TEXT NOT NULL, character_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 through_turn_id INTEGER NOT NULL, source_message_ids TEXT NOT NULL, profile TEXT NOT NULL,
 old_affinity INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta BETWEEN -5 AND 5), new_affinity INTEGER NOT NULL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL, update_kind TEXT NOT NULL DEFAULT 'direct',
 created_at REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(subject_key,character_id,revision),
 FOREIGN KEY(subject_key,character_id) REFERENCES user_profile(subject_key,character_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS profile_direct_batch_idx ON user_profile_history(subject_key,character_id,through_turn_id) WHERE update_kind='direct';
CREATE TABLE IF NOT EXISTS qq_session (
 bot_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
 conversation_id TEXT NOT NULL UNIQUE REFERENCES conversation(id), PRIMARY KEY(bot_id,user_id,group_id)
);
CREATE TABLE IF NOT EXISTS qq_request (
 bot_id TEXT NOT NULL, message_id TEXT NOT NULL, subject_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, state TEXT NOT NULL, response TEXT,
 created_at REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(bot_id,message_id)
);
CREATE TABLE IF NOT EXISTS qq_group_state (
 bot_id TEXT NOT NULL, group_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 through_id INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
 summary TEXT NOT NULL DEFAULT '', summary_revision INTEGER NOT NULL DEFAULT 0,
 stage TEXT NOT NULL DEFAULT 'idle', error TEXT, direct_inflight INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(bot_id,group_id)
);
CREATE TABLE IF NOT EXISTS qq_group_message (
 id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
 user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', content TEXT, plain_text TEXT, quoted_message_id TEXT,
 sent_at REAL NOT NULL, received_at REAL NOT NULL DEFAULT (unixepoch()), direct INTEGER NOT NULL DEFAULT 0,
 is_bot INTEGER NOT NULL DEFAULT 0, recalled INTEGER NOT NULL DEFAULT 0,
 profile_eligible INTEGER NOT NULL DEFAULT 0, text_hash TEXT, UNIQUE(bot_id,group_id,message_id)
);
CREATE INDEX IF NOT EXISTS group_pending_idx ON qq_group_message(bot_id,group_id,id);
CREATE INDEX IF NOT EXISTS group_person_idx ON qq_group_message(bot_id,user_id,id) WHERE profile_eligible;
CREATE TABLE IF NOT EXISTS qq_group_batch (
 id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, group_id TEXT NOT NULL, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
 source_ids TEXT NOT NULL, coverage TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'running', decision TEXT,
 error TEXT, created_at REAL NOT NULL DEFAULT (unixepoch()), UNIQUE(bot_id,group_id,last_id)
);
CREATE TABLE IF NOT EXISTS qq_group_candidate (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE REFERENCES qq_group_batch(id), bot_id TEXT NOT NULL,
 group_id TEXT NOT NULL, generation INTEGER NOT NULL, observed_id INTEGER NOT NULL,
 reply TEXT NOT NULL, quote_message_id TEXT, source_ids TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 reviewed INTEGER NOT NULL DEFAULT 0, error TEXT, message_id TEXT, created_at REAL NOT NULL DEFAULT (unixepoch()),
 attempted_at REAL, sent_at REAL
);
CREATE INDEX IF NOT EXISTS candidate_poll_idx ON qq_group_candidate(bot_id,state);
CREATE TABLE IF NOT EXISTS qq_passive_profile (
 bot_id TEXT NOT NULL, user_id TEXT NOT NULL, through_id INTEGER NOT NULL DEFAULT 0,
 updates INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'idle', error TEXT, PRIMARY KEY(bot_id,user_id)
);
CREATE TABLE IF NOT EXISTS qq_group_evidence (
 message_id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, group_id TEXT NOT NULL,
 user_id TEXT NOT NULL, quote TEXT NOT NULL, recalled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS summary_batch (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
 source_ids TEXT NOT NULL, coverage TEXT NOT NULL, summary TEXT NOT NULL CHECK(length(summary)<=600),
 created_at REAL NOT NULL DEFAULT (unixepoch()), UNIQUE(scope_key,last_id)
);

-- 独立附表使已部署的flash库可以原位升级，不改旧聊天或摘要。
CREATE TABLE IF NOT EXISTS qq_followup_window (
 bot_id TEXT NOT NULL, group_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 anchor_message_id TEXT, anchor_message_ids TEXT NOT NULL DEFAULT '[]', anchor_text TEXT NOT NULL DEFAULT '', remaining INTEGER NOT NULL DEFAULT 0,
 last_sent REAL NOT NULL DEFAULT 0, PRIMARY KEY(bot_id,group_id)
);
CREATE TABLE IF NOT EXISTS qq_followup_job (
 message_row_id INTEGER PRIMARY KEY REFERENCES qq_group_message(id),
 bot_id TEXT NOT NULL, group_id TEXT NOT NULL, window_version INTEGER NOT NULL,
 state TEXT NOT NULL, saved_eligible INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '', draft TEXT, observed_id INTEGER,
 created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS followup_group_idx ON qq_followup_job(bot_id,group_id,state);
CREATE TABLE IF NOT EXISTS qq_followup_opened (
 bot_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
 PRIMARY KEY(bot_id,group_id,message_id)
);
