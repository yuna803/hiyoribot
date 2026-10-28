"""群旁听边界验证；合成资料只允许写入专用临时数据库。"""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

import agent_tools
import group_profile
import group_service as service
import group_storage as store
import main
import profile_service
import profile_storage
import storage
import user_scope

BOT='810001'
USER='820001'
OTHER='820002'
OWNER='820003'
GROUP='830001'
GROUP2='830002'


def event(n,group=GROUP,user=USER,**changes):
    return service.Ingest(**{**dict(bot_id=BOT,group_id=group,user_id=user,message_id=str(n),nickname='合成昵称',
        message=f'我在合成聊天里喜欢茶，编号{n}',text=f'我在合成聊天里喜欢茶，编号{n}',sent_at=int(time.time())),**changes})


class GroupUnitTests(unittest.TestCase):
    def test_long_fragment_covers_every_character_including_middle(self):
        row=dict(id=1,user_id=USER,nickname='测试',content='甲'*32000,is_bot=False)
        chunks=[r for f in service.GroupModel.fragments([row]) for r in f]
        self.assertEqual(''.join(c['content'] for c in chunks),row['content'])
        self.assertEqual([c['offset'] for c in chunks],list(range(0,32000,3000)))

    def test_invalid_passive_evidence_and_score_controls_are_rejected(self):
        sources={1:dict(plain_text='我喜欢喝无糖的茶')}
        result=group_profile.PassiveAnalysis(observations=[dict(category='preference',kind='real',text='茶',
            evidence=[dict(source_kind='group',message_id=1,quote='我喜欢喝无糖的茶')])])
        self.assertEqual(group_profile.validate(result,sources)[0]['text'],'我喜欢喝无糖的茶')
        result.observations[0].evidence[0].message_id=2
        with self.assertRaises(ValueError):
            group_profile.validate(result,sources)
        result.observations[0].evidence[0].message_id=1
        result.observations[0].evidence[0].source_kind='message'
        with self.assertRaises(ValueError):
            group_profile.validate(result,sources)


