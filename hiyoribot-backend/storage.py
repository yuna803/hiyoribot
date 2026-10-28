"""SQLite权威状态；模型调用期间不持有写事务。"""
import json
import os
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4
import user_scope

ROOT=Path(__file__).resolve().parents[1]
SCHEMA_PATH=Path(__file__).with_name('schema.sql')
_path=None
_active_database=None
_ready=set()
_init_lock=threading.Lock()
_locks_guard=threading.Lock()
_locks=weakref.WeakValueDictionary()
sqlite3.register_adapter(UUID,str)
JSON_FIELDS={'profile','evidence','source_message_ids','response','agent_messages','recalled_context','source_ids','coverage','decision'}

def json_dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def _row(cursor,values):
    result=dict(zip((c[0] for c in cursor.description),values))
    for key in JSON_FIELDS & result.keys():
        if result[key] is not None:
            result[key]=json.loads(result[key])
    return result

def configure(path):
    global _path
    path=Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('SQLite必须位于仓库外')
    if _active_database is not None and path!=_active_database:
        raise ValueError('运行中不能切换SQLite路径，请重启后端')
    _path=path

def database_path():
    return _path or Path(os.environ.get('HIYORI_SQLITE_PATH',str(Path.home()/'.local/share/hiyoribot/flash/hiyoribot.sqlite3'))).expanduser().resolve()

def _open_connection():
    path=database_path()
    if path.is_relative_to(ROOT):
        raise ValueError('SQLite必须位于仓库外')
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path,timeout=5,isolation_level=None)
    conn.row_factory=_row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

def init_db():
    with _init_lock:
        path=database_path()
        if path in _ready and path.exists():
            return
        conn=_open_connection()
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA_PATH.read_text(encoding='utf8'))
            _ready.add(path)
        finally:
            conn.close()

@contextmanager
def connect(write=False):
    init_db()
    conn=_open_connection()
    try:
        if write:
            conn.execute('BEGIN IMMEDIATE')
        yield conn
        if write:
            conn.commit()
    except BaseException:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()

@contextmanager
def subject_lock(key,blocking=False):
    # 单后端进程，锁仅串行同一用户/群；事务仍由SQLite负责。
    with _locks_guard:
        lock=_locks.get(key)
        if lock is None:
            lock=threading.RLock()
            _locks[key]=lock
    acquired=lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()

def placeholders(values):
    return ','.join('?' for _ in values) or 'NULL'

def create_conversation(first_message):
    identifier=str(uuid4())
    with connect(write=True) as c:
        c.execute('INSERT INTO conversation(id,subject_key,title) VALUES(?,?,?)',(identifier,user_scope.subject(),first_message[:40]))
        return c.execute('SELECT * FROM conversation WHERE id=?',(identifier,)).fetchone()

def get_conversation(identifier):
    with connect() as c:
        return c.execute('SELECT * FROM conversation WHERE id=? AND subject_key=?',(str(identifier),user_scope.subject())).fetchone()

def recent_messages(identifier,limit=24):
    if get_conversation(identifier) is None:
        return []
    with connect() as c:
        return list(reversed(c.execute('SELECT * FROM message WHERE conversation_id=? ORDER BY id DESC LIMIT ?',(str(identifier),limit)).fetchall()))

def save_turn(identifier,user_text,reply,reasoning='',*,agent_messages=None,recalled_context=None):
    with connect(write=True) as c:
        if c.execute('SELECT 1 FROM conversation WHERE id=? AND subject_key=?',(str(identifier),user_scope.subject())).fetchone() is None:
            raise ValueError('不能保存其他用户的问答')
        uid=c.execute("INSERT INTO message(conversation_id,role,content) VALUES(?,'user',?)",(str(identifier),user_text)).lastrowid
        aid=c.execute("INSERT INTO message(conversation_id,role,content,agent_messages,recalled_context) VALUES(?,'assistant',?,?,?)",
            (str(identifier),reply,json_dump(agent_messages or []),json_dump(recalled_context or {}))).lastrowid
        c.execute('INSERT INTO completed_turn(conversation_id,user_message_id,assistant_message_id,subject_key) VALUES(?,?,?,?)',
            (str(identifier),uid,aid,user_scope.subject()))
        c.execute('UPDATE conversation SET updated_at=unixepoch() WHERE id=?',(str(identifier),))
        return uid

def group_evidence_ids(ids,group_id):
    with connect() as c:
        return {r['id'] for r in c.execute(f"""SELECT m.id FROM message m JOIN qq_session s ON s.conversation_id=m.conversation_id
            JOIN conversation v ON v.id=m.conversation_id WHERE v.subject_key=? AND s.group_id=? AND m.role='user'
            AND m.id IN ({placeholders(ids)})""",(user_scope.subject(),group_id,*ids))}

def search_chat_history(identifier,query):
    group=user_scope.public_group()
    with connect() as c:
        if group:
            return list(reversed(c.execute("""SELECT id,user_id,nickname,content FROM qq_group_message WHERE bot_id=? AND group_id=?
                AND NOT recalled AND content IS NOT NULL AND instr(lower(content),lower(?))>0 ORDER BY id DESC LIMIT 10""",(*group,query)).fetchall()))
        if get_conversation(identifier) is None:
            return []
        return list(reversed(c.execute('SELECT id,role,content FROM message WHERE conversation_id=? AND instr(lower(content),lower(?))>0 ORDER BY id DESC LIMIT 10',
            (str(identifier),query)).fetchall()))

def search_summaries(scope,query):
    with connect() as c:
        return c.execute('SELECT first_id,last_id,summary FROM summary_batch WHERE scope_key=? AND instr(lower(summary),lower(?))>0 ORDER BY id DESC LIMIT 8',(scope,query)).fetchall()


@contextmanager
def process_guard():
    global _active_database
    target=database_path().with_suffix('.process.lock')
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('a+b') as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size==0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('同一SQLite只允许一个后端进程，勿启用多worker') from error
        _active_database=database_path()
        try:
            yield
        finally:
            _active_database=None
            if os.name=='nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
