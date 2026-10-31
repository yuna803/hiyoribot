"""群消息账本与一次性发送许可。模型永远不能决定投递群号。"""
import hashlib
import re
import time
from uuid import uuid4

from fastapi import HTTPException

import storage
import followup_storage as follow
import media_service


def ensure(conn, bot, group):
    conn.execute('INSERT INTO qq_group_state(bot_id,group_id) VALUES(?,?) ON CONFLICT DO NOTHING', (bot,group))
    return conn.execute('SELECT * FROM qq_group_state WHERE bot_id=? AND group_id=?', (bot,group)).fetchone()


def invalidate(conn, bot, group, reason):
    conn.execute('UPDATE qq_group_state SET generation=generation+1 WHERE bot_id=? AND group_id=?', (bot,group))
    conn.execute("UPDATE qq_group_candidate SET state='discarded',error=? WHERE bot_id=? AND group_id=? AND state IN ('pending','reviewing')",
                 (reason,bot,group))


def pending(conn, state):
    return conn.execute('''SELECT count(*) AS n FROM qq_group_message WHERE bot_id=? AND group_id=?
        AND id>? AND NOT is_bot AND NOT recalled AND content IS NOT NULL''',
        (state['bot_id'],state['group_id'],state['through_id'])).fetchone()['n']


def ingest(body):
    with storage.connect(write=True) as conn:
        state=ensure(conn,body.bot_id,body.group_id)
        if body.user_id==body.bot_id:
            return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=False)
        old=conn.execute('SELECT id FROM qq_group_message WHERE bot_id=? AND group_id=? AND message_id=?',
                         (body.bot_id,body.group_id,body.message_id)).fetchone()
        if old or (not state['enabled'] and not body.direct):
            return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=False)
        # 暂停时只保留直接互动的失效信号，不继续累计旁听。
        if body.direct:
            invalidate(conn,body.bot_id,body.group_id,'直接互动优先')
        normalized=re.sub(r'[\W_]+','',body.text).casefold()
        digest=hashlib.sha256(normalized.encode()).hexdigest() if normalized else None
        duplicate=bool(digest and conn.execute('SELECT 1 FROM qq_group_message WHERE bot_id=? AND user_id=? AND text_hash=? LIMIT 1',
                                               (body.bot_id,body.user_id,digest)).fetchone())
        eligible=(state['enabled'] and not body.direct and not duplicate and len(normalized)>=4
                  and not body.text.lstrip().startswith(('/','！','!')))
        row_id=conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,nickname,content,plain_text,
            quoted_message_id,sent_at,direct,profile_eligible,text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (body.bot_id,body.group_id,body.message_id,body.user_id,body.nickname,
             body.message if state['enabled'] else None,body.text if state['enabled'] else None,
             body.quoted_message_id,body.sent_at,body.direct,eligible,digest)).lastrowid
        reserved=follow.reserve(conn,body,row_id,eligible) if state['enabled'] else {}
        if eligible:
            conn.execute('INSERT INTO qq_passive_profile(bot_id,user_id) VALUES(?,?) ON CONFLICT DO NOTHING', (body.bot_id,body.user_id))
        return dict(enabled=state['enabled'],pending=pending(conn,state),accepted=True,**reserved)


def toggle(body, owner):
    if body.user_id!=owner:
        raise HTTPException(403,'只有绑定本人可以修改旁听开关')
    with storage.connect(write=True) as conn:
        state=ensure(conn,body.bot_id,body.group_id)
        if state['enabled']!=body.enabled:
            follow.close(conn,body.bot_id,body.group_id,'旁听开关变化')
            invalidate(conn,body.bot_id,body.group_id,'旁听开关变更')
            last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=? AND group_id=?',
                              (body.bot_id,body.group_id)).fetchone()['id']
            conn.execute('UPDATE qq_group_state SET enabled=?,through_id=?,stage=\'idle\',error=NULL WHERE bot_id=? AND group_id=?',
                         (body.enabled,last,body.bot_id,body.group_id))
        return dict(enabled=body.enabled,pending=0 if state['enabled']!=body.enabled else pending(conn,state))


