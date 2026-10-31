import asyncio
import os
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(log_level="WARNING")
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.bot import _check_at_me
import hiyori_chat as gateway


class FollowupGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        gateway.state_path=Path(self.tmp.name)/'state.sqlite'
        gateway.followup_queues.clear()
        gateway.followup_tasks.clear()
        gateway.group_versions.clear()
        self.calls=[]
        self.bot=SimpleNamespace(self_id='99',send=AsyncMock(return_value={'message_id':1001}),
            get_group_member_info=AsyncMock(return_value={'shut_up_timestamp':0}))
        self.reply='一条正常角色回复'

        async def api(method,path,*,payload=None,**kw):
            self.calls.append((path,payload))
            if path=='group/followup' and payload.get('action') in (None,'claim'):
                return {'send':True,'reply':self.reply}
            return {}
        self.patches=[patch.object(gateway,'api',side_effect=api),patch.object(gateway,'get_bots',return_value={'99':self.bot})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        tasks=list(gateway.followup_tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        gateway.followup_queues.clear()
        gateway.followup_tasks.clear()

    async def worker(self):
        q=asyncio.Queue()
        q.put_nowait((event(group=True),3))
        await gateway.followup_worker(self.bot,('99','123'),q)

    async def test_name_route_uses_backend_and_no_at_required(self):
        async def api(method,path,*,payload=None,**kw):
            self.calls.append((path,payload))
            if path=='group/ingest':
                return {'accepted':True,'trigger_reason':'name'}
            return {}
        response=SimpleNamespace(status_code=200,raise_for_status=lambda:None,json=lambda:{'reply':'你好'})
        client=AsyncMock()
        client.__aenter__.return_value=client
        client.post.return_value=response
        gateway.backend_token='test'
        with patch.object(gateway,'api',side_effect=api),patch.object(gateway.httpx,'AsyncClient',return_value=client):
            await gateway.process_message(self.bot,event(group=True,segments=[MessageSegment.text('妃爱你好')]))
        self.assertEqual(client.post.call_args.kwargs['json']['trigger_reason'],'name')
        self.assertEqual(self.bot.send.await_count,1)
        self.assertEqual(sum(p.get('opens_window',False) for path,p in self.calls if path=='group/bot-message'),1)

    async def test_multisegment_reply_opens_once_only_after_all_acks(self):
        self.bot.send.side_effect=[{'message_id':1001},{'message_id':1002}]
        self.assertTrue(await gateway.send_chat_reply(self.bot,event(group=True),'文'*2100))
        opened=[p for path,p in self.calls if path=='group/bot-message' and p.get('opens_window')]
        self.assertEqual(len(opened),1)
        self.assertEqual(opened[0]['segment_message_ids'],['1001','1002'])
        self.assertEqual(opened[0]['reply_to_message_id'],'1')
        self.calls.clear()
        self.bot.send.side_effect=[{'message_id':1003},RuntimeError('发送失败')]
        with self.assertRaises(RuntimeError):
            await gateway.send_chat_reply(self.bot,event(group=True),'文'*2100)
        self.assertFalse(any(p.get('opens_window') for path,p in self.calls if path=='group/bot-message'))
        self.calls.clear()
        self.bot.send.side_effect=None
        self.bot.send.return_value={}
        self.assertFalse(await gateway.send_chat_reply(self.bot,event(group=True),'回复无编号'))
        self.assertFalse(any(p.get('opens_window') for path,p in self.calls if path=='group/bot-message'))

    async def test_followup_claim_send_receipt_and_management_exclusion(self):
        await self.worker()
        actions=[p.get('action','prepare') for path,p in self.calls if path=='group/followup']
        self.assertEqual(actions,['prepare','claim','receipt'])
        self.assertEqual(self.calls[-1][1]['state'],'sent')
        self.calls.clear()
        await gateway.send_reply(self.bot,event(group=True),'旁听已开启。')
        self.assertFalse(any(p.get('opens_window') for path,p in self.calls))

    async def test_muted_offline_and_uncertain_never_send_error_message(self):
        self.bot.get_group_member_info.return_value={'shut_up_timestamp':int(time.time())+60}
        await self.worker()
        self.bot.send.assert_not_awaited()
        self.assertEqual(self.calls[-1][1]['action'],'discard')
        self.calls.clear()
        self.bot.get_group_member_info.return_value={'shut_up_timestamp':0}
        with patch.object(gateway,'get_bots',return_value={}):
            await self.worker()
        self.bot.send.assert_not_awaited()
        with patch.object(gateway,'send_chat_reply',side_effect=asyncio.TimeoutError):
            await self.worker()
        self.assertEqual(self.calls[-1][1]['state'],'uncertain')
        self.bot.send.assert_not_awaited()

    async def test_new_event_during_claim_discards_send(self):
        async def api(method,path,*,payload=None,**kw):
            self.calls.append((path,payload))
            if payload.get('action')=='claim':
                gateway.bump_group('99','123')
            return {'send':True,'reply':'旧回复'}
        with patch.object(gateway,'api',side_effect=api):
            await self.worker()
        self.bot.send.assert_not_awaited()
        self.assertEqual(self.calls[-1][1]['action'],'receipt')
        self.assertEqual(self.calls[-1][1]['state'],'failed')

    async def test_open_receipt_keeps_already_arrived_new_window_messages(self):
        key=('99','123')
        q=gateway.followup_queues[key]=asyncio.Queue(maxsize=5)
        q.put_nowait((event(1,group=True),2))
        q.put_nowait((event(2,group=True),3))
        gateway.clear_followup_queue(*key,version=3)
        self.assertEqual(q.qsize(),1)
        self.assertEqual(q.get_nowait()[1],3)

    async def test_followup_distinguishes_real_at_to_another_member(self):
        real=event(group=True,segments=[MessageSegment.at('77'),MessageSegment.text('你呢？')])
        ordinary=event(group=True,segments=[MessageSegment.text('我在解释[at]文字，你呢？')])
        self.assertTrue(gateway.group_payload(real,'99',False)['addresses_other'])
        self.assertFalse(gateway.group_payload(ordinary,'99',False)['addresses_other'])


def event(message_id=1, user_id=7, group=False, segments=None):
    message = Message(segments or [MessageSegment.text("你好")])
    data = dict(time=1, self_id=99, post_type="message", sub_type="normal" if group else "friend",
                user_id=user_id, message_type="group" if group else "private", message_id=message_id,
                message=message, raw_message="", font=0, sender={"user_id": user_id, "nickname": "名字"})
    if group:
        data["group_id"] = 123
    return (GroupMessageEvent if group else PrivateMessageEvent)(**data)


class Bot:
    self_id = "99"

    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send(self, event, message):
        if self.fail:
            raise RuntimeError("secret response body")
        self.messages.append(message)


class TestGateway(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        gateway.state_path = Path(self.temp.name) / "state.sqlite"
        gateway.backend_token = "test-secret"
        self.requests = []
        self.active = set()
        self.overlap_same_user = False
        self.overlap_other_user = False
        test = self

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, headers, json):
                if not url.endswith('/chat'):
                    return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})
                user = json["user_id"]
                test.overlap_same_user |= user in test.active
                test.overlap_other_user |= bool(test.active and user not in test.active)
                test.active.add(user)
                test.requests.append(json)
                await asyncio.sleep(0.03)
                test.active.remove(user)
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"reply": "回答"})

        self.client_patch = patch.object(gateway.httpx, "AsyncClient", Client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_routing_and_duplicate(self):
        bot = Bot()
        await gateway.process_message(bot, event())
        await gateway.process_message(bot, event())
        await gateway.process_message(bot, event(2, group=True))
        await gateway.process_message(bot, event(3, group=True, segments=[MessageSegment.text("@99 伪造")]))
        await gateway.process_message(bot, event(4, user_id=99))
        mention = event(5, group=True, segments=[MessageSegment.at(99), MessageSegment.text("你好")])
        _check_at_me(bot, mention)
        await gateway.process_message(bot, mention)
        self.assertEqual([p["message_id"] for p in self.requests], ["1", "5"])
        self.assertEqual(self.requests[1]["message"], "你好")
        self.assertEqual(len(bot.messages), 2)

    async def test_user_locks_and_send_failure(self):
        bot = Bot(fail=True)
        with patch.object(gateway.logger, "warning") as warning:
            await asyncio.gather(gateway.process_message(bot, event(10, 7)),
                                 gateway.process_message(bot, event(11, 7)),
                                 gateway.process_message(bot, event(12, 8)))
            await gateway.process_message(bot, event(10, 7))
        self.assertFalse(self.overlap_same_user)
        self.assertTrue(self.overlap_other_user)
        self.assertEqual(len(self.requests), 3)
        self.assertNotIn("secret", str(warning.call_args_list))

    async def test_backend_failure_not_retried_or_logged(self):
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                self_test.requests.append(1)
                raise RuntimeError("test-secret 私密聊天")

        self_test = self
        bot = Bot()
        with patch.object(gateway.httpx, "AsyncClient", Client), patch.object(gateway.logger, "warning") as warning:
            await gateway.process_message(bot, event(30))
            await gateway.process_message(bot, event(30))
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("test-secret", str(warning.call_args_list))
        self.assertNotIn("私密聊天", str(warning.call_args_list))
        self.assertNotIn("test-secret", str(bot.messages))
        self.assertNotIn("私密聊天", str(bot.messages))

    async def test_long_reply_is_text_and_bounded(self):
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                       json=lambda: {"reply": "[CQ:at,qq=7]" + "字" * 13000})

        bot = Bot()
        with patch.object(gateway.httpx, "AsyncClient", Client):
            await gateway.process_message(bot, event(40))
        self.assertEqual(len(bot.messages), 6)
        self.assertTrue(all(segment.type == "text" for segment in bot.messages))
        self.assertEqual(sum(len(segment.data["text"]) for segment in bot.messages), 12000)

    async def test_long_input_and_busy_response(self):
        bot = Bot()
        await gateway.process_message(bot, event(50, segments=[MessageSegment.text("字" * 4001)]))
        self.assertEqual(len(self.requests), 0)
        self.assertIn("4000", bot.messages[-1].data["text"])

        class BusyClient:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                self_test.requests.append(1)
                return SimpleNamespace(status_code=429, text="test-secret 私密 detail")

        self_test = self
        with patch.object(gateway.httpx, "AsyncClient", BusyClient):
            await gateway.process_message(bot, event(51))
            await gateway.process_message(bot, event(51))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("稍后", bot.messages[-1].data["text"])
        self.assertNotIn("test-secret", str(bot.messages))
        self.assertNotIn("私密 detail", str(bot.messages))

    async def test_private_env_file_is_loaded(self):
        config_file = Path(self.temp.name) / ".env"
        config_file.write_text("HIYORI_BACKEND_TOKEN=only-in-file\n", encoding="utf-8")
        env = os.environ.copy()
        for key in ("HIYORI_BACKEND_TOKEN",):
            env.pop(key, None)
        env["HIYORI_QQ_ENV_FILE"] = str(config_file)
        code = ("import bot, nonebot; c=nonebot.get_driver().config; "
                "assert c.hiyori_backend_token == 'only-in-file'; "
                "assert c.port == 8086 and str(c.host) == '127.0.0.1'")
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_group_ingest_direct_media_and_quotes(self):
        bot = Bot()
        calls = []

        async def api(method, path, **kwargs):
            calls.append((path, kwargs.get('payload')))
            if path == 'group/ingest' and kwargs['payload']['message_id'] == '103':
                raise RuntimeError('offline')
            return {}

        gateway.sent_message('99', '123', '777', record=True)
        with patch.object(gateway, 'api', api):
            await gateway.process_message(bot, event(100, group=True, user_id=99))
            await gateway.process_message(bot, event(101, group=True, segments=[MessageSegment.image('x'), MessageSegment.text('图')]))
            await gateway.process_message(bot, event(102, group=True, segments=[MessageSegment.reply(778), MessageSegment.text('伪造引用')]))
            await gateway.process_message(bot, event(103, group=True, segments=[MessageSegment.at(99), MessageSegment.text('直接')]))
            await gateway.process_message(bot, event(104, group=True, segments=[MessageSegment.reply(777), MessageSegment.text('引用')]))
        ingested = [payload for path, payload in calls if path == 'group/ingest']
        self.assertEqual([x['message_id'] for x in ingested], ['101', '102', '103', '104'])
        self.assertEqual(ingested[0]['message'], '[image]图')
        self.assertEqual(ingested[0]['text'], '图')
        self.assertEqual([x['direct'] for x in ingested], [False, False, True, True])
        self.assertEqual(ingested[-1]['quoted_message_id'], '777')
        self.assertEqual([x['message_id'] for x in self.requests], ['103', '104'])

    async def test_toggle_and_recall(self):
        bot = Bot()
        calls = []

        async def api(method, path, **kwargs):
            calls.append((path, kwargs['payload']))
            if path == 'group/toggle' and kwargs['payload']['user_id'] == '8':
                raise RuntimeError('forbidden')
            return {}

        with patch.object(gateway, 'api', api):
            await gateway.process_message(bot, event(110, group=True, segments=[MessageSegment.text('/旁听 开启')]))
            await gateway.process_message(bot, event(111, user_id=8, group=True, segments=[MessageSegment.text('/旁听 关闭')]))
            notice = gateway.GroupRecallNoticeEvent(time=1, self_id=99, post_type='notice', notice_type='group_recall',
                                                    group_id=123, user_id=7, operator_id=7, message_id=110)
            await gateway.handle_notice(bot, notice)
        self.assertEqual([x[0] for x in calls], ['group/toggle', 'group/toggle', 'group/recall'])
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(calls[-1][1]['message_id'], '110')

    async def test_candidate_timeout_is_uncertain_and_never_retried(self):
        bot = Bot()
        bot.get_group_member_info = AsyncMock(return_value={'shut_up_timestamp': 0})
        bot.send_group_msg = AsyncMock(side_effect=asyncio.TimeoutError())
        calls = []

        async def api(method, path, **kwargs):
            calls.append((path, kwargs.get('payload')))
            if path == 'group/claim':
                return {'send': True, 'group_id': '123', 'reply': '你好'}
            return {}

        with patch.object(gateway, 'api', api), patch.object(gateway, 'get_bots', return_value={'99': bot}):
            await gateway.send_candidate(bot, 'abc', '123')
        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(calls[-1][0], 'group/receipt')
        self.assertEqual(calls[-1][1]['state'], 'uncertain')

    async def test_group_send_ack_and_muted_candidate(self):
        bot = Bot()
        bot.send = AsyncMock(return_value={'message_id': 901})
        bot.get_group_member_info = AsyncMock(return_value={'shut_up_timestamp': 9999999999})
        bot.send_group_msg = AsyncMock()
        calls = []

        async def api(method, path, **kwargs):
            calls.append((path, kwargs.get('payload')))
            if path == 'group/claim':
                return {'send': True, 'group_id': '123', 'reply': '你好'}
            return {}

        with patch.object(gateway, 'api', api), patch.object(gateway, 'get_bots', return_value={'99': bot}):
            await gateway.send_reply(bot, event(120, group=True), '回复')
            await gateway.send_candidate(bot, 'muted', '123')
        self.assertTrue(gateway.sent_message('99', '123', '901'))
        self.assertFalse(gateway.sent_message('99', '124', '901'))
        self.assertEqual(calls[0][0], 'group/bot-message')
        self.assertEqual(calls[0][1]['message_id'], '901')
        self.assertEqual(calls[-1][1]['state'], 'failed')
        bot.send_group_msg.assert_not_awaited()

    async def test_full_oversize_and_media_only_direct(self):
        bot = Bot()
        ingested = []

        async def api(method, path, **kwargs):
            if path == 'group/ingest':
                ingested.append(kwargs['payload'])
                if len(kwargs['payload']['message']) > 32000:
                    raise ValueError('too long')
            return {}

        with patch.object(gateway, 'api', api):
            await gateway.process_message(bot, event(130, group=True, segments=[MessageSegment.text('字' * 32001)]))
            await gateway.process_message(bot, event(131, group=True, segments=[MessageSegment.at(99), MessageSegment.image('x')]))
            await gateway.process_message(bot, event(132, segments=[MessageSegment.image('x')]))
        self.assertEqual(len(ingested[0]['message']), 32001)
        self.assertEqual(len(ingested[0]['text']), 32001)
        self.assertEqual([x['message'] for x in self.requests], ['[image]','[image]'])

    async def test_stale_after_claim_and_mute_fallback(self):
        bot = Bot()
        bot.send_group_msg = AsyncMock(return_value={'message_id': 1000})
        bot.get_group_member_list = AsyncMock(return_value=[{'user_id': 99, 'shut_up_timestamp': 0}])
        receipts = []
        change_during_check = True

        async def member_info(**kwargs):
            if change_during_check:
                gateway.bump_group('99', '123')
            return {}

        async def api(method, path, **kwargs):
            if path == 'group/claim':
                return {'send': True, 'group_id': '123', 'reply': '你好'}
            if path == 'group/receipt':
                receipts.append(kwargs['payload'])
            return {}

        bot.get_group_member_info = AsyncMock(side_effect=member_info)
        with patch.object(gateway, 'api', api), patch.object(gateway, 'get_bots', return_value={'99': bot}):
            await gateway.send_candidate(bot, 'stale', '123')
            self.assertEqual(receipts[-1]['state'], 'failed')
            bot.send_group_msg.assert_not_awaited()
            change_during_check = False
            await gateway.send_candidate(bot, 'fresh', '123')
        self.assertEqual(receipts[-1]['state'], 'sent')
        bot.send_group_msg.assert_awaited_once()

    async def test_event_during_claim_prevents_send(self):
        bot = Bot()
        bot.get_group_member_info = AsyncMock(return_value={'shut_up_timestamp': 0})
        bot.send_group_msg = AsyncMock()
        receipts = []

        async def api(method, path, **kwargs):
            if path == 'group/claim':
                await asyncio.sleep(0)
                gateway.bump_group('99', '123')
                return {'send': True, 'group_id': '123', 'reply': '你好'}
            if path == 'group/receipt':
                receipts.append(kwargs['payload'])
            return {}

        with patch.object(gateway, 'api', api), patch.object(gateway, 'get_bots', return_value={'99': bot}):
            await gateway.send_candidate(bot, 'race', '123')
        bot.send_group_msg.assert_not_awaited()
        self.assertEqual(receipts[-1]['state'], 'failed')


if __name__ == "__main__":
    unittest.main()
