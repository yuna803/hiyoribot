"""每个身份独立的画像事务；正文和批次游标以 SQLite 为准。"""
import storage
import user_scope

SUBJECT = 'web:owner'
CHARACTER = 1


def key() -> tuple[str, int]:
    return user_scope.subject(), CHARACTER


def subjects() -> list[str]:
    with storage.connect() as conn:
        return [row['subject_key'] for row in conn.execute(
            'SELECT subject_key FROM user_profile WHERE character_id=?', (CHARACTER,)).fetchall()]


def ensure(initial: dict) -> dict:
    with storage.connect(write=True) as conn:
        # 与 save_turn 使用同一锁；起算不包含尚未提交的半轮/旧问答。
        conn.execute('''INSERT INTO user_profile
            (subject_key,character_id,baseline_turn_id,through_turn_id,profile)
            SELECT ?,?,COALESCE(max(id),0),COALESCE(max(id),0),? FROM completed_turn WHERE subject_key=?
            ON CONFLICT (subject_key,character_id) DO NOTHING''', (*key(),storage.json_dump(initial),user_scope.subject()))
    return get()


def get() -> dict | None:
    with storage.connect() as conn:
        return conn.execute('SELECT * FROM user_profile WHERE subject_key=? AND character_id=?',key()).fetchone()


def pending(cursor: int) -> int:
    with storage.connect() as conn:
        return conn.execute('SELECT count(*)*2 AS n FROM completed_turn WHERE id>? AND subject_key=?', (cursor,user_scope.subject())).fetchone()['n']


def source(cursor: int, count: int) -> list[dict]:
    with storage.connect() as conn:
        turns = conn.execute('''SELECT t.id AS turn_id,t.conversation_id,
            u.id AS user_id,u.content AS user_text,a.id AS assistant_id,a.content AS assistant_text
            FROM completed_turn t JOIN message u ON u.id=t.user_message_id
            JOIN message a ON a.id=t.assistant_message_id
            WHERE t.id>? AND t.subject_key=? ORDER BY t.id LIMIT ?''', (cursor,user_scope.subject(),count//2)).fetchall()
    return [dict(id=t[f'{role}_id'],content=t[f'{role}_text'],role=role,
                 conversation_id=str(t['conversation_id']),turn_id=t['turn_id'])
            for t in turns for role in ('user','assistant')]


def history(page: int, size: int) -> dict:
    with storage.connect() as conn:
        total=conn.execute('SELECT count(*) AS n FROM user_profile_history WHERE subject_key=? AND character_id=?',key()).fetchone()['n']
        rows=conn.execute('''SELECT * FROM user_profile_history WHERE subject_key=? AND character_id=?
                            ORDER BY revision DESC LIMIT ? OFFSET ?''',(*key(),size,(page-1)*size)).fetchall()
    return dict(items=rows,page=page,page_size=size,total=total)


def status(stage: str | None = None, *, error: str | None = None, export_error: str | None = None,
           export_only: bool = False) -> None:
    with storage.connect(write=True) as conn:
        if export_only:
            conn.execute('UPDATE user_profile SET export_error=? WHERE subject_key=? AND character_id=?',(export_error,*key()))
        else:
            conn.execute('UPDATE user_profile SET stage=?,error=? WHERE subject_key=? AND character_id=?',(stage,error,*key()))


def commit(old: dict, rows: list[dict], result: dict) -> bool:
    """CAS 与批次唯一键防止重启、重试、多进程重复计分。"""
    delta=result['delta']
    if type(delta) is not int or not -5<=delta<=5:
        raise ValueError('好感度变化超出范围')
    new_affinity=max(-100,min(100,old['affinity']+delta))
    with storage.connect(write=True) as conn:
        if old['subject_key']!=user_scope.subject():
            raise ValueError('画像不属于当前用户')
        turn_ids=sorted({row['turn_id'] for row in rows})
        placeholders=','.join('?' for _ in turn_ids)
        count=conn.execute(f'SELECT count(*) AS n FROM completed_turn WHERE subject_key=? AND id IN ({placeholders})',
                           (user_scope.subject(),*turn_ids)).fetchone()['n']
        if count!=len(turn_ids):
            raise ValueError('画像批次包含其他用户的消息')
        saved=conn.execute('''UPDATE user_profile SET profile=?,affinity=?,
            revision=revision+1,direct_updates=direct_updates+1,through_turn_id=?,updated_at=unixepoch(),error=NULL
            WHERE subject_key=? AND character_id=? AND revision=? AND through_turn_id=?
            RETURNING revision''', (storage.json_dump(result['profile']),new_affinity,rows[-1]['turn_id'],
                                   *key(),old['revision'],old['through_turn_id'])).fetchone()
        if not saved:
            return False
        conn.execute('''INSERT INTO user_profile_history
            (subject_key,character_id,revision,through_turn_id,source_message_ids,profile,
             old_affinity,delta,new_affinity,reason,evidence)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (*key(),saved['revision'],rows[-1]['turn_id'],storage.json_dump([row['id'] for row in rows]),
             storage.json_dump(result['profile']),old['affinity'],delta,new_affinity,result['reason'],storage.json_dump(result['evidence'])))
    return True