def bot_message(body):
    with storage.connect(write=True) as conn:
        ensure(conn,body.bot_id,body.group_id)
        conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,content,plain_text,sent_at,is_bot)
            VALUES(?,?,?,?,?,?,unixepoch(),true) ON CONFLICT DO NOTHING''',
            (body.bot_id,body.group_id,body.message_id,body.bot_id,body.message,body.message))
        if getattr(body,'opens_window',False):
            follow.open_window(conn,body)
        window=follow.window(conn,body.bot_id,body.group_id)
    return {'ok':True,'window_version':window['version']}


def rows(bot, group, *, after=0, limit=50, users_only=False):
    with storage.connect() as conn:
        return conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=? AND group_id=? AND id>?
            AND content IS NOT NULL AND NOT recalled AND (NOT ? OR NOT is_bot) ORDER BY id LIMIT ?''',
            (bot,group,after,users_only,limit)).fetchall()


def recent(bot, group, limit=20):
    with storage.connect() as conn:
        return list(reversed(conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=? AND group_id=?
            AND content IS NOT NULL AND NOT recalled ORDER BY id DESC LIMIT ?''',(bot,group,limit)).fetchall()))


def bot_recent(bot,group):
    with storage.connect() as conn:
        return list(reversed(conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=? AND group_id=?
            AND is_bot AND NOT recalled AND content IS NOT NULL AND received_at>unixepoch()-3600
            ORDER BY id DESC LIMIT 5''',(bot,group)).fetchall()))


def state(bot, group):
    with storage.connect() as conn:
        return conn.execute('SELECT * FROM qq_group_state WHERE bot_id=? AND group_id=?',(bot,group)).fetchone()


def quota(conn, bot, group):
    # uncertain 与失败尝试也占冷却，防止网络抖动造成刷屏；发送许可不重试。
    active=conn.execute('SELECT direct_inflight FROM qq_group_state WHERE bot_id=? AND group_id=?',(bot,group)).fetchone()
    if (active and active['direct_inflight']) or follow.active(conn,bot,group):
        return False
    row=conn.execute('''SELECT count(*) AS n, max(attempted_at) AS last FROM qq_group_candidate
        WHERE bot_id=? AND group_id=? AND attempted_at>unixepoch()-3600 ''',(bot,group)).fetchone()
    return row['n']<3 and (row['last'] is None or (time.time()-row['last'])>=300)


def begin_batch(bot, group):
    with storage.connect(write=True) as conn:
        current=ensure(conn,bot,group)
        if not current['enabled']:
            return None
        batch=conn.execute('''SELECT * FROM qq_group_message WHERE bot_id=? AND group_id=? AND id>?
            AND NOT is_bot AND NOT recalled AND content IS NOT NULL ORDER BY id LIMIT 50''',
            (bot,group,current['through_id'])).fetchall()
        if len(batch)<50:
            conn.execute("UPDATE qq_group_state SET stage='idle' WHERE bot_id=? AND group_id=?",(bot,group))
            return None
        current['suppress_speak']=bool(current['direct_inflight'] or follow.active(conn,bot,group))
        identifier=uuid4()
        conn.execute('''INSERT INTO qq_group_batch(id,bot_id,group_id,first_id,last_id,source_ids)
            VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id,group_id,last_id)
            DO UPDATE SET state='running',error=NULL,coverage='[]',source_ids=EXCLUDED.source_ids RETURNING id''',
            (identifier,bot,group,batch[0]['id'],batch[-1]['id'],storage.json_dump([r['id'] for r in batch])))
        saved=conn.execute('SELECT id FROM qq_group_batch WHERE bot_id=? AND group_id=? AND last_id=?',
                           (bot,group,batch[-1]['id'])).fetchone()['id']
        conn.execute("UPDATE qq_group_state SET stage='running',error=NULL WHERE bot_id=? AND group_id=?",(bot,group))
        return current,batch,saved


def finish_batch(old, batch, identifier, decision, coverage):
    with storage.connect(write=True) as conn:
        now=ensure(conn,old['bot_id'],old['group_id'])
        valid=now['enabled'] and now['generation']==old['generation'] and now['through_id']==old['through_id']
        conn.execute('UPDATE qq_group_batch SET state=?,decision=?,coverage=? WHERE id=?',
                     ('completed' if valid else 'superseded',storage.json_dump(decision),storage.json_dump(coverage),identifier))
        # 新直接互动只作废插话，不让已整理的50条被反复付费整理；开关重置游标则不覆盖。
        alive=conn.execute('SELECT count(*) AS n FROM qq_group_message WHERE id IN (SELECT value FROM json_each(?)) AND NOT recalled AND content IS NOT NULL',
                           (storage.json_dump([r['id'] for r in batch]),)).fetchone()['n']==len(batch)
        if now['enabled'] and now['through_id']==old['through_id'] and alive:
            conn.execute('UPDATE qq_group_state SET through_id=?,summary=?,summary_revision=summary_revision+1,stage=\'idle\',updated_at=unixepoch() WHERE bot_id=? AND group_id=?',
                         (batch[-1]['id'],decision['summary'],old['bot_id'],old['group_id']))
            import summary_service
            summary_service.archive(conn,f"group:{old['bot_id']}:{old['group_id']}",batch,decision,coverage)
        return valid and alive and not old.get('suppress_speak',False) and decision['speak'] and quota(conn,old['bot_id'],old['group_id'])


