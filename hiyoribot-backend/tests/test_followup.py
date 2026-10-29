"""仅临时SQLite与合成事件，覆盖跟聊判定、一次性许可和旧画像游标。"""
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import followup_service as service
import followup_storage as follow
import group_profile
import group_service
import group_storage
import main
import qq_service
import storage

BOT,USER,OTHER,GROUP='810001','820001','820002','830001'


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)
        storage.configure(path/'test.sqlite')
        self.config=main.AppConfig(api_key='test',qq_enabled=True,qq_bot_id=BOT,qq_owner_id=USER,
            qq_backend_token='test-token',sqlite_path=str(path/'test.sqlite'),profile_enabled=False,
            profile_home=str(path/'profiles'),qq_profile_home=str(path/'profiles'))
        self.addCleanup(patch.stopall)
        patch('group_service.schedule').start()
        patch('profile_service.schedule_safely').start()
        patch('chat_service.direct_reply',return_value={'reply':'那就聊红茶吧？','agent_messages':[],
            'recalled_context':{}}).start()
        self.ask=patch('followup_service.llm_runtime.json_output',return_value=service.Decision(send=True,reason='回应她的问题')).start()

    def ingest(self,n,text='我觉得红茶就很好啊',user=USER,group=GROUP,**kw):
        return group_service.ingest(group_service.Ingest(bot_id=BOT,group_id=group,user_id=user,
            message_id=str(n),nickname='合成显示名',message=text,text=kw.pop('plain',text),sent_at=int(time.time()),**kw),self.config)

    def open(self,mid='-100',group=GROUP):
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=group,message_id=mid,
            message='你平时喜欢喝什么茶？',opens_window=True,segment_message_ids=[mid]))
        self.cool(group)

    def cool(self,group=GROUP):
        with storage.connect(write=True) as c:
            c.execute('UPDATE qq_followup_window SET last_sent=unixepoch()-11 WHERE bot_id=? AND group_id=?',(BOT,group))

    def window(self,group=GROUP):
        with storage.connect(write=True) as c:
            return follow.window(c,BOT,group)

    def request(self,n,route,action='prepare',**kw):
        return service.Request(bot_id=BOT,group_id=GROUP,message_id=str(n),window_version=route['window_version'],action=action,**kw)

    def count_turns(self):
        with storage.connect() as c:
            return c.execute('SELECT count(*) AS n FROM completed_turn').fetchone()['n']

    def test_name_regex_and_original_text_only(self):
        self.assertEqual(self.ingest(1,'妃爱，你好')['trigger_reason'],'name')
        self.assertEqual(self.ingest(2,'我刚才说妃爱了啊')['trigger_reason'],'name')
        self.assertIsNone(self.ingest(3,'普通话题')['trigger_reason'])
        self.assertIsNone(self.ingest(4,'[forward]',plain='')['trigger_reason'])
        self.assertIsNone(self.ingest(5,'/妃爱 命令')['trigger_reason'])
        self.assertFalse(self.ingest(6,'妃爱',user=BOT)['accepted'])
        for pattern in ['[','.*','x'*201]:
            with self.assertRaises(ValidationError):
                self.config.model_copy().model_validate({**self.config.model_dump(),'qq_name_trigger_patterns':[pattern]})
        cfg=self.config.model_copy(update={'qq_name_trigger_patterns':['妃爱|ひよひよ']})
        body=group_service.Ingest(bot_id=BOT,group_id=GROUP,user_id=USER,message_id='7',message='ひよひよ',text='ひよひよ',sent_at=1)
        self.assertEqual(group_service.ingest(body,cfg)['trigger_reason'],'name')

    def test_exact_five_duplicate_media_cooldown_and_independent_groups(self):
        self.open()
        first=self.ingest(1)
        self.assertTrue(first['followup'])
        self.assertFalse(self.ingest(1)['accepted'])
        self.assertEqual(self.window()['remaining'],4)
        self.assertFalse(self.ingest(2,'[image]',plain='')['followup'])
        for n in range(3,6):
            self.assertTrue(self.ingest(n,user=OTHER)['followup'])
        self.assertNotIn('followup',self.ingest(6))
        self.assertEqual(self.window()['remaining'],0)
        self.assertNotIn('followup',self.ingest(7,group='830002'))
        self.open('-101')
        with storage.connect(write=True) as c:
            c.execute('UPDATE qq_followup_window SET last_sent=unixepoch()')
        route=self.ingest(8)
        self.assertFalse(route['followup'])
        self.assertEqual(route['remaining'],4)
        self.ask.assert_not_called()
        self.open('-102')
        with storage.connect(write=True) as c:
            c.execute('UPDATE qq_followup_window SET last_sent=unixepoch()-86400')
        self.assertTrue(self.ingest(9)['followup'])  # 无时间上限

    def test_prepare_claim_receipt_and_successful_reply_renews_once(self):
        self.config=self.config.model_copy(update={'profile_enabled':True})
        self.open()
        route=self.ingest(1,user=OTHER)
        self.assertTrue(service.handle(self.request(1,route),self.config)['send'])
        self.assertEqual(self.count_turns(),0)  # 草稿不算完整问答
        self.assertFalse(service.handle(self.request(1,route),self.config)['send'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:service.handle(self.request(1,route,'claim'),self.config),range(2)))
        self.assertEqual(sum(r['send'] for r in results),1)
        self.assertEqual(self.count_turns(),1)
        old_version=self.window()['version']
        self.open('-101')
        v=self.window()['version']
        self.assertGreater(v,old_version)
        self.assertEqual(self.window()['remaining'],5)
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=GROUP,message_id='-101',message='同一回执',opens_window=True))
        self.assertEqual(self.window()['version'],v)
        service.handle(self.request(1,route,'receipt',state='sent'),self.config)
        with storage.connect() as c:
            row=c.execute('SELECT direct,profile_eligible FROM qq_group_message WHERE message_id=?',('1',)).fetchone()
            self.assertEqual(row,{'direct':1,'profile_eligible':0})
            self.assertEqual(c.execute('SELECT affinity FROM user_profile WHERE subject_key=?',('qq:'+OTHER,)).fetchone()['affinity'],50)

    def test_silence_failure_and_pending_material_barrier(self):
        self.open()
        first=self.ingest(1)
        self.ingest(2)
        with storage.connect(write=True) as c:
            c.execute('UPDATE qq_group_state SET through_id=1000')
        self.assertEqual(group_profile.material(BOT,USER,0,10),[])
        self.ask.return_value=service.Decision(send=False,reason='群友正在互相说话')
        self.assertFalse(service.handle(self.request(1,first),self.config)['send'])
        self.assertEqual(len(group_profile.material(BOT,USER,0,10)),1)
        with storage.connect() as c:
            second=c.execute("SELECT window_version FROM qq_followup_job j JOIN qq_group_message m ON m.id=j.message_row_id WHERE m.message_id='2'").fetchone()
        self.ask.side_effect=RuntimeError('模拟接口失败')
        self.assertFalse(service.handle(self.request(2,second),self.config)['send'])
        self.assertEqual(self.count_turns(),0)
        # 第2条与第1条重复刷屏，没有额外旁听画像材料。
        self.assertEqual(len(group_profile.material(BOT,USER,0,10)),1)

    def test_direct_interrupts_generation_without_saving_a_turn(self):
        self.open()
        route=self.ingest(1)
        def reply(*args):
            self.ingest(2,'妃爱，先回答我这句',user=OTHER)
            return {'reply':'旧跟聊草稿','agent_messages':[],'recalled_context':{}}
        with patch('chat_service.direct_reply',side_effect=reply):
            self.assertFalse(service.handle(self.request(1,route),self.config)['send'])
        self.assertEqual(self.count_turns(),0)
        self.assertEqual(self.window()['remaining'],0)

    def test_review_once_and_new_message_after_review_discards(self):
        self.open()
        route=self.ingest(1)
        def reply(*args):
            self.ingest(2,'我也接着说一句',user=OTHER)
            return {'reply':'草稿','agent_messages':[],'recalled_context':{}}
        with patch('chat_service.direct_reply',side_effect=reply):
            self.assertTrue(service.handle(self.request(1,route),self.config)['send'])
        self.assertEqual(self.ask.call_count,2)
        self.ingest(3,'现在话题变了')
        self.assertFalse(service.handle(self.request(1,route,'claim'),self.config)['send'])
        self.assertEqual(self.count_turns(),0)
        self.open('-102')
        route=self.ingest(4)
        def judge(*args,**kw):
            if self.ask.call_count>=2:
                self.ingest(6,'复核时又有新话题',user=OTHER)
            return service.Decision(send=True,reason='合成允许')
        self.ask.reset_mock()
        self.ask.side_effect=judge
        def reply_again(*args):
            self.ingest(5,'生成时来了一条消息',user=OTHER)
            return {'reply':'草稿','agent_messages':[],'recalled_context':{}}
        with patch('chat_service.direct_reply',side_effect=reply_again):
            self.assertFalse(service.handle(self.request(4,route),self.config)['send'])
        self.assertEqual(self.ask.call_count,2)

    def test_pause_recall_restart_and_management_do_not_open(self):
        self.open()
        route=self.ingest(1)
        group_storage.startup(BOT)
        self.assertFalse(service.handle(self.request(1,route),self.config)['send'])
        self.open('-101')
        group_storage.recall(group_service.Recall(bot_id=BOT,group_id=GROUP,message_id='-101'))
        self.assertEqual(self.window()['remaining'],0)
        self.open('-102')
        group_storage.toggle(group_service.Toggle(bot_id=BOT,group_id=GROUP,user_id=USER,enabled=False),USER)
        self.assertEqual(self.window()['remaining'],0)
        self.assertEqual(self.ingest(2,'妃爱，好呀')['trigger_reason'],'name')
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=GROUP,message_id='-103',message='管理确认',opens_window=False))
        self.assertEqual(self.window()['remaining'],0)
        group_storage.toggle(group_service.Toggle(bot_id=BOT,group_id=GROUP,user_id=USER,enabled=True),USER)
        self.assertNotIn('followup',self.ingest(3))

    def test_busy_group_and_direct_preemption_do_not_block_new_direct_reply(self):
        self.open()
        first=self.ingest(1)
        second=self.ingest(2,'我也想接话',user=OTHER)
        entered,release=Event(),Event()
        def slow(*args):
            entered.set()
            release.wait(3)
            return {'reply':'旧草稿','agent_messages':[],'recalled_context':{}}
        with patch('chat_service.direct_reply',side_effect=slow),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(service.handle,self.request(1,first),self.config)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(service.handle(self.request(2,second),self.config)['send'])
                self.ingest(3,'妃爱，优先回这句')
                request=qq_service.QQChatRequest(bot_id=BOT,group_id=GROUP,user_id=USER,message_id='3',message='妃爱，优先回这句')
                direct=qq_service.chat(request,self.config,lambda *a:{'reply':'即时回复','agent_messages':[],'recalled_context':{}})
                self.assertEqual(direct['reply'],'即时回复')
            finally:
                release.set()
            self.assertFalse(future.result(timeout=4)['send'])
        self.assertEqual(self.count_turns(),1)

    def test_old_receipt_queue_renewal_and_additive_upgrade(self):
        self.open()
        self.open('-101')
        latest=self.window()['version']
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=GROUP,message_id='-100',
            message='晚到的旧回执',opens_window=True))
        self.assertEqual(self.window()['version'],latest)
        self.assertEqual(self.window()['anchor_message_id'],'-101')
        self.ingest(1)
        with storage.connect(write=True) as c:
            original=c.execute('SELECT count(*) AS n FROM qq_group_message').fetchone()['n']
            for table in ['qq_followup_job','qq_followup_window','qq_followup_opened']:
                c.execute('DROP TABLE '+table)
        storage._ready.discard(storage.database_path())
        storage.init_db()
        with storage.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM qq_group_message').fetchone()['n'],original)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()['integrity_check'],'ok')
            self.assertEqual(c.execute('SELECT count(*) AS n FROM qq_followup_window').fetchone()['n'],0)

    def test_window_suppresses_batch_speech_but_keeps_summary(self):
        self.open()
        for i in range(1,51):
            self.ingest(i,text=f'合成消息{i}')
        old,rows,batch=group_storage.begin_batch(BOT,GROUP)
        self.assertTrue(old['suppress_speak'])
        result={'summary':'本群公开摘要','batch_summary':'批次摘要','speak':True,'reason':'合成','focus':'话题','quote_id':None}
        self.assertFalse(group_storage.finish_batch(old,rows,batch,result,[]))
        self.assertEqual(group_storage.state(BOT,GROUP)['through_id'],rows[-1]['id'])
        with storage.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM summary_batch').fetchone()['n'],1)

    def test_api_auth_and_no_source_or_group_forgery(self):
        self.open()
        route=self.ingest(1)
        body=self.request(1,route).model_dump()
        with patch('main.load_config',return_value=self.config),patch('main.resolve_config',return_value=self.config):
            client=TestClient(main.app)
            self.assertEqual(client.post('/integrations/qq/group/followup',json=body).status_code,403)
            response=client.post('/integrations/qq/group/followup',headers={'Authorization':'Bearer test-token'},json={**body,'group_id':'830002'})
            self.assertFalse(response.json()['send'])
        self.ask.assert_not_called()


if __name__=='__main__':
    unittest.main()
