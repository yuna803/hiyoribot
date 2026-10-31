"""临时SQLite＋合成图片，不调用付费API、不发送真实QQ消息。"""
import base64
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

import agent_tools
import agent_service
import followup_service
import followup_storage
import chat_service
import group_service
import group_storage
import main
import media_service as media
import qq_service
import storage
import summary_service
import user_scope

BOT,OWNER,OTHER,GROUP='100001','100002','100003','200001'


def picture(color='red',format='PNG'):
    out=io.BytesIO()
    Image.new('RGB',(40,30),color).save(out,format=format)
    return out.getvalue()


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        storage.configure(self.folder/'state.sqlite3')
        storage.init_db()
        self.cfg=main.AppConfig(api_key='test',qq_enabled=True,qq_bot_id=BOT,qq_owner_id=OWNER,
            qq_backend_token='token',sqlite_path=str(self.folder/'state.sqlite3'),profile_enabled=False,
            profile_home=str(self.folder/'profiles'),qq_profile_home=str(self.folder/'profiles'))
        self.kick_patch=patch.object(media,'kick')
        self.kick=self.kick_patch.start()
        self.addCleanup(patch.stopall)
        self.recognize=patch.object(media,'recognize',return_value=media.Analysis(description='红色笑脸表情',
            text='开心',emotions=['开心'],scenes=['开心聊天'],is_meme=True)).start()

    def upload(self,mid='1',group=GROUP,user=OTHER,raw=None,manual=False):
        return media.ingest(media.Upload(bot_id=BOT,user_id=user,group_id=group,message_id=mid,slot=0,
            data=base64.b64encode(raw or picture()).decode(),manual=manual),self.cfg)['asset_id']

    def manage(self,cmd,user=OWNER,group=''):
        return media.manage(media.Manage(bot_id=BOT,user_id=user,group_id=group,command=cmd),self.cfg)

    def asset(self,identifier):
        with storage.connect() as c:
            return c.execute('SELECT * FROM media_asset WHERE id=?',(identifier,)).fetchone()

    def test_hash_cache_parallel_and_private_scope(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda n:self.upload(mid=str(n)),range(1,5)))
        self.assertEqual(len(set(ids)),1)
        media.work(self.cfg)
        self.upload(mid='5',group='',user=OWNER)
        media.work(self.cfg)
        self.recognize.assert_called_once()
        self.assertEqual(len(media.describe(BOT,OWNER,'','5')),1)
        self.assertEqual(media.describe(BOT,OTHER,'','5'),[])
        self.assertEqual(media.describe(BOT,OTHER,'200002','1'),[])
        self.assertNotIn('user_id',json.loads(self.asset(ids[0])['analysis']))

    def test_singleflight_kick_and_worker_late_wakeup(self):
        self.kick_patch.stop()
        with patch.object(media._executor,'submit') as submit:
            media._running=False;media._wake=False
            for _ in range(5):media.kick(self.cfg)
            self.assertEqual(submit.call_count,1)
            self.assertTrue(media._wake)
        media._running=False;media._wake=False

    def test_daily_100_101_crossday_and_cache(self):
        with storage.connect(write=True) as c:
            c.execute('INSERT INTO media_daily(day,attempts) VALUES(?,99)',(media.today(),))
        first=self.upload()
        second=self.upload('2',raw=picture('blue'))
        media.work(self.cfg)
        self.assertEqual(self.recognize.call_count,1)
        self.assertEqual({self.asset(first)['state'],self.asset(second)['state']},{'ready','budget'})
        self.upload('3');media.work(self.cfg)
        self.assertEqual(self.recognize.call_count,1)
        with patch.object(media,'today',return_value='2099-01-01'):
            media.work(self.cfg)  # 达限旧队列不会跨日自动补跑。
            self.assertEqual(self.recognize.call_count,1)
            self.manage('/识图 重试');media.work(self.cfg)
            self.assertEqual(self.recognize.call_count,2)

    def test_auth_balance_pause_and_manual_recovery(self):
        one=self.upload();two=self.upload('2',raw=picture('blue'))
        err=RuntimeError();err.status_code=402
        self.recognize.side_effect=err
        media.work(self.cfg)
        media.work(self.cfg)
        self.assertEqual(self.recognize.call_count,1)
        with storage.connect() as c:
            self.assertEqual(c.execute('SELECT attempts FROM media_daily').fetchone()['attempts'],1)
        self.assertIn('额度不足',self.manage('/表情 状态')['reply'])
        self.recognize.side_effect=None
        self.manage('/识图 重试');media.work(self.cfg)
        self.assertEqual(self.asset(one)['state'],'ready')
        self.assertEqual(self.asset(two)['state'],'ready')

    def test_invalid_model_output_does_not_make_candidate(self):
        identifier=self.upload()
        self.recognize.side_effect=ValueError('bad json')
        media.work(self.cfg)
        self.assertEqual(self.asset(identifier)['state'],'failed')
        self.assertEqual(self.asset(identifier)['meme_state'],'none')

    def test_animation_frames_decode_limits_and_original_preserved(self):
        out=io.BytesIO()
        frames=[Image.new('RGB',(16,16),c) for c in ('red','green','blue')]
        frames[0].save(out,format='GIF',save_all=True,append_images=frames[1:],duration=50,loop=0)
        raw=out.getvalue()
        meta,images=media.inspect_image(raw,extract=True)
        self.assertEqual(meta['frames'],3)
        self.assertEqual(len(images),3)
        identifier=self.upload(raw=raw)
        self.assertEqual(media.path_for(self.asset(identifier),self.cfg).read_bytes(),raw)
        with self.assertRaises(ValueError):media.inspect_image(b'x'*(media.MAX_BYTES+1))
        with self.assertRaises(ValueError):media.inspect_image(b'not an image')
        with patch.object(media,'MAX_PIXELS',100):
            with self.assertRaises(ValueError):media.inspect_image(picture())
        out=io.BytesIO()
        frames[0].save(out,format='WEBP',save_all=True,append_images=frames[1:],duration=50,loop=0)
        self.assertEqual(len(media.inspect_image(out.getvalue(),extract=True)[1]),3)

    def test_only_meme_group_or_owner_manual_enters_review(self):
        private=self.upload(group='')
        media.work(self.cfg)
        self.assertEqual(self.asset(private)['meme_state'],'none')
        self.upload(mid='2',group='',manual=True)  # 他人的私聊提交不能提权。
        self.assertEqual(self.asset(private)['meme_state'],'none')
        self.upload(mid='3',group='',user=OWNER,manual=True)
        self.assertEqual(self.asset(private)['meme_state'],'pending')
        with self.assertRaises(HTTPException):self.manage('/表情 通过 '+private,user=OTHER)
        with self.assertRaises(HTTPException):self.manage('/表情 通过 '+private,group=GROUP)
        other=self.upload(mid='4',raw=picture('blue'))
        self.recognize.return_value=media.Analysis(description='普通照片',is_meme=False)
        media.work(self.cfg)
        self.assertEqual(self.asset(other)['meme_state'],'none')

    def test_review_tools_scope_one_meme_and_receipt_once(self):
        identifier=self.upload();media.work(self.cfg)
        with user_scope.group(BOT,GROUP):
            selected=[]
            self.assertIn('error',media.select(identifier,self.cfg,selected))
            self.manage('/表情 标签 '+identifier+' 开心 笑脸')
            self.manage('/表情 通过 '+identifier)
            self.assertEqual(media.search('开心','开心',self.cfg)[0]['asset_id'],identifier)
            self.assertTrue(media.select(identifier,self.cfg,selected)['queued'])
            self.assertIn('error',media.select(identifier,self.cfg,selected))
        with storage.connect(write=True) as c:
            media.register_delivery(c,'d',BOT,OTHER,GROUP,selected)
        wrong=media.Scope(bot_id=BOT,user_id=OTHER,group_id='200002')
        with self.assertRaises(HTTPException):media.file_for(identifier,wrong,self.cfg,delivery_id='d')
        receipt=media.Receipt(bot_id=BOT,user_id=OTHER,group_id=GROUP,delivery_id='d',state='sent',message_id='99')
        media.receipt(receipt);media.receipt(receipt)
        self.assertEqual(self.asset(identifier)['uses'],1)
        with user_scope.group(BOT,GROUP):self.assertEqual(media.search('开心','',self.cfg),[])
        with user_scope.group(BOT,'200002'):self.assertEqual(len(media.search('开心','',self.cfg)),1)

    def test_photo_wait_unknown_recall_and_retention(self):
        self.assertEqual(media.describe(BOT,OWNER,'','9',expected=True,wait=.01),[{'status':'图片未识别'}])
        identifier=self.upload();media.work(self.cfg)
        body=group_service.Recall(bot_id=BOT,group_id=GROUP,message_id='1')
        group_storage.recall(body)
        self.assertEqual(media.describe(BOT,OTHER,GROUP,'1'),[])
        with self.assertRaises(HTTPException):self.upload()
        self.upload('2')
        with storage.connect(write=True) as c:
            c.execute('UPDATE media_asset SET last_seen=unixepoch()-604801')
        media.cleanup(self.cfg)
        self.assertFalse(media.path_for(self.asset(identifier),self.cfg).exists())
        self.upload('3')
        self.assertEqual(self.asset(identifier)['state'],'queued')

    def test_approved_image_survives_expiration_and_delete_disables(self):
        identifier=self.upload();media.work(self.cfg)
        self.manage('/表情 通过 '+identifier)
        with storage.connect(write=True) as c:c.execute('UPDATE media_asset SET last_seen=1')
        media.cleanup(self.cfg)
        self.assertTrue(media.path_for(self.asset(identifier),self.cfg).exists())
        self.manage('/表情 删除 '+identifier)
        with user_scope.group(BOT,GROUP):self.assertEqual(media.search('开心','',self.cfg),[])

    def test_image_context_separate_from_raw_text_and_profile_material(self):
        self.upload();media.work(self.cfg)
        group_storage.ingest(group_service.Ingest(bot_id=BOT,user_id=OTHER,group_id=GROUP,message_id='1',
            message='[image]',sent_at=1,has_images=True))
        with user_scope.activate('qq:'+OTHER),user_scope.expose_group(BOT,GROUP):
            cfg=self.cfg.model_copy(update={'qq_message_id':'1','qq_has_images':True,'media_wait_seconds':0})
            messages,_=chat_service.model_messages('看看图',cfg,group=(BOT,GROUP),initiator=OTHER)
        self.assertIn('红色笑脸',messages[0]['content'])
        with storage.connect() as c:
            raw=c.execute('SELECT * FROM qq_group_message WHERE message_id=\'1\'').fetchone()
        self.assertEqual(raw['content'],'[image]')
        self.assertFalse(raw['profile_eligible'])
        rows=media.annotate_rows([raw],BOT,GROUP)
        self.assertIn('images',list(summary_service.fragments(rows))[0][0])

    def test_private_links_and_pure_meme_reply_compatibility(self):
        identifier=self.upload(group='',user=OWNER,manual=True);media.work(self.cfg)
        self.manage('/表情 通过 '+identifier)
        result={'reply':'','attachments':[media.attachment(self.asset(identifier))],'agent_messages':[],'recalled_context':{}}
        request=qq_service.QQChatRequest(bot_id=BOT,user_id=OWNER,message_id='1',message='[image]',has_images=True,media_wait_seconds=0)
        with patch('profile_service.schedule_safely'),patch('summary_service.schedule'):
            response=qq_service.chat(request,self.cfg,lambda *a:result)
            duplicate=qq_service.chat(request,self.cfg,lambda *a:self.fail('重复模型调用'))
        self.assertEqual(response,duplicate)
        self.assertEqual(response['attachments'][0]['asset_id'],identifier)
        with storage.connect() as c:
            rows=c.execute('SELECT id,conversation_id,role,content FROM message').fetchall()
        annotated=media.annotate_rows(rows,BOT)
        self.assertIn('images',annotated[0])
        self.assertIn('表情#',annotated[1]['content'])

    def test_http_auth_and_owner_scope(self):
        with patch('main.resolve_config',return_value=self.cfg),patch('main.load_config',return_value=self.cfg):
            client=TestClient(main.app)
            body={'bot_id':BOT,'user_id':OWNER,'command':'/表情 状态'}
            self.assertEqual(client.post('/integrations/qq/media/manage',json=body).status_code,403)
            body['user_id']=OTHER
            self.assertEqual(client.post('/integrations/qq/media/manage',json=body,headers={'Authorization':'Bearer token'}).status_code,403)

    def test_agent_tool_loop_pure_image_and_invalid_path(self):
        identifier=self.upload();media.work(self.cfg);self.manage('/表情 通过 '+identifier)
        sequence=[{'role':'assistant','content':'','reasoning_content':'','tool_calls':[
            {'id':'tool1','type':'function','function':{'name':'send_meme','arguments':json.dumps({'meme_id':identifier})}}]},
            {'role':'assistant','content':'','reasoning_content':''}]
        def model_round(*args,**kw):
            if False:yield
            return sequence.pop(0)
        with user_scope.group(BOT,GROUP),patch.object(agent_service,'_model_round',side_effect=model_round):
            events=list(agent_service.run(None,'test',[{'role':'system','content':'合成人设'},{'role':'user','content':'开心'}],None,'妃爱',{},stream=False,tools_enabled=True,config=self.cfg))
        complete=[data for kind,data in events if kind=='complete'][0]
        self.assertEqual(complete['reply'],'')
        self.assertEqual(len(complete['attachments']),1)
        self.assertIn('error',agent_tools.execute('send_meme','{"meme_id":"../../secret"}',None,'妃爱',config=self.cfg,media_output=[]))

    def test_pure_image_followup_ready_and_unknown_paths(self):
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=GROUP,message_id='-1',
            message='今天开心吗？',opens_window=True,segment_message_ids=['-1']))
        with patch('group_service.schedule'):
            route=group_service.ingest(group_service.Ingest(bot_id=BOT,user_id=OTHER,group_id=GROUP,
                message_id='1',message='[image]',has_images=True,sent_at=1),self.cfg)
        request=followup_service.Request(bot_id=BOT,group_id=GROUP,message_id='1',window_version=route['window_version'],
            has_images=True,media_wait_seconds=0)
        with patch('followup_service.llm_runtime.json_output') as judge:
            self.assertFalse(followup_service.prepare(request,self.cfg)['send'])
            judge.assert_not_called()
        # 新窗口内，已完成的图片描述可交给相关性判断，单消息只消耗一个名额。
        group_storage.bot_message(group_service.BotMessage(bot_id=BOT,group_id=GROUP,message_id='-2',
            message='用表情回答也可以',opens_window=True,segment_message_ids=['-2']))
        with patch('group_service.schedule'):
            route=group_service.ingest(group_service.Ingest(bot_id=BOT,user_id=OTHER,group_id=GROUP,
                message_id='2',message='[image]',has_images=True,sent_at=1),self.cfg)
        self.upload('2');media.work(self.cfg)
        request=request.model_copy(update={'message_id':'2','window_version':route['window_version']})
        with patch('followup_service.llm_runtime.json_output',return_value=followup_service.Decision(send=False,reason='沉默')) as judge:
            self.assertFalse(followup_service.prepare(request,self.cfg)['send'])
            self.assertEqual(judge.call_args.args[1]['current_images'][0]['status'],'已识别')
        with storage.connect(write=True) as c:
            self.assertEqual(followup_storage.window(c,BOT,GROUP)['remaining'],4)


if __name__=='__main__':unittest.main()
