"""单用户聊天与记忆的 PostgreSQL 存储。"""

import math
import os
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import psycopg
import yaml
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
CONFIG_PATH = Path(__file__).with_name("config.yaml")
_schema_lock = Lock()
_initialized = False


def _open_connection() -> psycopg.Connection:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
            database_url = data.get("database_url") if isinstance(data, dict) else None
        except (OSError, yaml.YAMLError) as exc:
            raise psycopg.OperationalError("无法读取数据库配置") from exc
    if not database_url or not isinstance(database_url, str):
        raise psycopg.OperationalError("请配置 DATABASE_URL 或 config.yaml 的 database_url")
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=3,
    )


def init_db() -> None:
    global _initialized
    with _open_connection() as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    _initialized = True


def connect() -> psycopg.Connection:
    # ponytail: 单用户阶段每次操作新建连接；并发用户增多时再加连接池。
    if not _initialized:
        with _schema_lock:
            if not _initialized:
                init_db()
    return _open_connection()


def vector_literal(values: list[float]) -> str:
    if len(values) != 512 or any(not math.isfinite(value) for value in values):
        raise ValueError("embedding 必须是 512 维有限浮点数")
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def get_character() -> dict:
    with connect() as conn:
        return conn.execute(
            """SELECT id, name, description, personality, background, speaking_style,
                      system_prompt, updated_at FROM character WHERE id = 1"""
        ).fetchone()


def update_character(
    name: str, description: str, personality: str, background: str,
    speaking_style: str, system_prompt: str
) -> dict:
    with connect() as conn:
        return conn.execute(
            """UPDATE character SET name = %s, description = %s, personality = %s,
                      background = %s, speaking_style = %s, system_prompt = %s,
                      updated_at = now()
               WHERE id = 1 RETURNING id, name, description, personality, background,
                                     speaking_style, system_prompt, updated_at""",
            (name, description, personality, background, speaking_style, system_prompt),
        ).fetchone()


def create_conversation(first_message: str) -> dict:
    conversation_id = uuid4()
    title = first_message[:40]
    with connect() as conn:
        return conn.execute(
            """INSERT INTO conversation (id, title) VALUES (%s, %s)
               RETURNING id, title, created_at, updated_at""",
            (conversation_id, title),
        ).fetchone()


def get_conversation(conversation_id: UUID) -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversation WHERE id = %s",
            (conversation_id,),
        ).fetchone()


def list_conversations() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT id, title, created_at, updated_at FROM conversation
               ORDER BY updated_at DESC"""
        ).fetchall()


def list_messages(conversation_id: UUID) -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT id, role, content, reasoning, created_at FROM message
               WHERE conversation_id = %s ORDER BY id""",
            (conversation_id,),
        ).fetchall()


def recent_messages(conversation_id: UUID, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT role, content FROM message WHERE conversation_id = %s
               ORDER BY id DESC LIMIT %s""",
            (conversation_id, limit),
        ).fetchall()
    return list(reversed(rows))


def save_turn(
    conversation_id: UUID, user_text: str, assistant_text: str, reasoning: str = ""
) -> int:
    # 两条消息在同一事务中写入，不留下半轮成功记录。
    with connect() as conn:
        user = conn.execute(
            """INSERT INTO message (conversation_id, role, content)
               VALUES (%s, 'user', %s) RETURNING id""",
            (conversation_id, user_text),
        ).fetchone()
        conn.execute(
            """INSERT INTO message (conversation_id, role, content, reasoning)
               VALUES (%s, 'assistant', %s, %s)""",
            (conversation_id, assistant_text, reasoning or None),
        )
        conn.execute(
            "UPDATE conversation SET updated_at = now() WHERE id = %s",
            (conversation_id,),
        )
        return user["id"]


def list_memories() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT id, content, importance, source_message_id, created_at, updated_at
               FROM memory ORDER BY importance DESC, updated_at DESC"""
        ).fetchall()


def memory_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) AS count FROM memory").fetchone()["count"]


def recall_memories(embedding: list[float], limit: int = 8) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT id, content, importance,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM memory ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, vector, limit),
        ).fetchall()


def role_knowledge_count(character_name: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) AS count FROM role_knowledge WHERE character_name = %s",
            (character_name,),
        ).fetchone()["count"]


def recall_role_knowledge(
    embedding: list[float], character_name: str, limit: int = 8
) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT source_key, kind, content,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM role_knowledge WHERE character_name = %s
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, character_name, vector, limit),
        ).fetchall()


def upsert_role_knowledge(
    source_key: str, kind: str, content: str, embedding: list[float]
) -> dict:
    with connect() as conn:
        character_name = conn.execute(
            "SELECT name FROM character WHERE id = 1"
        ).fetchone()["name"]
        return conn.execute(
            """INSERT INTO role_knowledge
                 (source_key, character_name, kind, content, embedding)
               VALUES (%s, %s, %s, %s, %s::vector)
               ON CONFLICT (source_key) DO UPDATE SET
                 character_name = EXCLUDED.character_name,
                 kind = EXCLUDED.kind,
                 content = EXCLUDED.content,
                 embedding = EXCLUDED.embedding,
                 updated_at = now()
               RETURNING source_key, character_name, kind, content""",
            (source_key, character_name, kind, content, vector_literal(embedding)),
        ).fetchone()


def game_dialogue_counts() -> dict[str, int]:
    with connect() as conn:
        return conn.execute(
            """SELECT (SELECT count(*) FROM game_dialogue_line) AS lines,
                      (SELECT count(*) FROM game_dialogue_chunk) AS chunks"""
        ).fetchone()


def recall_game_dialogue(embedding: list[float], limit: int = 5) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT script_name, first_entry, last_entry, scope, content,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM game_dialogue_chunk
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, vector, limit),
        ).fetchall()


def add_memory(
    content: str, importance: int, embedding: list[float], source_message_id: int | None = None
) -> dict:
    with connect() as conn:
        return conn.execute(
            """INSERT INTO memory (content, importance, embedding, source_message_id)
               VALUES (%s, %s, %s::vector, %s)
               ON CONFLICT (content) DO UPDATE SET
                 importance = greatest(memory.importance, EXCLUDED.importance),
                 updated_at = now()
               RETURNING id, content, importance, source_message_id, created_at, updated_at""",
            (content, importance, vector_literal(embedding), source_message_id),
        ).fetchone()


def update_memory(
    memory_id: int, content: str, importance: int, embedding: list[float]
) -> dict | None:
    with connect() as conn:
        return conn.execute(
            """UPDATE memory SET content = %s, importance = %s,
                      embedding = %s::vector, updated_at = now()
               WHERE id = %s
               RETURNING id, content, importance, source_message_id, created_at, updated_at""",
            (content, importance, vector_literal(embedding), memory_id),
        ).fetchone()


def delete_memory(memory_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM memory WHERE id = %s", (memory_id,)).rowcount > 0


def merge_memories(
    memory_ids: list[int], content: str, importance: int, embedding: list[float]
) -> dict | None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM memory WHERE id = ANY(%s) FOR UPDATE", (memory_ids,)
        ).fetchall()
        if len(rows) != len(memory_ids):
            return None
        target_id = memory_ids[0]
        conn.execute("DELETE FROM memory WHERE id = ANY(%s) AND id <> %s", (memory_ids, target_id))
        return conn.execute(
            """UPDATE memory SET content = %s, importance = %s,
                      embedding = %s::vector, updated_at = now()
               WHERE id = %s
               RETURNING id, content, importance, source_message_id, created_at, updated_at""",
            (content, importance, vector_literal(embedding), target_id),
        ).fetchone()
