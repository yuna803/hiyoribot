"""群消息账本与一次性发送许可。模型永远不能决定投递群号。"""
import hashlib
import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

import storage


def ensure(conn, bot, group):
    conn.execute('INSERT INTO qq_group_state(bot_id,group_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (bot,group))
    return conn.execute('SELECT * FROM qq_group_state WHERE bot_id=%s AND group_id=%s FOR UPDATE', (bot,group)).fetchone()


def invalidate(conn, bot, group, reason):
    conn.execute('UPDATE qq_group_state SET generation=generation+1 WHERE bot_id=%s AND group_id=%s', (bot,group))
    conn.execute("UPDATE qq_group_candidate SET state='discarded',error=%s WHERE bot_id=%s AND group_id=%s AND state IN ('pending','reviewing')",
                 (reason,bot,group))


def pending(conn, state):
    return conn.execute('''SELECT count(*) AS n FROM qq_group_message WHERE bot_id=%s AND group_id=%s
        AND id>%s AND NOT is_bot AND NOT recalled AND content IS NOT NULL''',
        (state['bot_id'],state['group_id'],state['through_id'])).fetchone()['n']


def ingest(body):
    with storage.connect() as conn:
        state=ensure(conn,body.bot_id,body.group_id)
        if body.user_id==body.bot_id:
            return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=False)
        old=conn.execute('SELECT id FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND message_id=%s',
                         (body.bot_id,body.group_id,body.message_id)).fetchone()
        if old or (not state['enabled'] and not body.direct):
            return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=False)
        # 暂停时只保留直接互动的失效信号，不继续累计旁听。
        if body.direct:
            invalidate(conn,body.bot_id,body.group_id,'直接互动优先')
        normalized=re.sub(r'[\W_]+','',body.text).casefold()
        digest=hashlib.sha256(normalized.encode()).hexdigest() if normalized else None
        duplicate=bool(digest and conn.execute('SELECT 1 FROM qq_group_message WHERE bot_id=%s AND user_id=%s AND text_hash=%s LIMIT 1',
                                               (body.bot_id,body.user_id,digest)).fetchone())
        eligible=(state['enabled'] and not body.direct and not duplicate and len(normalized)>=4
                  and not body.text.lstrip().startswith(('/','！','!')))
        conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,nickname,content,plain_text,
            quoted_message_id,sent_at,direct,profile_eligible,text_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (body.bot_id,body.group_id,body.message_id,body.user_id,body.nickname,
             body.message if state['enabled'] else None,body.text if state['enabled'] else None,
             body.quoted_message_id,datetime.fromtimestamp(body.sent_at,timezone.utc),body.direct,eligible,digest))
        if eligible:
            conn.execute('INSERT INTO qq_passive_profile(bot_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (body.bot_id,body.user_id))
        return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=True)


def toggle(body, owner):
    if body.user_id!=owner:
        raise HTTPException(403,'只有绑定本人可以修改旁听开关')
    with storage.connect() as conn:
        state=ensure(conn,body.bot_id,body.group_id)
        if state['enabled']!=body.enabled:
            invalidate(conn,body.bot_id,body.group_id,'旁听开关变更')
            last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=%s AND group_id=%s',
                              (body.bot_id,body.group_id)).fetchone()['id']
            conn.execute('UPDATE qq_group_state SET enabled=%s,through_id=%s,stage=\'idle\',error=NULL WHERE bot_id=%s AND group_id=%s',
                         (body.enabled,last,body.bot_id,body.group_id))
        return dict(enabled=body.enabled,pending=0 if state['enabled']!=body.enabled else pending(conn,state))