def save_candidate(old, batch, identifier, reply, quote,*,attachments=None):
    with storage.connect(write=True) as conn:
        now=ensure(conn,old['bot_id'],old['group_id'])
        if not now['enabled'] or now['generation']!=old['generation'] or not quota(conn,old['bot_id'],old['group_id']):
            return
        # 保存生成开始时的末条ID，普通新消息将触发发送前复核。
        observed=batch[-1]['id']
        inserted=conn.execute('''INSERT INTO qq_group_candidate(id,batch_id,bot_id,group_id,generation,observed_id,reply,
            quote_message_id,source_ids) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO NOTHING RETURNING id''',
            (uuid4(),identifier,old['bot_id'],old['group_id'],old['generation'],observed,reply,quote,storage.json_dump([r['id'] for r in batch]))).fetchone()
        if inserted:
            media_service.register_delivery(conn,'candidate:'+inserted['id'],old['bot_id'],old['bot_id'],old['group_id'],attachments or [])


def fail(bot, group, error):
    with storage.connect(write=True) as conn:
        conn.execute("UPDATE qq_group_state SET stage='failed',error=? WHERE bot_id=? AND group_id=?",(error,bot,group))
        conn.execute("UPDATE qq_group_batch SET state='failed',error=? WHERE bot_id=? AND group_id=? AND state='running'",(error,bot,group))


def candidates(bot):
    with storage.connect(write=True) as conn:
        conn.execute("UPDATE qq_group_candidate SET state='discarded',error='候选已过期' WHERE bot_id=? AND state IN ('pending','reviewing') AND created_at<unixepoch()-600",(bot,))
        return {'items':conn.execute("SELECT id,group_id FROM qq_group_candidate WHERE bot_id=? AND state='pending' ORDER BY created_at LIMIT 20",(bot,)).fetchall()}


def prepare_claim(bot, identifier):
    with storage.connect(write=True) as conn:
        candidate=conn.execute('SELECT * FROM qq_group_candidate WHERE bot_id=? AND id=?',(bot,identifier)).fetchone()
        if candidate is None:
            return None
        current=ensure(conn,bot,candidate['group_id'])
        saved=conn.execute('''UPDATE qq_group_candidate SET state='reviewing' WHERE id=? AND state='pending'
            AND created_at>unixepoch()-600 RETURNING *''',(identifier,)).fetchone()
        if not saved:
            return None
        if not current['enabled'] or current['generation']!=candidate['generation'] or not quota(conn,bot,candidate['group_id']):
            conn.execute("UPDATE qq_group_candidate SET state='discarded',error='失效或频率限制' WHERE id=?",(identifier,))
            return None
        last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=? AND group_id=? AND NOT recalled',
                          (bot,candidate['group_id'])).fetchone()['id']
        return candidate,last


def permit(candidate, observed, approved):
    with storage.connect(write=True) as conn:
        now=ensure(conn,candidate['bot_id'],candidate['group_id'])
        last=conn.execute('SELECT COALESCE(max(id),0) AS id FROM qq_group_message WHERE bot_id=? AND group_id=? AND NOT recalled',
                          (candidate['bot_id'],candidate['group_id'])).fetchone()['id']
        valid=approved and now['enabled'] and now['generation']==candidate['generation'] and last==observed and quota(conn,candidate['bot_id'],candidate['group_id'])
        row=conn.execute('''UPDATE qq_group_candidate SET state=?,reviewed=?,attempted_at=CASE WHEN ? THEN unixepoch() ELSE NULL END,
            error=? WHERE id=? AND state='reviewing' RETURNING id''',
            ('sending' if valid else 'discarded',observed!=candidate['observed_id'],valid,
             None if valid else '新消息、直接互动或复核否决',candidate['id'])).fetchone()
        if not row or not valid:
            return {'send':False}
        delivery_id='candidate:'+candidate['id']
        # 同一连接内查询，不在持写锁时另开连接。
        asset=conn.execute("SELECT a.* FROM media_delivery d JOIN media_asset a ON a.id=d.asset_id WHERE d.id=? AND a.meme_state='approved'",(delivery_id,)).fetchone()
        attachments=[media_service.attachment(asset)] if asset else []
        if not candidate['reply'].strip() and not attachments:
            conn.execute("UPDATE qq_group_candidate SET state='discarded',error='表情不可用' WHERE id=?",(candidate['id'],))
            return {'send':False}
        result=dict(send=True,group_id=candidate['group_id'],reply=candidate['reply'],quote_message_id=candidate['quote_message_id'])
        if attachments:
            result.update(attachments=attachments,delivery_id=delivery_id)
        return result


