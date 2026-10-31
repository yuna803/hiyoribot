"""模拟OneBot；不下载QQ真实图片、不向用户或群发送消息。"""
import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from test_gateway import event
import test_gateway as fixtures
import hiyori_chat as gateway
import qq_media
from nonebot.adapters.onebot.v11 import MessageSegment


class MediaGatewayTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.FollowupGatewayTests.asyncSetUp
    async def asyncTearDown(self):
        await qq_media.disconnect('99')
        await fixtures.FollowupGatewayTests.asyncTearDown(self)

    async def test_text_image_and_pure_image_each_open_once(self):
        attachment={'asset_id':'a'*64,'description':'开心笑脸','mime':'image/gif'}
        for text in ('你好',''):
            self.calls.clear();self.bot.send.reset_mock()
            self.bot.send.side_effect=None
            self.bot.send.return_value={'message_id':1002}
            with patch.object(qq_media,'get_file',new=AsyncMock(return_value=b'original gif bytes')):
                self.assertTrue(await gateway.send_chat_reply(self.bot,event(group=True),text,[attachment],'d'))
            opened=[p for path,p in self.calls if path=='group/bot-message' and p.get('opens_window')]
            self.assertEqual(len(opened),1)
            self.assertEqual(len(opened[0]['segment_message_ids']),2 if text else 1)
            receipt=[p for path,p in self.calls if path=='media/receipt']
            self.assertEqual(receipt[0]['state'],'sent')
            segment=self.bot.send.call_args.args[1]
            self.assertTrue(segment.data['file'].startswith('base64://'))

    async def test_partial_failure_uncertain_ack_never_opens_or_retries(self):
        attachment={'asset_id':'a'*64,'description':'开心','mime':'image/png'}
        for outcome in (asyncio.TimeoutError(),{},RuntimeError('network failed')):
            self.calls.clear();self.bot.send.reset_mock()
            self.bot.send.side_effect=[{'message_id':1001},outcome]
            with patch.object(qq_media,'get_file',new=AsyncMock(return_value=b'bytes')):
                self.assertFalse(await gateway.send_chat_reply(self.bot,event(group=True),'文字',[attachment],'d'))
            self.assertEqual(self.bot.send.await_count,2)
            self.assertFalse(any(path=='group/bot-message' and p.get('opens_window') for path,p in self.calls))
            self.assertEqual([p for path,p in self.calls if path=='media/receipt'][0]['state'],'uncertain')

    async def test_image_fetch_failure_no_error_sent_to_group(self):
        self.calls.clear()
        with patch.object(qq_media,'get_file',new=AsyncMock(side_effect=ValueError('file unavailable'))):
            self.assertFalse(await gateway.send_chat_reply(self.bot,event(group=True),'',
                [{'asset_id':'a'*64,'description':'开心'}],'d'))
        self.bot.send.assert_not_awaited()
        self.assertEqual([p for path,p in self.calls if path=='media/receipt'][0]['state'],'failed')

    async def test_structured_images_quote_and_manual_scope(self):
        ev=event(group=True,segments=[MessageSegment.image('qq-file'),MessageSegment.reply(42)])
        self.bot.get_msg=AsyncMock(return_value={'message':[{'type':'image','data':{'file':'quoted-file'}}]})
        api=AsyncMock()
        with patch.object(qq_media,'image_bytes',new=AsyncMock(return_value=b'picture')):
            await qq_media.collect(self.bot,ev,api)
        calls=[c.kwargs['payload'] for c in api.call_args_list]
        self.assertEqual(len(calls),2)
        self.assertFalse(calls[0]['referenced'])
        self.assertTrue(calls[1]['referenced'])
        self.assertEqual(calls[1]['reference_message_id'],'42')
        self.assertEqual(base64.b64decode(calls[1]['data']),b'picture')
        self.assertFalse(calls[1]['manual'])
        self.assertEqual(qq_media.picture_segments(event(segments=[MessageSegment.text('https://qpic.cn/a')]).original_message),[])

    async def test_download_refresh_failure_and_safe_source_urls(self):
        self.assertFalse(qq_media.permitted_url('http://127.0.0.1/secret'))
        self.assertFalse(qq_media.permitted_url('https://qq.com.evil.test/a'))
        self.assertFalse(qq_media.permitted_url('file:///etc/passwd'))
        self.assertTrue(qq_media.permitted_url('https://multimedia.nt.qq.com.cn/download?token=private'))
        self.bot.get_image=AsyncMock(return_value={'url':'https://qpic.cn/new'})
        segment=MessageSegment.image('qq-file');segment.data['url']='https://qpic.cn/expired'
        with patch.object(qq_media,'download',new=AsyncMock(side_effect=[ValueError(),b'image'])) as download:
            self.assertEqual(await qq_media.image_bytes(self.bot,segment),b'image')
            self.assertEqual(download.await_count,2)

    async def test_own_image_not_collected_or_routed_to_agent(self):
        with patch.object(qq_media,'start') as collect:
            await gateway.process_message(self.bot,event(user_id=99,group=True,segments=[MessageSegment.image('x')]))
        collect.assert_not_called()

    async def test_followup_image_flag_and_pure_meme_send(self):
        self.reply=''
        attachment={'asset_id':'a'*64,'description':'笑脸'}
        async def api(method,path,*,payload=None,**kw):
            self.calls.append((path,payload))
            if path=='group/followup' and payload.get('action') in (None,'claim'):
                return {'send':True,'reply':'','attachments':[attachment],'delivery_id':'d'}
            return {}
        q=asyncio.Queue();q.put_nowait((event(group=True,segments=[MessageSegment.image('x')]),3))
        with patch.object(gateway,'api',side_effect=api),patch.object(qq_media,'get_file',new=AsyncMock(return_value=b'bytes')):
            await gateway.followup_worker(self.bot,('99','123'),q)
        prepared=[p for path,p in self.calls if path=='group/followup' and not p.get('action')]
        self.assertTrue(prepared[0]['has_images'])
        self.assertTrue(any(path=='group/bot-message' and p.get('opens_window') for path,p in self.calls))


if __name__=='__main__':unittest.main()
