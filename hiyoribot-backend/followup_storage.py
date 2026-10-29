"""跟聊窗口与一次性任务。所有状态变更都在调用方的短SQLite事务中。"""
import re
import time
from functools import lru_cache

import storage

WAITING=('pending','processing','prepared')


@lru_cache(maxsize=32)
def patterns(values):
    return tuple(re.compile(v) for v in values)


def name_matches(text,values):
    # 管理/插件命令优先，名字只查本条原始文字，不查昵称和引用正文。
    return bool(text and not text.lstrip().startswith(('/','!','！')) and
                any(p.search(text) for p in patterns(tuple(values))))


def window(c,bot,group):
    c.execute('INSERT OR IGNORE INTO qq_followup_window(bot_id,group_id) VALUES(?,?)',(bot,group))
    return c.execute('SELECT * FROM qq_followup_window WHERE bot_id=? AND group_id=?',(bot,group)).fetchone()


def settle(c,row_id,state,reason):
    job=c.execute('SELECT * FROM qq_followup_job WHERE message_row_id=?',(row_id,)).fetchone()
    if not job or job['state'] not in WAITING:
        return
    c.execute('UPDATE qq_followup_job SET state=?,reason=?,draft=NULL WHERE message_row_id=?',(state,reason,row_id))
    c.execute('UPDATE qq_group_message SET profile_eligible=? WHERE id=? AND NOT direct AND NOT recalled AND plain_text IS NOT NULL',
              (job['saved_eligible'],row_id))
    message=c.execute('SELECT * FROM qq_group_message WHERE id=?',(row_id,)).fetchone()
    if job['saved_eligible']:
        c.execute('INSERT OR IGNORE INTO qq_passive_profile(bot_id,user_id) VALUES(?,?)',(job['bot_id'],message['user_id']))
    c.execute("UPDATE qq_request SET state='failed',response=NULL WHERE bot_id=? AND message_id=? AND state IN ('running','prepared')",
              (job['bot_id'],message['message_id']))


def close(c,bot,group,reason):
    window(c,bot,group)
    c.execute("UPDATE qq_followup_window SET version=version+1,remaining=0,anchor_message_id=NULL,anchor_message_ids='[]',anchor_text='' WHERE bot_id=? AND group_id=?",(bot,group))
    for j in c.execute("SELECT message_row_id FROM qq_followup_job WHERE bot_id=? AND group_id=? AND state IN ('pending','processing','prepared')",(bot,group)).fetchall():
        settle(c,j['message_row_id'],'discarded',reason)


def open_window(c,body):
    # 重复或晚到的旧回执都不续期，不能仅与当前锚点比较。
    fresh=c.execute('INSERT OR IGNORE INTO qq_followup_opened VALUES(?,?,?)',
                    (body.bot_id,body.group_id,body.message_id)).rowcount
    if not fresh:
        return
    close(c,body.bot_id,body.group_id,'新的角色回复')
    enabled=c.execute('SELECT enabled FROM qq_group_state WHERE bot_id=? AND group_id=?',(body.bot_id,body.group_id)).fetchone()
    if enabled and enabled['enabled']:
        c.execute('''UPDATE qq_followup_window SET anchor_message_id=?,anchor_message_ids=?,anchor_text=?,remaining=5,last_sent=?
            WHERE bot_id=? AND group_id=?''',(body.message_id,storage.json_dump(body.segment_message_ids or [body.message_id]),
            body.message,time.time(),body.bot_id,body.group_id))
        if body.reply_to_message_id is not None:
            source=c.execute('SELECT id FROM qq_group_message WHERE bot_id=? AND group_id=? AND message_id=? AND NOT is_bot AND NOT recalled',
                             (body.bot_id,body.group_id,body.reply_to_message_id)).fetchone()
            if source:
                current=window(c,body.bot_id,body.group_id)
                c.execute('INSERT INTO qq_followup_origin VALUES(?,?,?,?)',
                          (body.bot_id,body.group_id,current['version'],source['id']))