def receipt(body):
    with storage.connect(write=True) as conn:
        conn.execute('''UPDATE qq_group_candidate SET state=?,message_id=?,sent_at=CASE WHEN ?='sent' THEN unixepoch() ELSE NULL END
            WHERE bot_id=? AND id=? AND state='sending' ''', (body.state,body.message_id,body.state,body.bot_id,body.candidate_id))
    return {'ok':True}


def startup(bot):
    with storage.connect(write=True) as conn:
        follow.restart(conn,bot)
        conn.execute("UPDATE media_delivery SET state='uncertain' WHERE bot_id=? AND state='pending'",(bot,))
        conn.execute('UPDATE qq_group_state SET generation=generation+1 WHERE bot_id=?',(bot,))
        conn.execute("UPDATE qq_group_candidate SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'discarded' END,error='进程重启不补发旧稿' WHERE bot_id=? AND state IN ('pending','reviewing','sending')",(bot,))
    return {'ok':True}


def direct(bot,group,started,*,preempt=True):
    if not group:
        return
    with storage.connect(write=True) as conn:
        ensure(conn,bot,group)
        if started and preempt:
            invalidate(conn,bot,group,'直接互动优先')
            follow.close(conn,bot,group,'直接互动优先')
        conn.execute('UPDATE qq_group_state SET direct_inflight=max(0,direct_inflight+?) WHERE bot_id=? AND group_id=?',
                     (1 if started else -1,bot,group))


def public_evidence_ids(ids, group, bot, user):
    if not ids:
        return set()
    with storage.connect() as conn:
        return {r['message_id'] for r in conn.execute('''SELECT message_id FROM qq_group_evidence
            WHERE message_id IN (SELECT value FROM json_each(?)) AND bot_id=? AND group_id=? AND user_id=? AND NOT recalled''',(storage.json_dump(ids),bot,group,user))}