def bot_message(body):
    with storage.connect() as conn:
        ensure(conn,body.bot_id,body.group_id)
        conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,content,plain_text,sent_at,is_bot)
            VALUES(%s,%s,%s,%s,%s,%s,now(),true) ON CONFLICT DO NOTHING''',
            (body.bot_id,body.group_id,body.message_id,body.bot_id,body.message,body.message))
    return {'ok':True}


def rows(bot, group, *, after=0, limit=50, users_only=False):
    with storage.connect() as conn:
        return conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND id>%s
            AND content IS NOT NULL AND NOT recalled AND (NOT %s OR NOT is_bot) ORDER BY id LIMIT %s''',
            (bot,group,after,users_only,limit)).fetchall()


def recent(bot, group, limit=20):
    with storage.connect() as conn:
        return list(reversed(conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=%s AND group_id=%s
            AND content IS NOT NULL AND NOT recalled ORDER BY id DESC LIMIT %s''',(bot,group,limit)).fetchall()))


def bot_recent(bot,group):
    with storage.connect() as conn:
        return list(reversed(conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=%s AND group_id=%s
            AND is_bot AND NOT recalled AND content IS NOT NULL AND received_at>now()-interval '1 hour'
            ORDER BY id DESC LIMIT 5''',(bot,group)).fetchall()))


def state(bot, group):
    with storage.connect() as conn:
        return conn.execute('SELECT * FROM qq_group_state WHERE bot_id=%s AND group_id=%s',(bot,group)).fetchone()


def quota(conn, bot, group):
    # uncertain 与失败尝试也占冷却，防止网络抖动造成刷屏；发送许可不重试。
    active=conn.execute('SELECT direct_inflight FROM qq_group_state WHERE bot_id=%s AND group_id=%s',(bot,group)).fetchone()
    if active and active['direct_inflight']:
        return False
    row=conn.execute('''SELECT count(*) AS n, max(attempted_at) AS last FROM qq_group_candidate
        WHERE bot_id=%s AND group_id=%s AND attempted_at>now()-interval '1 hour' ''',(bot,group)).fetchone()
    return row['n']<3 and (row['last'] is None or (datetime.now(timezone.utc)-row['last']).total_seconds()>=300)