def reserve(c,body,row_id,eligible):
    w=window(c,body.bot_id,body.group_id)
    if body.direct:
        close(c,body.bot_id,body.group_id,'直接互动优先')
        return {}
    if not w['remaining'] or not w['anchor_message_id']:
        return {}
    c.execute('UPDATE qq_followup_window SET remaining=remaining-1 WHERE bot_id=? AND group_id=?',(body.bot_id,body.group_id))
    reason=None
    if not body.text.strip() or body.text.lstrip().startswith(('/','!','！')):
        reason='非文字或命令，仅计数'
    elif getattr(body,'addresses_other',False):
        reason='明确@其他成员，仅计数'
    elif body.quoted_message_id is not None:
        target=c.execute('SELECT is_bot,user_id FROM qq_group_message WHERE bot_id=? AND group_id=? AND message_id=?',
                         (body.bot_id,body.group_id,body.quoted_message_id)).fetchone()
        if target and not target['is_bot'] and target['user_id']!=body.user_id:
            reason='明确引用其他成员，仅计数'
    c.execute('''INSERT INTO qq_followup_job(message_row_id,bot_id,group_id,window_version,state,saved_eligible,reason)
        VALUES(?,?,?,?,?,?,?)''',(row_id,body.bot_id,body.group_id,w['version'],'skipped' if reason else 'pending',eligible,reason or '等待判断'))
    if not reason:
        c.execute('UPDATE qq_group_message SET profile_eligible=false WHERE id=?',(row_id,))
    return {'followup':not bool(reason),'window_version':w['version'],'remaining':w['remaining']-1}


def get_job(c,body):
    return c.execute('''SELECT j.*,m.message_id,m.user_id,m.nickname,m.plain_text,m.recalled,m.content
        FROM qq_followup_job j JOIN qq_group_message m ON m.id=j.message_row_id
        WHERE j.bot_id=? AND j.group_id=? AND m.message_id=? AND j.window_version=?''',
        (body.bot_id,body.group_id,body.message_id,body.window_version)).fetchone()


def valid(c,job):
    w=window(c,job['bot_id'],job['group_id'])
    state=c.execute('SELECT enabled,direct_inflight FROM qq_group_state WHERE bot_id=? AND group_id=?',
                    (job['bot_id'],job['group_id'])).fetchone()
    return bool(state and state['enabled'] and not state['direct_inflight'] and w['anchor_message_id']
                and w['version']==job['window_version'] and not job['recalled'] and job['content'] is not None)


def last_id(c,bot,group):
    return c.execute('SELECT COALESCE(max(id),0) AS n FROM qq_group_message WHERE bot_id=? AND group_id=? AND NOT recalled',
                     (bot,group)).fetchone()['n']


def active(c,bot,group):
    w=window(c,bot,group)
    return bool(w['remaining'] or c.execute("SELECT 1 FROM qq_followup_job WHERE bot_id=? AND group_id=? AND state IN ('pending','processing','prepared','sending') LIMIT 1",(bot,group)).fetchone())


def restart(c,bot):
    groups=c.execute('SELECT group_id FROM qq_followup_window WHERE bot_id=?',(bot,)).fetchall()
    for g in groups:
        close(c,bot,g['group_id'],'断线或重启，不补发')
    c.execute("UPDATE qq_followup_job SET state='uncertain',draft=NULL,reason='重启不补发' WHERE bot_id=? AND state='sending'",(bot,))


def recall(c,body):
    w=window(c,body.bot_id,body.group_id)
    import json
    if body.message_id in json.loads(w['anchor_message_ids']):
        close(c,body.bot_id,body.group_id,'锚定角色回复撤回')
    # 复用现有撤回的保守原则：本群撤回会使尚未发出的草稿失效。
    for j in c.execute("SELECT message_row_id FROM qq_followup_job WHERE bot_id=? AND group_id=? AND state IN ('pending','processing','prepared')",(body.bot_id,body.group_id)).fetchall():
        settle(c,j['message_row_id'],'discarded','依据可能撤回')