def public_profiles(bot, group, user_ids=None):
    """仅返回本群有仍有效证据的观察；不返回全局相处感受、分值。"""
    with storage.connect() as conn:
        proofs=conn.execute('SELECT * FROM qq_group_evidence WHERE bot_id=? AND group_id=? AND NOT recalled',(bot,group)).fetchall()
        allowed={r['message_id']:r['user_id'] for r in proofs}
        participants={r['user_id'] for r in conn.execute('SELECT DISTINCT user_id FROM qq_group_message WHERE bot_id=? AND group_id=? AND NOT is_bot AND NOT recalled',(bot,group))}
        subjects=['qq:'+u for u in (user_ids if user_ids is not None else participants)]
        portraits=conn.execute('SELECT subject_key,profile FROM user_profile WHERE character_id=1 AND subject_key IN (SELECT value FROM json_each(?))',(storage.json_dump(subjects),)).fetchall()
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
                WHERE s.bot_id=? AND s.group_id=? AND s.user_id=? AND m.role='user' AND m.id IN (SELECT value FROM json_each(?))''',(bot,group,uid,storage.json_dump(ids)))}
            for item in p['profile']['observations']:
                if all((allowed.get(e['message_id'])==uid if e.get('source_kind')=='group' else e['message_id'] in permitted)
                       for e in item['evidence']):
                    value=dict(user_id=uid,category=item['category'],text=item['text'])
                    if value not in result:
                        result.append(value)
        return result


def cleanup():
    with storage.connect(write=True) as conn:
        # 回收失联任务，不按时间关闭用户选择的5条窗口。
        for job in conn.execute("SELECT message_row_id FROM qq_followup_job WHERE state IN ('pending','processing','prepared') AND created_at<unixepoch()-600").fetchall():
            follow.settle(conn,job['message_row_id'],'discarded','任务中断，不补发')
        conn.execute("UPDATE qq_followup_job SET draft=NULL,reason='' WHERE created_at<unixepoch()-604800")
        # 留下不含原文的去重编号与指纹；未达门槛的材料也不能无限保存。
        conn.execute("UPDATE qq_group_message SET content=NULL,plain_text=NULL,profile_eligible=false,nickname='' WHERE received_at<unixepoch()-604800 AND content IS NOT NULL")
        conn.execute("UPDATE qq_group_candidate SET reply='',state=CASE WHEN state IN ('pending','reviewing') THEN 'discarded' ELSE state END WHERE created_at<unixepoch()-604800 AND reply<>''")
        conn.execute("UPDATE qq_group_batch SET decision=json_object('summary',json_extract(decision,'$.summary'),'speak',json_extract(decision,'$.speak')) WHERE created_at<unixepoch()-604800 AND json_type(decision,'$.focus') IS NOT NULL")


def recall(body):
    changed=set()
    with storage.connect(write=True) as conn:
        ensure(conn,body.bot_id,body.group_id)
        follow.recall(conn,body)
        conn.execute('DELETE FROM media_source WHERE bot_id=? AND group_id=? AND (message_id=? OR reference_message_id=?)',
                     (body.bot_id,body.group_id,body.message_id,body.message_id))
        row=conn.execute('''UPDATE qq_group_message SET recalled=true,content=NULL,plain_text=NULL,profile_eligible=false
            WHERE bot_id=? AND group_id=? AND message_id=? RETURNING id''',(body.bot_id,body.group_id,body.message_id)).fetchone()
        invalidate(conn,body.bot_id,body.group_id,'群消息已撤回')
        # 摘要是有损资料，无法可靠删除单句影响；清空重建比保留撤回信息更稳妥。
        conn.execute("UPDATE qq_group_state SET summary='',summary_revision=summary_revision+1 WHERE bot_id=? AND group_id=?",(body.bot_id,body.group_id))
        conn.execute('UPDATE qq_group_batch SET decision=NULL WHERE bot_id=? AND group_id=?',(body.bot_id,body.group_id))
        conn.execute('DELETE FROM summary_batch WHERE scope_key=?',(f'group:{body.bot_id}:{body.group_id}',))
        if row is None:
            # 撤回通知可能比原事件先到，保存不含原文的墓碑防止随后重放。
            conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,sent_at,is_bot,recalled)
                VALUES(?,?,?,?,unixepoch(),true,true) ON CONFLICT DO NOTHING''',
                (body.bot_id,body.group_id,body.message_id,body.bot_id))
            return changed
        proof_id=row['id']
        conn.execute('UPDATE qq_group_evidence SET recalled=true,quote=\'\' WHERE message_id=?',(proof_id,))
        subject_rows=conn.execute('SELECT * FROM user_profile WHERE subject_key LIKE \'qq:%\' AND character_id=1').fetchall()
        def redact(profile):
            return {**profile,'observations':[o for o in profile['observations'] if not any(
                e.get('source_kind')=='group' and e['message_id']==proof_id for e in o['evidence'])]}
        for p in subject_rows:
            portrait=redact(p['profile'])
            if portrait==p['profile']:
                continue
            changed.add(p['subject_key'])
            conn.execute('UPDATE user_profile SET profile=?,revision=revision+1,updated_at=unixepoch() WHERE subject_key=? AND character_id=1', (storage.json_dump(portrait),p['subject_key']))
            conn.execute('''INSERT INTO user_profile_history(subject_key,character_id,revision,through_turn_id,source_message_ids,
                profile,old_affinity,delta,new_affinity,reason,evidence,update_kind) VALUES(?,1,?,?,'[]',?,?,0,?,'撤回旁听证据','[]','recall')''',
                (p['subject_key'],p['revision']+1,p['through_turn_id'],storage.json_dump(portrait),p['affinity'],p['affinity']))
        # 历史与导出同样不能继续暴露已经撤回的原话。
        for h in conn.execute('SELECT subject_key,revision,profile,evidence FROM user_profile_history WHERE character_id=1 AND subject_key LIKE \'qq:%\'').fetchall():
            profile=redact(h['profile'])
            evidence=[e for e in h['evidence'] if not (e.get('source_kind')=='group' and e['message_id']==proof_id)]
            if profile!=h['profile'] or evidence!=h['evidence']:
                changed.add(h['subject_key'])
                conn.execute('UPDATE user_profile_history SET profile=?,evidence=? WHERE subject_key=? AND character_id=1 AND revision=?',
                             (storage.json_dump(profile),storage.json_dump(evidence),h['subject_key'],h['revision']))
    return changed
