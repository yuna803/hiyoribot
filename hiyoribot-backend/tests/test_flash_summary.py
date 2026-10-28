import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import main
import storage
import summary_service as service
import user_scope

class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.home=tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        folder=Path(self.home.name)
        storage.configure(folder/'summary.sqlite3')
        storage.init_db()
        self.config=main.AppConfig(api_key='test',sqlite_path=str(folder/'summary.sqlite3'),profile_home=str(folder/'profiles'),qq_profile_home=str(folder/'profiles'))

    def test_long_fragments_preserve_all_characters(self):
        rows=[dict(id=1,role='user',content='甲'*32000),dict(id=2,role='assistant',content='乙'*17000)]
        parts=[r for p in service.fragments(rows) for r in p]
        for row in rows:
            self.assertEqual(''.join(r['content'] for r in parts if r['id']==row['id']),row['content'])

    def test_50_100_archives_accumulate_failure_retry_and_keyword_scope(self):
        with user_scope.activate('qq:200001'):
            config=user_scope.config_for(self.config)
            conv=storage.create_conversation('起点')
            other=storage.create_conversation('另一会话')
            for n in range(24): storage.save_turn(conv['id'],f'用户原话{n}','正式回复')
            with patch('summary_service.summarize') as analyze:
                service.refresh(conv['id'],config)
                analyze.assert_not_called()
            storage.save_turn(conv['id'],'我想继续聊SQLite','好的')
            with patch('summary_service.summarize',side_effect=ValueError('非法输出')):
                service.refresh(conv['id'],config)
            self.assertEqual(storage.get_conversation(conv['id'])['summary_through_id'],0)
            covered=[]
            def analyze(old,rows,config):
                covered.append([r['id'] for r in rows])
                return dict(batch_summary='用户想聊SQLite，助手同意继续。',summary=old+'继续讨论SQLite。'),[{'id':r['id']} for r in rows]
            with patch('summary_service.summarize',side_effect=analyze):
                service.refresh(conv['id'],config)
                service.refresh(conv['id'],config)
                for n in range(25): storage.save_turn(conv['id'],f'后续新问答{n}','新的正式回复')
                service.refresh(conv['id'],config)
            self.assertEqual([len(r) for r in covered],[50,50])
            self.assertFalse(set(covered[0])&set(covered[1]))
            current=storage.get_conversation(conv['id'])
            self.assertEqual(current['summary_revision'],2)
            self.assertIn('继续讨论SQLite。继续讨论SQLite。',current['summary'])
            self.assertEqual(len(storage.search_summaries(service.private_scope(conv['id']),'SQLite')),2)
            self.assertEqual(storage.search_summaries(service.private_scope(other['id']),'SQLite'),[])

if __name__=='__main__': unittest.main()