@unittest.skipUnless(os.getenv('PROFILE_TEST_DATABASE')=='1','仅在profile_test_临时库执行')
class GroupDatabaseTests(unittest.TestCase):
    def setUp(self):
        with storage._open_connection() as conn:
            self.assertTrue(conn.execute('SELECT current_database() AS n').fetchone()['n'].startswith('profile_test_'))
        storage.init_db()
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config=main.AppConfig(api_key='synthetic',qq_enabled=True,qq_bot_id=BOT,qq_owner_id=OWNER,
            profile_enabled=True,profile_home=str(Path(self.folder.name)/'web'),qq_profile_home=str(Path(self.folder.name)/'qq'))
        self.schedule=patch('group_service.schedule').start()
        self.addCleanup(patch.stopall)
        self.clean()

    def clean(self):
        with storage.connect() as conn:
            for table in ('qq_group_candidate','qq_group_batch','qq_group_evidence','qq_group_message','qq_group_state','qq_passive_profile'):
                conn.execute('DELETE FROM '+table+' WHERE bot_id=%s',(BOT,))
            for table in ('user_profile_history','user_profile'):
                conn.execute('DELETE FROM '+table+' WHERE subject_key=ANY(%s)',(['qq:'+USER,'qq:'+OTHER],))

    def tearDown(self):
        self.clean()

    def fill(self,start=1,count=50,group=GROUP,user=USER,**extra):
        return [service.ingest(event(n,group,user,**extra),self.config) for n in range(start,start+count)]

    def draft(self,group=GROUP):
        self.fill(group=group)
        old,rows,identifier=store.begin_batch(BOT,group)
        decision=dict(summary='合成公开话题',speak=True,reason='有相关话题',focus='喝茶',quote_id=rows[-1]['id'])
        self.assertTrue(store.finish_batch(old,rows,identifier,decision,[]))
        store.save_candidate(old,rows,identifier,'红茶还是绿茶？这个话题我也有点兴趣呢。',rows[-1]['message_id'])
        return store.candidates(BOT)['items'][-1]['id']

    def test_49_50_100_independent_groups_dedup_media_and_self(self):
        self.fill(count=49)
        self.assertIsNone(store.begin_batch(BOT,GROUP))
        self.schedule.assert_not_called()
        self.fill(start=50,count=1,message='[image]',text='')
        self.assertEqual(self.schedule.call_count,1)
        old,rows,batch=store.begin_batch(BOT,GROUP)
        self.assertEqual(len(rows),50)
        store.finish_batch(old,rows,batch,dict(summary='公开摘要',speak=False),[])
        self.fill(start=51,count=50)
        self.assertEqual(len(store.begin_batch(BOT,GROUP)[1]),50)
        self.fill(count=49,group=GROUP2)
        self.assertIsNone(store.begin_batch(BOT,GROUP2))
        self.assertFalse(service.ingest(event(100),self.config)['accepted'])
        self.assertFalse(service.ingest(event(500,user=BOT),self.config)['accepted'])
        self.assertEqual(service.ingest(event(50,GROUP2,message='[record]',text=''),self.config)['pending'],50)

    def test_owner_toggle_resets_counter_and_paused_still_invalidates_direct(self):
        self.fill(count=20)
        with self.assertRaises(HTTPException):
            store.toggle(service.Toggle(bot_id=BOT,group_id=GROUP,user_id=USER,enabled=False),OWNER)
        body=service.Toggle(bot_id=BOT,group_id=GROUP,user_id=OWNER,enabled=False)
        store.toggle(body,OWNER)
        self.assertFalse(service.ingest(event(21),self.config)['accepted'])
        self.assertTrue(service.ingest(event(22,direct=True),self.config)['accepted'])
        self.assertEqual(service.ingest(event(23,direct=True),self.config)['pending'],0)
        store.toggle(body.model_copy(update={'enabled':True}),OWNER)
        self.assertEqual(service.ingest(event(24),self.config)['pending'],1)

    def test_silence_processes_batch_without_candidate_or_user_turn(self):
        self.fill()
        with patch('group_service.observe',return_value=(dict(summary='无须插话',speak=False),[])),\
             patch('group_service.generate') as generate,patch('group_profile.update'):
            service.work(BOT,GROUP,self.config)
        generate.assert_not_called()
        self.assertEqual(store.candidates(BOT)['items'],[])
        self.assertGreater(store.state(BOT,GROUP)['through_id'],0)
        with storage.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM completed_turn WHERE subject_key=%s',('qq-group:'+BOT+':'+GROUP,)).fetchone()['n'],0)

    def test_candidate_one_shot_receipt_and_five_minute_cooldown(self):
        identifier=self.draft()
        claim=service.Claim(bot_id=BOT,candidate_id=identifier)
        self.assertTrue(service.claim(claim,self.config)['send'])
        self.assertFalse(service.claim(claim,self.config)['send'])
        store.receipt(service.Receipt(bot_id=BOT,candidate_id=identifier,state='uncertain'))
        with storage.connect() as conn:
            self.assertFalse(store.quota(conn,BOT,GROUP))
        self.assertFalse(service.claim(claim,self.config)['send'])

    def test_three_attempts_per_rolling_hour(self):
        for i in range(3):
            group=GROUP
            self.fill(start=1+i*50)
            old,rows,batch=store.begin_batch(BOT,group)
            store.finish_batch(old,rows,batch,dict(summary='测试',speak=True),[])
            store.save_candidate(old,rows,batch,'合成回复',None)
            identifier=store.candidates(BOT)['items'][-1]['id']
            self.assertTrue(service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config)['send'])
            with storage.connect() as conn:
                conn.execute("UPDATE qq_group_candidate SET attempted_at=now()-(%s * interval '1 minute') WHERE id=%s",(20-i*6,identifier))
        with storage.connect() as conn:
            self.assertFalse(store.quota(conn,BOT,GROUP))

    def test_freshness_review_only_once_and_second_new_message_discards(self):
        identifier=self.draft()
        self.fill(start=51,count=1)
        def review(*args):
            self.fill(start=52,count=1)
            return service.Review(send=True,reason='仍相关')
        with patch.object(service.GroupModel,'json',side_effect=review) as model:
            result=service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config)
        self.assertFalse(result['send'])
        self.assertEqual(model.call_count,1)

    def test_review_failure_and_rejection_do_not_send(self):
        identifier=self.draft()
        self.fill(start=51,count=1)
        with patch.object(service.GroupModel,'json',side_effect=RuntimeError('合成故障')):
            self.assertFalse(service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config)['send'])

    def test_parallel_claims_grant_only_one_send_permission(self):
        identifier=self.draft()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda _:service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config),range(2)))
        self.assertEqual(sum(r['send'] for r in result),1)

    def test_direct_started_during_review_invalidates_permission(self):
        identifier=self.draft()
        self.fill(start=51,count=1)
        def review(*args):
            store.direct(BOT,GROUP,True)
            return service.Review(send=True,reason='模型判断仍相关')
        with patch.object(service.GroupModel,'json',side_effect=review):
            self.assertFalse(service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config)['send'])
        store.direct(BOT,GROUP,False)

    def test_direct_interaction_and_restart_discard_pending_drafts(self):
        identifier=self.draft()
        self.fill(start=51,count=1,direct=True)
        self.assertFalse(service.claim(service.Claim(bot_id=BOT,candidate_id=identifier),self.config)['send'])
        identifier2=self.draft(GROUP2)
        store.startup(BOT)
        self.assertFalse(service.claim(service.Claim(bot_id=BOT,candidate_id=identifier2),self.config)['send'])

    def test_raw_expiry_removes_pending_material_but_preserves_cited_proof(self):
        self.fill(count=49)
        row=store.recent(BOT,GROUP)[0]
        with storage.connect() as conn:
            conn.execute('INSERT INTO qq_group_evidence(message_id,bot_id,group_id,user_id,quote) VALUES(%s,%s,%s,%s,%s)',
                         (row['id'],BOT,GROUP,USER,'我喜欢喝茶'))
            conn.execute("UPDATE qq_group_message SET received_at=now()-interval '8 days' WHERE bot_id=%s",(BOT,))
        store.cleanup()
        self.assertIsNone(store.begin_batch(BOT,GROUP))
        self.assertEqual(service.ingest(event(50),self.config)['pending'],1)
        self.assertEqual(store.public_evidence_ids([row['id']],GROUP,BOT,USER),{row['id']})

    def test_passive_10_25_identity_affinity_and_direct_threshold_are_independent(self):
        self.fill()
        old,rows,batch=store.begin_batch(BOT,GROUP)
        store.finish_batch(old,rows,batch,dict(summary='合成摘要',speak=False),[])
        calls=[]
        def analyze(_instructions,payload,schema,config):
            messages=payload['messages']
            calls.append(len(messages))
            r=messages[0]
            return schema(observations=[dict(category='preference',kind='real',text='喝茶',
                evidence=[dict(source_kind='group',message_id=r['id'],quote=r['content'][:14])])])
        with patch.object(service.GroupModel,'json',side_effect=analyze):
            group_profile.update(BOT,USER,self.config,service.GroupModel)
        self.assertEqual(calls,[10,25])
        with user_scope.activate('qq:'+USER):
            state=profile_storage.get()
            self.assertEqual(state['affinity'],50)
            self.assertEqual(state['direct_updates'],0)
            self.assertEqual(profile_service.threshold(state,self.config),20)
            self.assertEqual(state['profile']['identity'],f'QQ用户（{USER}）')
            self.assertEqual(profile_service.snapshot(user_scope.config_for(self.config).model_copy(update={'qq_group_id':GROUP2}))['profile']['observations'],[])
            self.assertTrue(profile_service.snapshot(user_scope.config_for(self.config).model_copy(update={'qq_group_id':GROUP}))['profile']['observations'])
        self.assertTrue(store.public_profiles(BOT,GROUP))
        self.assertEqual(store.public_profiles(BOT,GROUP2),[])

    def test_passive_failure_keeps_cursor_and_recall_removes_all_evidence(self):
        self.fill()
        old,rows,batch=store.begin_batch(BOT,GROUP)
        store.finish_batch(old,rows,batch,dict(summary='合成摘要',speak=False),[])
        with patch.object(service.GroupModel,'json',side_effect=ValueError('非法输出')):
            with self.assertRaises(ValueError):
                group_profile.update(BOT,USER,self.config,service.GroupModel)
        with storage.connect() as conn:
            self.assertEqual(conn.execute('SELECT through_id FROM qq_passive_profile WHERE bot_id=%s AND user_id=%s',(BOT,USER)).fetchone()['through_id'],0)
        def answer(_instructions,payload,schema,config):
            r=payload['messages'][0]
            return schema(observations=[dict(category='preference',kind='real',text='偏好',evidence=[dict(source_kind='group',message_id=r['id'],quote=r['content'][:14])])])
        with patch.object(service.GroupModel,'json',side_effect=answer):
            group_profile.update(BOT,USER,self.config,service.GroupModel)
        service.recall(service.Recall(bot_id=BOT,group_id=GROUP,message_id='1'),self.config)
        with user_scope.activate('qq:'+USER):
            current=profile_storage.get()
            self.assertEqual(current['affinity'],50)
            self.assertFalse(any(e['message_id']==rows[0]['id'] for o in current['profile']['observations'] for e in o['evidence']))
        self.assertEqual(store.state(BOT,GROUP)['summary'],'')
        self.assertNotIn(rows[0]['id'],store.public_evidence_ids([rows[0]['id']],GROUP,BOT,USER))

    def test_group_tools_do_not_search_personal_history_or_memory(self):
        self.fill(count=1)
        with user_scope.group(BOT,GROUP),patch('memory_service.recall') as private_memory,patch('storage.search_chat_history') as private_history:
            self.assertEqual(agent_tools.execute('search_user_memory','{"query":"秘密"}',uuid4(),'和泉妃爱'),{'public_observations':[]})
            self.assertTrue(agent_tools.execute('search_chat_history','{"query":"喜欢茶"}',uuid4(),'和泉妃爱')['messages'])
        private_memory.assert_not_called()
        private_history.assert_not_called()

    def test_model_failure_leaves_batch_cursor_and_no_candidate(self):
        self.fill()
        with patch('group_service.observe',side_effect=ValueError('合成错误')):
            service.work(BOT,GROUP,self.config)
        self.assertEqual(store.state(BOT,GROUP)['through_id'],0)
        self.assertEqual(store.state(BOT,GROUP)['stage'],'failed')
        self.assertEqual(store.candidates(BOT)['items'],[])

    def test_recall_before_ingest_tombstone_and_inflight_restart(self):
        store.recall(service.Recall(bot_id=BOT,group_id=GROUP,message_id='1'))
        self.assertFalse(service.ingest(event(1),self.config)['accepted'])
        self.fill(start=2)
        old,rows,batch=store.begin_batch(BOT,GROUP)
        store.startup(BOT)
        self.assertFalse(store.finish_batch(old,rows,batch,dict(summary='旧任务',speak=True),[]))
        store.save_candidate(old,rows,batch,'不能补发旧稿',None)
        self.assertEqual(store.candidates(BOT)['items'],[])


if __name__=='__main__':
    unittest.main()
