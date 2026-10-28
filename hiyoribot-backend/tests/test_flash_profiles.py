import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import group_profile
import group_storage
import profile_service as service
import profile_storage as store
import storage
import user_scope


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        storage.configure(root / 'test.sqlite')
        self.config = SimpleNamespace(profile_enabled=True, profile_home=str(root / 'profile'),
            qq_profile_home=str(root / 'qq'), profile_initial_messages=20, profile_batch_messages=50,
            qq_owner_id='', qq_bot_id='11111', qq_group_id='')
        store.ensure(user_scope.initial_profile(self.config))

    def tearDown(self):
        self.tmp.cleanup()

    def turns(self, n, subject='web:owner'):
        with storage.connect(write=True) as conn:
            cid = 'c-' + subject
            conn.execute('INSERT OR IGNORE INTO conversation(id,subject_key) VALUES(?,?)', (cid, subject))
            for i in range(n):
                u = conn.execute('INSERT INTO message(conversation_id,role,content) VALUES(?,?,?)',
                                 (cid, 'user', f'message {i}')).lastrowid
                a = conn.execute('INSERT INTO message(conversation_id,role,content) VALUES(?,?,?)',
                                 (cid, 'assistant', 'reply')).lastrowid
                conn.execute('INSERT INTO completed_turn(conversation_id,user_message_id,assistant_message_id,subject_key) VALUES(?,?,?,?)', (cid, u, a, subject))

    def result(self, old, delta=0):
        return dict(profile=old['profile'], delta=delta, reason='test', evidence=[])

    def test_direct_batches_cas_bounds_and_isolation(self):
        self.turns(9)
        self.assertEqual(store.pending(0), 18)
        self.turns(1)
        old = store.get()
        rows = store.source(0, 20)
        self.assertEqual((len(rows), service.threshold(old, self.config)), (20, 20))
        with self.assertRaises(ValueError):
            store.commit(old, rows, self.result(old, 6))
        self.assertEqual(store.get()['through_turn_id'], 0)
        self.assertTrue(store.commit(old, rows, self.result(old, 5)))
        self.assertFalse(store.commit(old, rows, self.result(old, 5)))
        self.assertEqual((store.get()['affinity'], service.threshold(store.get(), self.config)), (55, 50))
        self.turns(25)
        state = store.get()
        self.assertEqual(len(store.source(state['through_turn_id'], 50)), 50)
        self.turns(1, 'qq:12345')
        with user_scope.activate('qq:12345'):
            other = store.ensure(user_scope.initial_profile(self.config))
            with self.assertRaises(ValueError):
                store.commit(state, rows, self.result(state))
            self.assertEqual(other['affinity'], 50)
        with storage.connect(write=True) as conn:
            conn.execute('UPDATE user_profile SET affinity=99 WHERE subject_key=?', ('web:owner',))
        state = store.get()
        self.assertTrue(store.commit(state, store.source(state['through_turn_id'], 50), self.result(state, 5)))
        self.assertEqual(store.get()['affinity'], 100)
        self.turns(25)
        with storage.connect(write=True) as conn:
            conn.execute('UPDATE user_profile SET affinity=-99 WHERE subject_key=?', ('web:owner',))
        state = store.get()
        self.assertTrue(store.commit(state, store.source(state['through_turn_id'], 50), self.result(state, -5)))
        self.assertEqual(store.get()['affinity'], -100)

    def test_invalid_model_output_and_public_evidence(self):
        state = store.get()
        data = self.result(state, 1)
        data['evidence'] = [{'source_kind': 'message', 'message_id': 3, 'quote': 'fake quote'}]
        with self.assertRaises(ValueError):
            service.validate(service.Analysis.model_validate(data), {3: 'different text'}, state['profile'], {3})
        with self.assertRaises(ValueError):
            service.Analysis.model_validate(self.result(state, 6))
        with storage.connect(write=True) as conn:
            conn.execute('INSERT INTO qq_group_evidence(message_id,bot_id,group_id,user_id,quote) VALUES(1,?,?,?,?)', ('11111', '22222', '12345', 'test'))
        self.assertEqual(group_storage.public_evidence_ids([1], '22222', '11111', '12345'), {1})
        self.assertEqual(group_storage.public_evidence_ids([1], '33333', '11111', '12345'), set())
        self.assertEqual(group_storage.public_evidence_ids([1], '22222', '11111', '54321'), set())
        with storage.connect(write=True) as conn:
            conn.execute('UPDATE qq_group_evidence SET recalled=1 WHERE message_id=1')
        self.assertEqual(group_storage.public_evidence_ids([1], '22222', '11111', '12345'), set())

    def test_passive_batches_and_failure(self):
        bot, user, group = '11111', '12345', '22222'
        with storage.connect(write=True) as conn:
            conn.execute('INSERT INTO qq_group_state(bot_id,group_id,through_id) VALUES(?,?,1000)', (bot, group))
            conn.execute('INSERT INTO qq_passive_profile(bot_id,user_id) VALUES(?,?)', (bot, user))
            for i in range(9):
                conn.execute('INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,plain_text,sent_at,profile_eligible) VALUES(?,?,?,?,?,0,1)', (bot, group, str(i), user, 'I enjoy reading'))
        class Ask:
            def fragments(self, rows, **kwargs):
                return [rows]
            def json(self, *args):
                return group_profile.PassiveAnalysis(observations=[])
        group_profile.update(bot, user, self.config, Ask())
        with storage.connect(write=True) as conn:
            conn.execute('INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,plain_text,sent_at,profile_eligible) VALUES(?,?,?,?,?,0,1)', (bot, group, '9', user, 'I enjoy reading'))
        group_profile.update(bot, user, self.config, Ask())
        with storage.connect() as conn:
            progress = conn.execute('SELECT * FROM qq_passive_profile').fetchone()
            self.assertEqual((progress['updates'], progress['through_id']), (1, 10))
        with user_scope.activate('qq:' + user):
            state = store.get()
            self.assertEqual((state['affinity'], state['direct_updates'], service.threshold(state, self.config)), (50, 0, 20))
        with storage.connect(write=True) as conn:
            for i in range(10, 35):
                conn.execute('INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,plain_text,sent_at,profile_eligible) VALUES(?,?,?,?,?,0,1)', (bot, group, str(i), user, 'I enjoy reading'))
        class Failing(Ask):
            def json(self, *args):
                raise ValueError('failed')
        with self.assertRaises(ValueError):
            group_profile.update(bot, user, self.config, Failing())
        with storage.connect() as conn:
            self.assertEqual(conn.execute('SELECT through_id FROM qq_passive_profile').fetchone()['through_id'], 10)
        group_profile.update(bot, user, self.config, Ask())
        with storage.connect() as conn:
            self.assertEqual(conn.execute('SELECT updates FROM qq_passive_profile').fetchone()['updates'], 2)

    def test_export_retry_and_lock(self):
        with patch.object(service, '_atomic', side_effect=OSError('failed')):
            service.export_safely(self.config)
        self.assertIsNotNone(store.get()['export_error'])
        service.export_safely(self.config)
        self.assertIsNone(store.get()['export_error'])
        self.assertTrue((Path(self.config.profile_home) / 'profile.json').exists())
        result = []
        with storage.subject_lock('profile:web:owner', blocking=False):
            def attempt():
                with storage.subject_lock('profile:web:owner', blocking=False) as ok:
                    result.append(ok)
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join()
        self.assertEqual(result, [False])


if __name__ == '__main__':
    unittest.main()
