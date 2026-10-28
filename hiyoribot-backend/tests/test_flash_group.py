"""SQLite 群聊回归：只使用临时库和合成消息。"""
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import group_service as service
import group_storage as store
import storage

BOT, USER, OWNER, GROUP, GROUP2 = '810001', '820001', '820003', '830001', '830002'


def event(n, group=GROUP, user=USER, **changes):
    text = f'我喜欢喝茶，消息编号 {n}'
    return service.Ingest(**{**dict(bot_id=BOT, group_id=group, user_id=user,
        message_id=str(n), nickname='合成用户', message=text, text=text, sent_at=int(time.time())), **changes})


def decision(speak=False):
    return dict(summary='公开话题摘要', batch_summary='本批摘要', speak=speak,
                reason='合成测试', focus='话题', quote_id=None)


class FlashGroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.configure(Path(self.tmp.name) / 'group.sqlite')
        self.config = SimpleNamespace(qq_bot_id=BOT, qq_owner_id=OWNER, qq_enabled=True,
                                      profile_enabled=False)
        self.scheduler = patch('group_service.schedule').start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def fill(self, start=1, count=50, group=GROUP, **changes):
        return [service.ingest(event(i, group, **changes), self.config) for i in range(start, start + count)]

    def draft(self, group=GROUP, start=1):
        self.fill(start=start, group=group)
        old, rows, identifier = store.begin_batch(BOT, group)
        self.assertTrue(store.finish_batch(old, rows, identifier, decision(True), []))
        store.save_candidate(old, rows, identifier, '合成回复', None)
        return store.candidates(BOT)['items'][-1]['id']

    def claim(self, identifier):
        return service.claim(service.Claim(bot_id=BOT, candidate_id=identifier), self.config)

    def test_batches_groups_and_filtering(self):
        self.fill(count=49)
        self.assertIsNone(store.begin_batch(BOT, GROUP))
        self.scheduler.assert_not_called()
        self.fill(start=50, count=1, message='[image]', text='')
        self.assertEqual(self.scheduler.call_count, 1)
        old, rows, batch = store.begin_batch(BOT, GROUP)
        self.assertEqual(len(rows), 50)
        store.finish_batch(old, rows, batch, decision(), [])
        self.fill(start=51, count=50)
        self.assertEqual(len(store.begin_batch(BOT, GROUP)[1]), 50)
        self.fill(count=49, group=GROUP2)
        self.assertIsNone(store.begin_batch(BOT, GROUP2))
        self.assertFalse(service.ingest(event(100), self.config)['accepted'])
        self.assertFalse(service.ingest(event(500, user=BOT), self.config)['accepted'])
        self.assertEqual(service.ingest(event(50, GROUP2, message='[record]', text=''), self.config)['pending'], 50)
        self.fill(start=101, count=1, message='/command', text='/command')
        self.fill(start=102, count=1, message='我喜欢喝茶，消息编号 1', text='我喜欢喝茶，消息编号 1')
        with storage.connect() as conn:
            rows = conn.execute('SELECT message_id,profile_eligible FROM qq_group_message WHERE bot_id=? AND group_id=? AND message_id IN (?,?,?)', (BOT, GROUP, '50', '101', '102')).fetchall()
        self.assertEqual({r['message_id']: r['profile_eligible'] for r in rows}, {'50': 0, '101': 0, '102': 0})

    def test_owner_switch(self):
        self.fill(count=20)
        with self.assertRaises(HTTPException):
            store.toggle(service.Toggle(bot_id=BOT, group_id=GROUP, user_id=USER, enabled=False), OWNER)
        toggle = service.Toggle(bot_id=BOT, group_id=GROUP, user_id=OWNER, enabled=False)
        store.toggle(toggle, OWNER)
        self.assertFalse(service.ingest(event(21), self.config)['accepted'])
        self.assertTrue(service.ingest(event(22, direct=True), self.config)['accepted'])
        store.toggle(toggle.model_copy(update={'enabled': True}), OWNER)
        self.assertEqual(service.ingest(event(23), self.config)['pending'], 1)

    def test_parallel_claim_cooldown_and_hour_limit(self):
        identifier = self.draft()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.claim(identifier), range(2)))
        self.assertEqual(sum(item['send'] for item in results), 1)
        with storage.connect() as conn:
            self.assertFalse(store.quota(conn, BOT, GROUP))
        for i in range(2):
            with storage.connect(write=True) as conn:
                conn.execute('UPDATE qq_group_candidate SET attempted_at=unixepoch()-? WHERE id=?', (1200 - i * 360, str(identifier)))
            identifier = self.draft(start=51 + i * 50)
            self.assertTrue(self.claim(identifier)['send'])
        with storage.connect(write=True) as conn:
            conn.execute('UPDATE qq_group_candidate SET attempted_at=unixepoch()-360 WHERE id=?', (str(identifier),))
        with storage.connect() as conn:
            self.assertFalse(store.quota(conn, BOT, GROUP))

    def test_review_new_message_direct_and_restart(self):
        identifier = self.draft()
        self.fill(start=51, count=1)
        def review(*args):
            self.fill(start=52, count=1)
            return service.Review(send=True, reason='允许')
        with patch.object(service.GroupModel, 'json', side_effect=review) as model:
            self.assertFalse(self.claim(identifier)['send'])
            self.assertEqual(model.call_count, 1)
        identifier = self.draft(group=GROUP2)
        self.fill(start=51, count=1, group=GROUP2)
        def direct(*args):
            store.direct(BOT, GROUP2, True)
            return service.Review(send=True, reason='允许')
        with patch.object(service.GroupModel, 'json', side_effect=direct):
            self.assertFalse(self.claim(identifier)['send'])
        store.direct(BOT, GROUP2, False)
        identifier = self.draft(group='830003')
        self.fill(start=51, count=1, group='830003')
        with patch.object(service.GroupModel, 'json', return_value=service.Review(send=True, reason='允许')):
            self.assertTrue(self.claim(identifier)['send'])
        identifier = self.draft(group='830004')
        store.startup(BOT)
        self.assertFalse(self.claim(identifier)['send'])

    def test_recall_tombstone_and_restart_inflight(self):
        store.recall(service.Recall(bot_id=BOT, group_id=GROUP, message_id='1'))
        self.assertFalse(service.ingest(event(1), self.config)['accepted'])
        self.fill(start=2)
        old, rows, identifier = store.begin_batch(BOT, GROUP)
        store.startup(BOT)
        self.assertFalse(store.finish_batch(old, rows, identifier, decision(True), []))
        store.save_candidate(old, rows, identifier, '过期草稿', None)
        self.assertEqual(store.candidates(BOT)['items'], [])

    def test_recall_archive_expiry_and_failure(self):
        self.fill()
        old, rows, batch = store.begin_batch(BOT, GROUP)
        store.finish_batch(old, rows, batch, decision(), [])
        scope = f'group:{BOT}:{GROUP}'
        with storage.connect(write=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM summary_batch WHERE scope_key=?', (scope,)).fetchone()['n'], 1)
            conn.execute('INSERT INTO qq_group_evidence(message_id,bot_id,group_id,user_id,quote) VALUES(?,?,?,?,?)', (rows[0]['id'], BOT, GROUP, USER, '我喜欢喝茶'))
        store.recall(service.Recall(bot_id=BOT, group_id=GROUP, message_id='1'))
        with storage.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM summary_batch WHERE scope_key=?', (scope,)).fetchone()['n'], 0)
        self.assertEqual(store.state(BOT, GROUP)['summary'], '')
        self.assertEqual(store.public_evidence_ids([rows[0]['id']], GROUP, BOT, USER), set())
        self.fill(start=51, count=49)
        with storage.connect(write=True) as conn:
            conn.execute('INSERT INTO qq_group_evidence(message_id,bot_id,group_id,user_id,quote) VALUES(?,?,?,?,?)', (rows[1]['id'], BOT, GROUP, USER, '我喜欢喝茶'))
            conn.execute('UPDATE qq_group_message SET received_at=unixepoch()-691200 WHERE bot_id=?', (BOT,))
        store.cleanup()
        self.assertEqual(store.public_evidence_ids([rows[1]['id']], GROUP, BOT, USER), {rows[1]['id']})
        self.assertIsNone(store.begin_batch(BOT, GROUP))
        self.assertEqual(service.ingest(event(100), self.config)['pending'], 1)
        self.fill(start=101, count=49)
        cursor = store.state(BOT, GROUP)['through_id']
        with patch('group_service.observe', side_effect=ValueError('synthetic failure')):
            service.work(BOT, GROUP, self.config)
        self.assertEqual(store.state(BOT, GROUP)['through_id'], cursor)
        self.assertEqual(store.state(BOT, GROUP)['stage'], 'failed')
        self.assertEqual(store.candidates(BOT)['items'], [])


if __name__ == '__main__':
    unittest.main()
