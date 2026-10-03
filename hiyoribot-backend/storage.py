"""单用户聊天与记忆的 PostgreSQL 存储。"""

import math
import hashlib
import os
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
        legacy = conn.execute("SELECT id,content,kind,conversation_id FROM memory WHERE dedup_key IS NULL AND status IN ('pending','confirmed')").fetchall()
        for row in legacy:
            conn.execute("UPDATE memory SET dedup_key=%s WHERE id=%s",
                         (memory_key(row["content"], row["kind"], row["conversation_id"]), row["id"]))
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


def create_conversation(first_message: str, story_progress: dict | None = None) -> dict:
    conversation_id = uuid4()
    title = first_message[:40]
    with connect() as conn:
        return conn.execute(
            """INSERT INTO conversation (id, title, story_progress) VALUES (%s, %s, %s)
               RETURNING id, title, story_progress, created_at, updated_at""",
            (conversation_id, title, Jsonb(story_progress) if story_progress is not None else None),
        ).fetchone()


def get_conversation(conversation_id: UUID) -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, title, story_progress, summary, summary_origin,summary_through_id, summary_revision, created_at, updated_at FROM conversation WHERE id = %s",
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
            """SELECT id, role, content, reasoning, agent_messages, recalled_context, created_at FROM message
               WHERE conversation_id = %s ORDER BY id""",
            (conversation_id,),
        ).fetchall()


def recent_messages(conversation_id: UUID, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT role, content, reasoning, agent_messages FROM message WHERE conversation_id = %s
               ORDER BY id DESC LIMIT %s""",
            (conversation_id, limit),
        ).fetchall()
    return list(reversed(rows))


def search_chat_history(conversation_id: UUID, query: str, limit: int = 6) -> list[dict]:
    # 参数化查询且把 LIKE 通配符转义，仅查当前会话的字面关键词。
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, role, content FROM message
               WHERE conversation_id = %s AND content ILIKE %s
               ORDER BY id DESC LIMIT %s""", (conversation_id, pattern, limit),
        ).fetchall()
    return list(reversed(rows))


def save_turn(
    conversation_id: UUID, user_text: str, assistant_text: str, reasoning: str = "",
    agent_messages: list[dict] | None = None,
    recalled_context: dict | None = None,
) -> int:
    # 两条消息在同一事务中写入，不留下半轮成功记录。
    with connect() as conn:
        user = conn.execute(
            """INSERT INTO message (conversation_id, role, content)
               VALUES (%s, 'user', %s) RETURNING id""",
            (conversation_id, user_text),
        ).fetchone()
        conn.execute(
            """INSERT INTO message (conversation_id, role, content, reasoning, agent_messages, recalled_context)
               VALUES (%s, 'assistant', %s, %s, %s, %s)""",
            (conversation_id, assistant_text, reasoning or None,
             Jsonb(agent_messages) if agent_messages else None, Jsonb(recalled_context) if recalled_context else None),
        )
        conn.execute(
            "UPDATE conversation SET updated_at = now() WHERE id = %s",
            (conversation_id,),
        )
        return user["id"]