def begin_batch(bot, group):
    with storage.connect() as conn:
        current=ensure(conn,bot,group)
        if not current['enabled']:
            return None
        batch=conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND id>%s
            AND NOT is_bot AND NOT recalled AND content IS NOT NULL ORDER BY id LIMIT 50''',
            (bot,group,current['through_id'])).fetchall()
        if len(batch)<50:
            conn.execute("UPDATE qq_group_state SET stage='idle' WHERE bot_id=%s AND group_id=%s",(bot,group))
            return None
        identifier=uuid4()
        conn.execute('''INSERT INTO qq_group_batch(id,bot_id,group_id,first_id,last_id,source_ids)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(bot_id,group_id,last_id)
            DO UPDATE SET state='running',error=NULL,coverage='[]',source_ids=EXCLUDED.source_ids RETURNING id''',
            (identifier,bot,group,batch[0]['id'],batch[-1]['id'],[r['id'] for r in batch]))
        saved=conn.execute('SELECT id FROM qq_group_batch WHERE bot_id=%s AND group_id=%s AND last_id=%s',
                           (bot,group,batch[-1]['id'])).fetchone()['id']
        conn.execute("UPDATE qq_group_state SET stage='running',error=NULL WHERE bot_id=%s AND group_id=%s",(bot,group))
        return current,batch,saved


def finish_batch(old, batch, identifier, decision, coverage):
    with storage.connect() as conn:
        now=ensure(conn,old['bot_id'],old['group_id'])
        valid=now['enabled'] and now['generation']==old['generation'] and now['through_id']==old['through_id']
        conn.execute('UPDATE qq_group_batch SET state=%s,decision=%s,coverage=%s WHERE id=%s',
                     ('completed' if valid else 'superseded',Jsonb(decision),Jsonb(coverage),identifier))
        # 新直接互动只作废插话，不让已整理的50条被反复付费整理；开关重置游标则不覆盖。
        alive=conn.execute('SELECT count(*) AS n FROM qq_group_message WHERE id=ANY(%s) AND NOT recalled AND content IS NOT NULL',
                           ([r['id'] for r in batch],)).fetchone()['n']==len(batch)
        if now['enabled'] and now['through_id']==old['through_id'] and alive:
            conn.execute('UPDATE qq_group_state SET through_id=%s,summary=%s,stage=\'idle\',updated_at=now() WHERE bot_id=%s AND group_id=%s',
                         (batch[-1]['id'],decision['summary'],old['bot_id'],old['group_id']))
        return valid and alive and decision['speak'] and quota(conn,old['bot_id'],old['group_id'])


def save_candidate(old, batch, identifier, reply, quote):
    with storage.connect() as conn:
        now=ensure(conn,old['bot_id'],old['group_id'])
        if not now['enabled'] or now['generation']!=old['generation'] or not quota(conn,old['bot_id'],old['group_id']):
            return
        # 保存生成开始时的末条ID，普通新消息将触发发送前复核。
        observed=batch[-1]['id']
        conn.execute('''INSERT INTO qq_group_candidate(id,batch_id,bot_id,group_id,generation,observed_id,reply,
            quote_message_id,source_ids) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(batch_id) DO NOTHING''',
            (uuid4(),identifier,old['bot_id'],old['group_id'],old['generation'],observed,reply,quote,[r['id'] for r in batch]))


def fail(bot, group, error):
    with storage.connect() as conn:
        conn.execute("UPDATE qq_group_state SET stage='failed',error=%s WHERE bot_id=%s AND group_id=%s",(error,bot,group))
        conn.execute("UPDATE qq_group_batch SET state='failed',error=%s WHERE bot_id=%s AND group_id=%s AND state='running'",(error,bot,group))


def candidates(bot):
    with storage.connect() as conn:
        conn.execute("UPDATE qq_group_candidate SET state='discarded',error='候选已过期' WHERE bot_id=%s AND state IN ('pending','reviewing') AND created_at<now()-interval '10 minutes'",(bot,))
        return {'items':conn.execute("SELECT id,group_id FROM qq_group_candidate WHERE bot_id=%s AND state='pending' ORDER BY created_at LIMIT 20",(bot,)).fetchall()}


def prepare_claim(bot, identifier):
    with storage.connect() as conn:
        candidate=conn.execute('SELECT * FROM qq_group_candidate WHERE bot_id=%s AND id=%s',(bot,identifier)).fetchone()
        if candidate is None:
            return None
        current=ensure(conn,bot,candidate['group_id'])
        saved=conn.execute('''UPDATE qq_group_candidate SET state='reviewing' WHERE id=%s AND state='pending'
            AND created_at>now()-interval '10 minutes' RETURNING *''',(identifier,)).fetchone()
        if not saved:
            return None
        if not current['enabled'] or current['generation']!=candidate['generation'] or not quota(conn,bot,candidate['group_id']):
            conn.execute("UPDATE qq_group_candidate SET state='discarded',error='失效或频率限制' WHERE id=%s",(identifier,))
            return None
        last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND NOT recalled',
                          (bot,candidate['group_id'])).fetchone()['id']
        return candidate,last


def permit(candidate, observed, approved):
    with storage.connect() as conn:
        now=ensure(conn,candidate['bot_id'],candidate['group_id'])
        last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND NOT recalled',
                          (candidate['bot_id'],candidate['group_id'])).fetchone()['id']
        valid=approved and now['enabled'] and now['generation']==candidate['generation'] and last==observed and quota(conn,candidate['bot_id'],candidate['group_id'])
        row=conn.execute('''UPDATE qq_group_candidate SET state=%s,reviewed=%s,attempted_at=CASE WHEN %s THEN now() ELSE NULL END,
            error=%s WHERE id=%s AND state='reviewing' RETURNING id''',
            ('sending' if valid else 'discarded',observed!=candidate['observed_id'],valid,
             None if valid else '新消息、直接互动或复核否决',candidate['id'])).fetchone()
        if not row or not valid:
            return {'send':False}
        return dict(send=True,group_id=candidate['group_id'],reply=candidate['reply'],quote_message_id=candidate['quote_message_id'])


def receipt(body):
    with storage.connect() as conn:
        conn.execute('''UPDATE qq_group_candidate SET state=%s,message_id=%s,sent_at=CASE WHEN %s='sent' THEN now() ELSE NULL END
            WHERE bot_id=%s AND id=%s AND state='sending' ''', (body.state,body.message_id,body.state,body.bot_id,body.candidate_id))
    return {'ok':True}


def startup(bot):
    with storage.connect() as conn:
        conn.execute('UPDATE qq_group_state SET generation=generation+1 WHERE bot_id=%s',(bot,))
        conn.execute("UPDATE qq_group_candidate SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'discarded' END,error='进程重启不补发旧稿' WHERE bot_id=%s AND state IN ('pending','reviewing','sending')",(bot,))
    return {'ok':True}


def direct(bot,group,started):
    if not group:
        return
    with storage.connect() as conn:
        ensure(conn,bot,group)
        if started:
            invalidate(conn,bot,group,'直接互动优先')
        conn.execute('UPDATE qq_group_state SET direct_inflight=GREATEST(0,direct_inflight+%s) WHERE bot_id=%s AND group_id=%s',
                     (1 if started else -1,bot,group))


def public_evidence_ids(ids, group, bot, user):
    if not ids:
        return set()
    with storage.connect() as conn:
        return {r['message_id'] for r in conn.execute('''SELECT message_id FROM qq_group_evidence
            WHERE message_id=ANY(%s) AND bot_id=%s AND group_id=%s AND user_id=%s AND NOT recalled''',(ids,bot,group,user))}


def public_profiles(bot, group, user_ids=None):
    """仅返回本群有仍有效证据的观察；不返回全局相处感受、分值。"""
    with storage.connect() as conn:
        proofs=conn.execute('SELECT * FROM qq_group_evidence WHERE bot_id=%s AND group_id=%s AND NOT recalled',(bot,group)).fetchall()
        allowed={r['message_id']:r['user_id'] for r in proofs}
        participants={r['user_id'] for r in conn.execute('SELECT DISTINCT user_id FROM qq_group_message WHERE bot_id=%s AND group_id=%s AND NOT is_bot AND NOT recalled',(bot,group))}
        subjects=['qq:'+u for u in (user_ids if user_ids is not None else participants)]
        portraits=conn.execute('SELECT subject_key,profile FROM user_profile WHERE character_id=1 AND subject_key=ANY(%s)',(subjects,)).fetchall()
        result=[]
        for p in portraits:
            uid=p['subject_key'][3:]
            for item in p['profile']['observations']:
                if all(e.get('source_kind')=='group' and allowed.get(e['message_id'])==uid for e in item['evidence']):
                    result.append(dict(user_id=uid,category=item['category'],text=item['text']))
        # 完整问答证据也必须确实来自本群。
        for p in portraits:
            uid=p['subject_key'][3:]
            ids=[e['message_id'] for o in p['profile']['observations'] for e in o['evidence'] if e.get('source_kind','message')=='message']
            permitted={r['id'] for r in conn.execute('''SELECT m.id FROM message m JOIN qq_session s ON s.conversation_id=m.conversation_id
                WHERE s.bot_id=%s AND s.group_id=%s AND s.user_id=%s AND m.role='user' AND m.id=ANY(%s)''',(bot,group,uid,ids))}
            for item in p['profile']['observations']:
                if all((allowed.get(e['message_id'])==uid if e.get('source_kind')=='group' else e['message_id'] in permitted)
                       for e in item['evidence']):
                    value=dict(user_id=uid,category=item['category'],text=item['text'])
                    if value not in result:
                        result.append(value)
        return result


def cleanup():
    with storage.connect() as conn:
        # 留下不含原文的去重编号与指纹；未达门槛的材料也不能无限保存。
        conn.execute("UPDATE qq_group_message SET content=NULL,plain_text=NULL,profile_eligible=false,nickname='' WHERE received_at<now()-interval '7 days' AND content IS NOT NULL")
        conn.execute("UPDATE qq_group_candidate SET reply='',state=CASE WHEN state IN ('pending','reviewing') THEN 'discarded' ELSE state END WHERE created_at<now()-interval '7 days' AND reply<>''")
        conn.execute("UPDATE qq_group_batch SET decision=jsonb_build_object('summary',decision->'summary','speak',decision->'speak') WHERE created_at<now()-interval '7 days' AND decision ? 'focus'")


def recall(body):
    changed=set()
    with storage.connect() as conn:
        ensure(conn,body.bot_id,body.group_id)
        row=conn.execute('''UPDATE qq_group_message SET recalled=true,content=NULL,plain_text=NULL,profile_eligible=false
            WHERE bot_id=%s AND group_id=%s AND message_id=%s RETURNING id''',(body.bot_id,body.group_id,body.message_id)).fetchone()
        invalidate(conn,body.bot_id,body.group_id,'群消息已撤回')
        # 摘要是有损资料，无法可靠删除单句影响；清空重建比保留撤回信息更稳妥。
        conn.execute("UPDATE qq_group_state SET summary='' WHERE bot_id=%s AND group_id=%s",(body.bot_id,body.group_id))
        conn.execute('UPDATE qq_group_batch SET decision=NULL WHERE bot_id=%s AND group_id=%s',(body.bot_id,body.group_id))
        if row is None:
            # 撤回通知可能比原事件先到，保存不含原文的墓碑防止随后重放。
            conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,sent_at,is_bot,recalled)
                VALUES(%s,%s,%s,%s,now(),true,true) ON CONFLICT DO NOTHING''',
                (body.bot_id,body.group_id,body.message_id,body.bot_id))
            return changed
        proof_id=row['id']
        conn.execute('UPDATE qq_group_evidence SET recalled=true,quote=\'\' WHERE message_id=%s',(proof_id,))
        subject_rows=conn.execute('SELECT * FROM user_profile WHERE subject_key LIKE \'qq:%%\' AND character_id=1 FOR UPDATE').fetchall()
        def redact(profile):
            return {**profile,'observations':[o for o in profile['observations'] if not any(
                e.get('source_kind')=='group' and e['message_id']==proof_id for e in o['evidence'])]}
        for p in subject_rows:
            portrait=redact(p['profile'])
            if portrait==p['profile']:
                continue
            changed.add(p['subject_key'])
            conn.execute('UPDATE user_profile SET profile=%s,revision=revision+1,updated_at=now() WHERE subject_key=%s AND character_id=1', (Jsonb(portrait),p['subject_key']))
            conn.execute('''INSERT INTO user_profile_history(subject_key,character_id,revision,through_turn_id,source_message_ids,
                profile,old_affinity,delta,new_affinity,reason,evidence,update_kind) VALUES(%s,1,%s,%s,'{}',%s,%s,0,%s,'撤回旁听证据','[]','recall')''',
                (p['subject_key'],p['revision']+1,p['through_turn_id'],Jsonb(portrait),p['affinity'],p['affinity']))
        # 历史与导出同样不能继续暴露已经撤回的原话。
        for h in conn.execute('SELECT subject_key,revision,profile,evidence FROM user_profile_history WHERE character_id=1 AND subject_key LIKE \'qq:%%\'').fetchall():
            profile=redact(h['profile'])
            evidence=[e for e in h['evidence'] if not (e.get('source_kind')=='group' and e['message_id']==proof_id)]
            if profile!=h['profile'] or evidence!=h['evidence']:
                changed.add(h['subject_key'])
                conn.execute('UPDATE user_profile_history SET profile=%s,evidence=%s WHERE subject_key=%s AND character_id=1 AND revision=%s',
                             (Jsonb(profile),Jsonb(evidence),h['subject_key'],h['revision']))
    return changed
