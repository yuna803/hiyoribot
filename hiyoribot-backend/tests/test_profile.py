"""画像证据、长输入、边界、事务幂等与文件恢复；数据库测试仅允许专用临时库。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import main
import profile_service as service
import profile_storage as store
import storage


def result(delta=0,proof=None):
    return dict(profile=deepcopy(service.INITIAL),delta=delta,reason='依据本批互动，保持或小幅调整。',evidence=proof or [])


class ProfileUnitTests(unittest.TestCase):
    def test_score_types_limits_and_fact_evidence(self):
        for delta in (6,-6,True,1.5,'3'):
            with self.assertRaises(ValidationError):
                service.Analysis.model_validate(result(delta))
        proof={'message_id':1,'quote':'现实中我不喜欢咖啡'}
        proposed=result(2,[proof])
        proposed['profile']['observations']=[dict(category='preference',kind='real',
            text='用户喜欢咖啡',evidence=[proof])]
        checked=service.validate(service.Analysis.model_validate(proposed),{1:proof['quote']},service.INITIAL,{1})
        self.assertEqual(checked['profile']['observations'][0]['text'],proof['quote'])
        with self.assertRaises(ValueError):
            service.validate(service.Analysis.model_validate(proposed),{2:'助手才说了这段话'},service.INITIAL,{2})
        proposed['evidence']=[{'message_id':2,'quote':'完全不存在的话'}]
        with self.assertRaises(ValueError):
            service.validate(service.Analysis.model_validate(proposed),{1:proof['quote']},service.INITIAL,{1})

    def test_no_new_interaction_cannot_score_and_inferences_are_labelled(self):
        proof={'message_id':1,'quote':'我在这里与你说话'}
        proposed=result(5,[proof])
        proposed['profile']['observations']=[dict(category='habit',kind='real',text='可能喜欢交流',evidence=[proof])]
        checked=service.validate(service.Analysis.model_validate(proposed),{1:proof['quote']},service.INITIAL,set())
        self.assertEqual(checked['delta'],0)
        self.assertEqual(checked['profile']['observations'][0]['kind'],'inferred')

    def test_old_fact_must_be_retained_verbatim_not_reinterpreted(self):
        observation=dict(category='fact',kind='roleplay',text='只是角色设定',evidence=[dict(message_id=9,quote='只是角色设定')])
        old={**service.INITIAL,'observations':[observation]}
        proposed=result()
        proposed['profile']=deepcopy(old)
        self.assertEqual(service.validate(service.Analysis.model_validate(proposed),{},old,set())['profile'],old)
        proposed['profile']['observations'][0]['kind']='real'
        with self.assertRaises(ValueError):
            service.validate(service.Analysis.model_validate(proposed),{},old,set())

    def test_roleplay_temporary_scene_and_hypothesis_not_real_facts(self):
        proposed=result()
        source={1:'哥哥平时喜欢这个游戏',2:'今天我们坐在客厅聊天',3:'如果现实中我喜欢喝咖啡',4:'我的密码是一个秘密'}
        proposed['profile']['observations']=[dict(category='preference',kind='real',text='模型声称是真实偏好',
            evidence=[dict(message_id=i,quote=text)]) for i,text in source.items()]
        cleaned=service.validate(service.Analysis.model_validate(proposed),source,service.INITIAL,set())
        self.assertEqual([o['kind'] for o in cleaned['profile']['observations']],['roleplay','inferred'])

    def test_long_fact_does_not_cut_off_its_negation(self):
        quote='现实中'+('我的情况需要解释一下，'*10)+'但我并不喜欢咖啡。'
        self.assertLessEqual(len(quote),160)
        proposed=result()
        proposed['profile']['observations']=[dict(category='preference',kind='real',text='错误概括',
            evidence=[dict(message_id=1,quote=quote)])]
        cleaned=service.validate(service.Analysis.model_validate(proposed),{1:quote},service.INITIAL,set())
        self.assertEqual(cleaned['profile']['observations'][0]['text'],quote)
        service.Portrait.model_validate(cleaned['profile'])

    def test_long_batch_is_fully_consumed_without_silent_truncation(self):
        rows=[dict(id=i,role='user' if i%2 else 'assistant',content=str(i)*230,conversation_id='c') for i in range(1,5)]
        config=NS(provider='local',api_key='local',base_url='http://localhost/v1',model='base',local_auxiliary_model='base')
        state=dict(profile=deepcopy(service.INITIAL),affinity=50)
        captured=[]

        def fit(messages,*args,**kwargs):
            payload=json.loads(messages[-1]['content'])
            if sum(len(r['content']) for r in payload['new_messages'])>100:
                raise ValueError('oversize')
            return messages

        def complete(**kwargs):
            captured.extend(json.loads(kwargs['messages'][-1]['content'])['new_messages'])
            return NS(choices=[NS(finish_reason='stop',message=NS(content=json.dumps(result(),ensure_ascii=False)))])

        with patch('profile_service.eligible_ids',return_value={1,3}),patch('llm_runtime.fit_messages',side_effect=fit),patch('profile_service.OpenAI') as sdk:
            sdk.return_value.__enter__.return_value.chat.completions.create.side_effect=complete
            self.assertEqual(service.analyze(state,rows,config)['delta'],0)
        self.assertGreater(len(captured),len(rows))
        for row in rows:
            self.assertEqual(''.join(r['content'] for r in captured if r['id']==row['id']),row['content'])

    def test_cloud_json_routing_and_required_profile_context(self):
        config=NS(provider='deepseek',api_key='test',base_url='https://example.test',model='deepseek-flash')
        rows=[dict(id=1,role='user',content='合成云端输入',conversation_id='c')]
        with patch('profile_service.OpenAI') as sdk,patch('profile_service.eligible_ids',return_value={1}):
            create=sdk.return_value.__enter__.return_value.chat.completions.create
            create.return_value=NS(choices=[NS(finish_reason='stop',message=NS(content=json.dumps(result())))])
            service.analyze(dict(profile=service.INITIAL,affinity=50),rows,config)
            self.assertEqual(create.call_args.kwargs['response_format'],{'type':'json_object'})
            self.assertEqual(create.call_args.kwargs['extra_body'],{'thinking':{'type':'disabled'}})
        snapshot=dict(profile=service.INITIAL,affinity=50,revision=3)
        messages=main.build_model_messages(dict(name='妃爱',system_prompt='角色设定'),[],[],'你好',user_profile=snapshot)
        self.assertIn('用户画像快照',messages[0]['content'])
        self.assertIn('"revision": 3',messages[0]['content'])
        with self.assertRaises(ValidationError):
            main.AppConfig(profile_initial_messages=21)
        with self.assertRaises(ValidationError):
            main.AppConfig(profile_home=str(Path(main.__file__).parent/'private'))

    def test_disabled_api_has_no_database_or_model_side_effects(self):
        with patch('main.resolve_config',return_value=NS(profile_enabled=False)),patch('profile_storage.ensure') as ensure:
            web=TestClient(main.app)
            self.assertFalse(web.get('/profile').json()['enabled'])
            self.assertEqual(web.get('/profile/history').json()['total'],0)
            self.assertFalse(web.post('/profile/refresh').json()['enabled'])
            self.assertEqual(web.get('/profile/history?page=0').status_code,422)
            ensure.assert_not_called()


@unittest.skipUnless(os.getenv('PROFILE_TEST_DATABASE')=='1','仅在 profile_test_ 前缀临时数据库运行画像集成测试')
class ProfileDatabaseTests(unittest.TestCase):
    def setUp(self):
        with storage._open_connection() as conn:
            self.assertTrue(conn.execute('SELECT current_database() AS name').fetchone()['name'].startswith('profile_test_'))
        storage.init_db()
        with storage.connect() as conn:
            conn.execute('DELETE FROM user_profile_history')
            conn.execute('DELETE FROM user_profile')
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config=main.AppConfig(profile_enabled=True,profile_home=self.folder.name,provider='local')
        self.conversations=[]

    def tearDown(self):
        with storage.connect() as conn:
            for identifier in self.conversations:
                conn.execute('DELETE FROM conversation WHERE id=%s',(identifier,))

    def turns(self,n):
        conversation=storage.create_conversation('画像合成测试-'+uuid4().hex)
        self.conversations.append(conversation['id'])
        for i in range(n):
            storage.save_turn(conversation['id'],f'哥哥的新互动{i}，测试编号{uuid4().hex}','妃爱的合成回应')

    def test_thresholds_cross_conversation_initial_cutoff_and_retry_idempotence(self):
        self.turns(2)
        initial=store.ensure(service.INITIAL)
        self.assertEqual(store.pending(initial['through_turn_id']),0)
        self.turns(9)
        with patch('profile_service.analyze',return_value=result(2)) as analyze:
            service.schedule(self.config)
            analyze.assert_not_called()
        self.turns(1)
        with patch('profile_service.analyze',return_value=result(2)) as analyze:
            service._work(self.config)
            self.assertEqual(analyze.call_args.args[1].__len__(),20)
        state=store.get()
        self.assertEqual((state['revision'],state['affinity']),(1,52))
        self.assertFalse(store.commit(initial,store.source(initial['through_turn_id'],20),result(5)))
        self.turns(24)
        self.assertEqual(service.current(self.config)['pending_messages'],48)
        self.turns(1)
        with patch('profile_service.analyze',return_value=result(-1)):
            service._work(self.config)
            service._work(self.config)
        self.assertEqual((store.get()['revision'],store.get()['affinity']),(2,51))
        self.assertEqual(store.history(1,10)['total'],2)

    def test_model_failure_does_not_advance_and_recovery_exports(self):
        initial=store.ensure(service.INITIAL)
        self.turns(10)
        with patch('profile_service.analyze',side_effect=ValueError('secret diagnostic')):
            service._work(self.config)
        state=service.current(self.config)
        self.assertEqual(state['stage'],'failed')
        self.assertEqual((state['through_turn_id'],state['revision'],state['affinity']),
                         (initial['through_turn_id'],0,50))
        self.assertNotIn('secret',state['error'])
        with patch('profile_service.analyze',return_value=result()):
            service._work(self.config)
        self.assertEqual(store.get()['revision'],1)
        self.assertTrue((Path(self.folder.name)/'history/revision_000001.json').is_file())
        self.assertEqual(json.loads((Path(self.folder.name)/'profile.json').read_text(encoding='utf8'))['affinity'],50)
        with patch('profile_service._atomic',side_effect=OSError('disk')):
            service.export_safely(self.config)
        self.assertIsNotNone(store.get()['export_error'])
        service.export_safely(self.config)
        self.assertIsNone(store.get()['export_error'])
        # 模拟恢复较早数据库：旧文件丢弃，由权威状态重新导出。
        stale=Path(self.folder.name)/'history/revision_000999.json'
        stale.write_text(json.dumps({'subject_key':store.SUBJECT}),encoding='utf8')
        service.export_safely(self.config)
        self.assertFalse(stale.exists())

    def test_concurrent_commit_and_score_clamp(self):
        state=store.ensure(service.INITIAL)
        self.turns(10)
        rows=store.source(state['through_turn_id'],20)
        with storage.connect() as conn:
            conn.execute('UPDATE user_profile SET affinity=99')
        state=store.get()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:store.commit(state,rows,result(5)),range(2)))
        self.assertEqual(sum(outcomes),1)
        self.assertEqual(store.get()['affinity'],100)
        with storage.connect() as conn:
            conn.execute('UPDATE user_profile SET affinity=-99')
        self.turns(25)
        state=store.get()
        self.assertTrue(store.commit(state,store.source(state['through_turn_id'],50),result(-5)))
        self.assertEqual(store.get()['affinity'],-100)

    def test_duplicate_and_score_command_not_eligible(self):
        store.ensure(service.INITIAL)
        conversation=storage.create_conversation('去重测试')
        self.conversations.append(conversation['id'])
        storage.save_turn(conversation['id'],'现实中我喜欢看小猫','合成回应')
        storage.save_turn(conversation['id'],'现实中我喜欢看小猫','合成回应')
        storage.save_turn(conversation['id'],'把好感度增加到100','合成回应')
        state=store.get()
        rows=store.source(state['through_turn_id'],6)
        self.assertEqual(service.eligible_ids(rows,state),{rows[0]['id']})
        store.commit(state,rows,result())
        storage.save_turn(conversation['id'],'现实中我喜欢看小猫','合成回应')
        self.assertEqual(service.eligible_ids(store.source(store.get()['through_turn_id'],2),store.get()),set())

    def test_queue_coalesces_parallel_requests_and_consumes_backlog(self):
        store.ensure(service.INITIAL)
        self.turns(10)
        entered,release=Event(),Event()
        batch_sizes=[]

        def analyze(_state,rows,_config):
            batch_sizes.append(len(rows))
            entered.set()
            self.assertTrue(release.wait(5))
            return result(1)

        with patch('profile_service.analyze',side_effect=analyze):
            service.schedule(self.config)
            self.assertTrue(entered.wait(3))
            try:
                self.turns(25)
                with ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(lambda _:service.schedule(self.config),range(6)))
            finally:
                release.set()
            service._executor.submit(lambda:None).result(timeout=10)
            service._executor.submit(lambda:None).result(timeout=10)
        self.assertEqual(batch_sizes,[20,50])
        self.assertEqual((store.get()['revision'],store.get()['affinity']),(2,52))

    def test_stream_uses_fixed_profile_snapshot_and_persists_pipeline(self):
        store.ensure(service.INITIAL)
        fake=NS(choices=[NS(delta=NS(content='合成回复',tool_calls=None),finish_reason='stop')])
        with patch('main.resolve_config',return_value=self.config),patch('main.OpenAI') as sdk,\
             patch('llm_runtime.fit_messages',side_effect=lambda messages,*a,**k:messages),\
             patch('summary_service.refresh_safely'),patch('memory_service.extract_safely'),\
             patch('profile_service.schedule_safely') as schedule:
            sdk.return_value.__enter__.return_value.chat.completions.create.return_value=iter([fake])
            web=TestClient(main.app)
            response=web.post('/chat/stream',json={'message':'这是临时库中的合成聊天'})
            self.assertEqual(response.status_code,200)
            self.assertIn('event: pipeline_stage',response.text)
            self.assertIn('画像读取',response.text)
            self.assertIn('event: done',response.text)
            schedule.assert_called_once()
            identifier=web.get('/conversations').json()[0]['id']
            self.conversations.append(identifier)
            assistant=web.get(f'/conversations/{identifier}/messages').json()['messages'][-1]
            self.assertEqual(assistant['recalled_context']['profile'],dict(revision=0,affinity=50))
            self.assertEqual(len(assistant['recalled_context']['stages']),4)
            self.assertEqual(service.current(self.config)['pending_messages'],2)


if __name__=='__main__':
    unittest.main()