def list_memories() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT m.id,m.content,m.importance,m.source_message_id,m.created_at,m.updated_at,
                      m.kind,m.status,m.conversation_id,m.fact_key,m.replaces_ids,
                      s.content AS source_text,s.conversation_id AS source_conversation_id,c.title AS conversation_title
               FROM memory m LEFT JOIN message s ON s.id=m.source_message_id
               LEFT JOIN conversation c ON c.id=coalesce(m.conversation_id,s.conversation_id)
               ORDER BY (m.status='pending') DESC,m.importance DESC,m.updated_at DESC"""
        ).fetchall()


def memory_count(conversation_id: UUID | None = None) -> int:
    with connect() as conn:
        return conn.execute("""SELECT count(*) AS count FROM memory WHERE status='confirmed'
                            AND (kind='real' OR (kind='roleplay' AND conversation_id=%s))""",
                            (conversation_id,)).fetchone()["count"]


def recall_memories(embedding: list[float], limit: int = 8, conversation_id: UUID | None = None) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT id, content, importance,kind,conversation_id,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM memory WHERE status='confirmed'
                 AND (kind='real' OR (kind='roleplay' AND conversation_id=%s))
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, conversation_id, vector, limit),
        ).fetchall()


def role_knowledge_count(character_name: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) AS count FROM role_knowledge WHERE character_name = %s",
            (character_name,),
        ).fetchone()["count"]


def recall_role_knowledge(
    embedding: list[float], character_name: str, limit: int = 8,
    completed_scripts: list[str] | None = None,
) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT source_key, kind, content,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM role_knowledge WHERE character_name = %s
                 AND (%s::text[] IS NULL OR
                      replace(split_part(source_key, ':', 1), 'script' || chr(92), '') = ANY(%s))
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, character_name, completed_scripts, completed_scripts, vector, limit),
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


def recall_game_dialogue(embedding: list[float], limit: int = 5,
                         bounds: dict[str, int] | None = None) -> list[dict]:
    vector = vector_literal(embedding)
    with connect() as conn:
        return conn.execute(
            """SELECT script_name, first_entry, last_entry, scope, content,
                      1 - (embedding <=> %s::vector) AS similarity
               FROM game_dialogue_chunk
               WHERE (%s::jsonb IS NULL OR EXISTS (
                   SELECT 1 FROM jsonb_each_text(%s::jsonb) AS bound
                   WHERE bound.key = script_name AND first_entry <= bound.value::integer))
               ORDER BY embedding <=> %s::vector LIMIT %s""",
            (vector, Jsonb(bounds) if bounds is not None else None,
             Jsonb(bounds) if bounds is not None else None, vector, limit),
        ).fetchall()


def story_graph() -> tuple[list[dict], list[dict]]:
    with connect() as conn:
        chapters = conn.execute("SELECT script_name, scope, max_entry, uncertain_from FROM story_chapter").fetchall()
        edges = conn.execute("SELECT source_script, source_entry, target_script, condition, via FROM story_edge").fetchall()
    return chapters, edges


def update_story_progress(conversation_id: UUID, progress: dict) -> dict | None:
    with connect() as conn:
        return conn.execute(
            """UPDATE conversation SET story_progress = %s,
               summary=CASE WHEN story_progress IS DISTINCT FROM %s THEN '' ELSE summary END,
               summary_through_id=CASE WHEN story_progress IS DISTINCT FROM %s THEN 0 ELSE summary_through_id END,
               summary_revision=summary_revision+CASE WHEN story_progress IS DISTINCT FROM %s THEN 1 ELSE 0 END,
               updated_at = now()
               WHERE id = %s RETURNING id, title, story_progress""",
            (Jsonb(progress),Jsonb(progress),Jsonb(progress),Jsonb(progress),conversation_id),
        ).fetchone()


def expand_game_dialogue(hits: list[dict], bounds: dict[str, int]) -> list[dict]:
    """补上下文时再次截住进度；未来台词不进入返回的文本。"""
    result = []
    if not hits:
        return result
    with connect() as conn:
        for hit in hits:
            maximum = bounds.get(hit["script_name"], -1)
            rows = conn.execute(
                """SELECT entry_no, speaker, content FROM game_dialogue_line
                   WHERE script_name = %s AND entry_no BETWEEN %s AND %s ORDER BY entry_no""",
                (hit["script_name"], max(0, hit["first_entry"] - 3), min(maximum, hit["last_entry"] + 3)),
            ).fetchall()
            if not rows:
                continue
            result.append({**hit, "first_entry": rows[0]["entry_no"], "last_entry": rows[-1]["entry_no"],
                           "content": "\n".join(f"{r['speaker'] or '旁白'}：{r['content']}" for r in rows)[:1400]})
    return result


def memory_key(content: str, kind: str, conversation_id: UUID | None) -> str:
    return hashlib.sha256(f"{kind}\0{conversation_id or ''}\0{content.strip()}".encode()).hexdigest()


def add_memory(
    content: str, importance: int, embedding: list[float], source_message_id: int | None = None,
    *, kind: str = "real", status: str = "confirmed", conversation_id: UUID | None = None, fact_key: str = ""
) -> dict:
    with connect() as conn:
        if kind == "roleplay" and conversation_id is None and source_message_id is not None:
            source = conn.execute("SELECT conversation_id FROM message WHERE id=%s", (source_message_id,)).fetchone()
            conversation_id = source["conversation_id"] if source else None
        if kind == "roleplay" and conversation_id is None:
            raise ValueError("角色扮演记忆需要来源会话")
        if kind != "roleplay":
            conversation_id = None
        return conn.execute(
            """INSERT INTO memory (content,importance,embedding,source_message_id,kind,status,conversation_id,fact_key,dedup_key)
               VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (dedup_key) DO UPDATE SET
                 importance = greatest(memory.importance, EXCLUDED.importance),
                 updated_at = now()
               RETURNING id,content,importance,source_message_id,kind,status,conversation_id,fact_key,created_at,updated_at""",
            (content, importance, vector_literal(embedding), source_message_id,kind,status,conversation_id,fact_key,
             memory_key(content,kind,conversation_id) if status in ("pending", "confirmed") else None),
        ).fetchone()


def update_memory(
    memory_id: int, content: str, importance: int, embedding: list[float]
) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT kind,status,conversation_id FROM memory WHERE id=%s FOR UPDATE", (memory_id,)).fetchone()
        if row is None:
            return None
        return conn.execute(
            """UPDATE memory SET content = %s, importance = %s,dedup_key=%s,
                      embedding = %s::vector, updated_at = now()
               WHERE id = %s
               RETURNING id, content, importance, source_message_id, created_at, updated_at""",
            (content, importance, memory_key(content,row["kind"],row["conversation_id"]) if row["status"] in ("pending","confirmed") else None,
             vector_literal(embedding), memory_id),
        ).fetchone()


def delete_memory(memory_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM memory WHERE id = %s", (memory_id,)).rowcount > 0


def merge_memories(
    memory_ids: list[int], content: str, importance: int, embedding: list[float]
) -> dict | None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,kind,status,conversation_id FROM memory WHERE id = ANY(%s) FOR UPDATE", (memory_ids,)
        ).fetchall()
        if len(rows) != len(memory_ids):
            return None
        target_id = memory_ids[0]
        if len({(r["kind"],r["status"],r["conversation_id"]) for r in rows}) != 1:
            raise ValueError("只能合并分类、状态和所属会话相同的记忆")
        target = rows[0]
        conn.execute("UPDATE memory SET status='superseded',dedup_key=NULL,updated_at=now() WHERE id=ANY(%s) AND id<>%s",
                     (memory_ids,target_id))
        return conn.execute(
            """UPDATE memory SET content = %s, importance = %s,
                      embedding = %s::vector,dedup_key=%s,replaces_ids=%s, updated_at = now()
               WHERE id = %s
               RETURNING id, content, importance, source_message_id, created_at, updated_at""",
            (content, importance, vector_literal(embedding),
             memory_key(content,target["kind"],target["conversation_id"]) if target["status"] in ("pending", "confirmed") else None,
             [identifier for identifier in memory_ids if identifier != target_id],target_id),
        ).fetchone()


def memory_conflicts(memory_id: int, kind: str, conversation_id: UUID | None) -> list[dict]:
    if kind == "hypothetical":
        return []
    with connect() as conn:
        return conn.execute(
            """SELECT old.id,old.content,1-(old.embedding <=> new.embedding) AS similarity
               FROM memory old JOIN memory new ON new.id=%s
               WHERE old.id<>new.id AND old.status='confirmed' AND old.kind=%s
                 AND old.conversation_id IS NOT DISTINCT FROM %s
                 AND (1-(old.embedding <=> new.embedding)>=0.72 OR
                      (new.fact_key<>'' AND old.fact_key=new.fact_key))
               ORDER BY (new.fact_key<>'' AND old.fact_key=new.fact_key) DESC,old.embedding <=> new.embedding LIMIT 3""",
            (memory_id,kind,conversation_id if kind == "roleplay" else None),
        ).fetchall()


def review_memory(memory_id: int, kind: str, status: str, conversation_id: UUID | None,
                  replace_ids: list[int]) -> dict | None:
    if len(replace_ids) != len(set(replace_ids)) or memory_id in replace_ids:
        raise ValueError("替换条目不能重复或包含自身")
    with connect() as conn:
        row = conn.execute("SELECT * FROM memory WHERE id=%s FOR UPDATE", (memory_id,)).fetchone()
        if row is None:
            return None
        if kind == "roleplay" and conversation_id is None:
            raise ValueError("请指定角色扮演记忆所属的会话")
        scope = conversation_id if kind == "roleplay" else None
        if kind == "hypothetical":
            status = "rejected"
        if replace_ids:
            old = conn.execute("SELECT id,kind,status,conversation_id FROM memory WHERE id=ANY(%s) FOR UPDATE",
                               (replace_ids,)).fetchall()
            if status != "confirmed" or len(old) != len(replace_ids) or any(
                r["kind"] != kind or r["conversation_id"] != scope or r["status"] != "confirmed" for r in old
            ):
                raise ValueError("只能替换同类、同一会话内的已确认记忆")
            conn.execute("UPDATE memory SET status='superseded',dedup_key=NULL,updated_at=now() WHERE id=ANY(%s)", (replace_ids,))
        return conn.execute(
            """UPDATE memory SET kind=%s,status=%s,conversation_id=%s,dedup_key=%s,replaces_ids=%s,updated_at=now()
               WHERE id=%s RETURNING id,content,kind,status,conversation_id,replaces_ids""",
            (kind,status,scope,memory_key(row["content"],kind,scope) if status in ("pending","confirmed") else None,replace_ids,memory_id),
        ).fetchone()


def summary_source(conversation_id: UUID, *, force: bool = False) -> tuple[dict | None, list[dict]]:
    conversation = get_conversation(conversation_id)
    if conversation is None:
        return None, []
    with connect() as conn:
        rows = conn.execute("SELECT id,role,content FROM message WHERE conversation_id=%s ORDER BY id", (conversation_id,)).fetchall()
    older = rows if force else rows[:-24]
    pending = [row for row in older if row["id"] > conversation["summary_through_id"]]
    conversation["summary_pending_count"] = len(pending)
    # 仅提交完整问答，限制输入体积；其余留给下一次摘要。
    selected, size = [], 0
    for index in range(0,len(pending)-1,2):
        turn = pending[index:index+2]
        if [row["role"] for row in turn] != ["user","assistant"]:
            break
        length = sum(len(row["content"]) for row in turn)
        if selected and size + length > 6000:
            break
        selected.extend(turn)
        size += length
    cutoff = selected[-1]["id"] if selected else conversation["summary_through_id"]
    conversation["summary_user_texts"] = [row["content"] for row in rows if row["role"]=="user" and row["id"]<=cutoff]
    return conversation, selected


def save_summary(conversation_id: UUID, summary: str, through_id: int, revision: int, *, allow_reset: bool=False,
                 origin: str="auto") -> bool:
    with connect() as conn:
        return bool(conn.execute(
            """UPDATE conversation SET summary=%s,summary_through_id=%s,summary_revision=summary_revision+1,summary_origin=%s
               WHERE id=%s AND summary_revision=%s AND (%s OR summary_through_id<=%s) RETURNING id""",
            (summary,through_id,origin,conversation_id,revision,allow_reset,through_id),
        ).fetchone())
