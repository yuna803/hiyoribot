import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import agent_tools
import chat_service
import group_service
import group_storage
import main
import qq_service
import storage
import user_scope

BOT='100001'
OWNER='100002'
A='200001'
B='200002'
G='900001'

class ChatTests(unittest.TestCase):
    def setUp(self):
        self.home=tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        folder=Path(self.home.name)
        storage.configure(folder/'chat.sqlite3')
        storage.init_db()
        self.config=main.AppConfig(api_key='test',qq_enabled=True,qq_bot_id=BOT,qq_owner_id=OWNER,qq_backend_token='test-token',
            sqlite_path=str(folder/'chat.sqlite3'),profile_home=str(folder/'profiles'),qq_profile_home=str(folder/'profiles'))

    def request(self,user=A,group=None,mid='1',text='普通合成消息'):
        return qq_service.QQChatRequest(bot_id=BOT,user_id=user,group_id=group,message_id=mid,message=text)

    def test_original_prompt_and_qq_rules_no_game_tools_or_web_routes(self):
        raw,filtered=chat_service.core_prompt()
        self.assertEqual(raw,json.loads(chat_service.ROLECARD.read_text(encoding='utf8'))['system_prompt'])
        self.assertEqual(len(raw),11048)
        self.assertNotIn('输出结构\n',filtered)
        with user_scope.activate('qq:'+A):
            conv=storage.create_conversation('开始')
            msgs,meta=chat_service.model_messages('今天想聊程序',user_scope.config_for(self.config),identifier=conv['id'])
        self.assertIn('你在QQ里用文字',msgs[0]['content'])
        self.assertIn('不是智宏或哥哥',msgs[0]['content'])
        self.assertNotIn('当前场景：',msgs[0]['content'])
        names={t['function']['name'] for t in agent_tools.TOOLS}
        self.assertIn('search_summaries',names)
        self.assertNotIn('search_character_knowledge',names)
        routes={r.path for r in main.app.routes}
        self.assertNotIn('/chat',routes)
        self.assertNotIn('/tts/jobs',routes)
        self.assertNotIn('/assets',routes)

    def test_group_shared_authors_and_no_cross_group_or_private_history(self):
        with storage.connect(write=True) as c:
            group_storage.ensure(c,BOT,G)
        for mid,user,group,text in [('1',A,G,'A在本群喜欢红茶'),('2',B,G,'B在本群谈程序'),('3',A,'900002','其他群的隐私')]:
            group_storage.ingest(group_service.Ingest(bot_id=BOT,user_id=user,group_id=group,message_id=mid,
                nickname='显示名',message=text,text=text,sent_at=1))
        with user_scope.activate('qq:'+A):
            private=storage.create_conversation('仅私聊')
            storage.save_turn(private['id'],'私聊秘密短语','明白')
            cfg=user_scope.config_for(self.config).model_copy(update={'qq_group_id':G})
            msgs,meta=chat_service.model_messages('群里聊到哪了',cfg,group=(BOT,G),initiator=A)
            text=msgs[0]['content']
            self.assertIn('A在本群喜欢红茶',text)
            self.assertIn('B在本群谈程序',text)
            self.assertNotIn('其他群的隐私',text)
            self.assertNotIn('私聊秘密短语',text)
            with user_scope.expose_group(BOT,G):
                result=agent_tools.execute('search_chat_history','{"query":"隐私"}',private['id'],'和泉妃爱')
                self.assertEqual(result['messages'],[])
        with user_scope.activate('qq:'+B):
            result=agent_tools.execute('search_chat_history','{"query":"秘密"}',private['id'],'和泉妃爱')
            self.assertIn('error',result)

    def test_success_duplicate_failure_and_no_fake_turn_from_proactive_reply(self):
        def execute(request,config):
            return dict(reply='合成正式回复',agent_messages=[],recalled_context={'profile':{'affinity':50,'revision':0}})
        with patch('profile_service.schedule_safely'),patch('summary_service.schedule'):
            first=qq_service.chat(self.request(),self.config,execute)
            duplicate=qq_service.chat(self.request(),self.config,lambda *a: self.fail('不能重复请求模型'))
            self.assertEqual(first,duplicate)
            with self.assertRaises(RuntimeError):
                qq_service.chat(self.request(mid='2'),self.config,lambda *a: (_ for _ in ()).throw(RuntimeError('故障')))
        with storage.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM completed_turn').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM message').fetchone()['n'],2)
        with patch('main.load_config',return_value=self.config),patch('main.resolve_config',return_value=self.config),patch('main.chat_service.direct_reply') as run:
            client=TestClient(main.app)
            self.assertEqual(client.post('/integrations/qq/chat',json=self.request(mid='3').model_dump()).status_code,403)
            run.assert_not_called()

    def test_unsummarized_48_messages_are_complete_not_recent_24(self):
        with user_scope.activate('qq:'+A):
            conv=storage.create_conversation('起点')
            for n in range(24):
                storage.save_turn(conv['id'],f'第{n}条用户完整内容','第'+str(n)+'条完整回答')
            msgs,record=chat_service.model_messages('当前新消息',user_scope.config_for(self.config),identifier=conv['id'])
            self.assertEqual(len(record['source_message_ids']),48)
            self.assertEqual(record['omitted_message_ids'],[])
            self.assertIn('第0条用户完整内容',msgs[0]['content'])
            self.assertIn('第23条完整回答',msgs[0]['content'])
            with storage.connect(write=True) as c:
                c.execute("UPDATE message SET content=? WHERE conversation_id=? AND role='user'",('长'*4000,conv['id']))
            with self.assertRaisesRegex(ValueError,'完整原文超过'):
                chat_service.model_messages('当前新消息',user_scope.config_for(self.config),identifier=conv['id'])

    def test_group_49_unsummarized_messages_are_complete(self):
        for n in range(49):
            group_storage.ingest(group_service.Ingest(bot_id=BOT,user_id=A if n%2 else B,group_id=G,message_id=str(100+n),
                message=f'本群第{n}条完整原文',text=f'本群第{n}条完整原文',sent_at=1))
        with user_scope.group(BOT,G):
            messages,record=chat_service.model_messages('当前请求',self.config,group=(BOT,G))
        self.assertEqual(len(record['source_message_ids']),49)
        self.assertEqual(record['omitted_message_ids'],[])
        self.assertIn('本群第0条完整原文',messages[0]['content'])
        self.assertIn('本群第48条完整原文',messages[0]['content'])

    def test_startup_sqlite_only_and_fresh_database(self):
        with patch('main.load_config',return_value=self.config),patch('main.resolve_config',return_value=self.config):
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/health').json()['storage'],'sqlite')
        with storage.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM user_profile').fetchone()['n'],0)
            self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()['foreign_keys'],1)
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()['journal_mode'],'wal')

if __name__=='__main__': unittest.main()
