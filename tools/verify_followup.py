"""用临时SQLite检查真实DeepSeek跟聊，不连接QQ、不向QQ发送消息。"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'hiyoribot-backend'))


def live():
    import followup_service as service
    import group_service
    import group_storage
    import main
    import storage
    config=main.resolve_config()
    report={'cases':[],'qq_messages_sent':0}
    with tempfile.TemporaryDirectory(prefix='hiyori-followup-check-') as tmp:
        home=Path(tmp)
        config=config.model_copy(update={'sqlite_path':str(home/'test.sqlite'),'profile_home':str(home/'profiles'),
            'qq_profile_home':str(home/'profiles'),'profile_enabled':False,'qq_bot_id':'100001','qq_owner_id':'100002'})
        storage.configure(config.sqlite_path)
        for n,(text,expected,food,third_person) in enumerate([
            ('轻松日常吧，下班已经够费脑了。',True,False,False),
            ('小王，你刚刚说的解谜游戏叫什么？',False,False,False),
            ('明天服务器要升级，先备份数据库。',False,False,False),
            ('你喜欢吃什么？',True,True,False),
            ('饺子呢？',True,True,False),
            ('那我给你推荐冰淇淋拌面，要尝尝吗？',True,True,True),
            ('小王，刚才问你的午饭呢？',False,True,False),
        ],1):
            group=str(900000+n)
            with patch('group_service.schedule'):
                group_service.ingest(group_service.Ingest(bot_id='100001',group_id=group,user_id='100002',
                    message_id=str(1000+n),message='妃爱，午饭有什么建议？' if food else '妃爱，周末玩什么？',
                    text='妃爱，午饭有什么建议？' if food else '妃爱，周末玩什么？',sent_at=int(time.time())),config)
            group_storage.bot_message(group_service.BotMessage(bot_id='100001',group_id=group,message_id=str(-n),
                message='你想吃什么？我想吃清淡一点的。' if food else '周末想玩点游戏，你更喜欢轻松日常还是解谜？',
                opens_window=True,reply_to_message_id=str(1000+n)))
            with patch('group_service.schedule'):
                if food:
                    for mid,body in [(2000+n,'我刚看完一个视频'),(3000+n,'')]:
                        group_service.ingest(group_service.Ingest(bot_id='100001',group_id=group,user_id='100003',
                            message_id=str(mid),message=body or '[image]',text=body,sent_at=int(time.time())),config)
                route=group_service.ingest(group_service.Ingest(bot_id='100001',group_id=group,user_id='100004' if third_person else '100002',
                    message_id=str(n),message=text,text=text,sent_at=int(time.time())),config)
            started=time.monotonic()
            request=service.Request(bot_id='100001',group_id=group,message_id=str(n),window_version=route['window_version'])
            response=service.handle(request,config)
            with storage.connect() as c:
                job=c.execute('''SELECT j.state,j.reason FROM qq_followup_job j JOIN qq_group_message m ON m.id=j.message_row_id
                    WHERE j.bot_id=? AND j.group_id=? AND m.message_id=?''',('100001',group,str(n))).fetchone()
            item={'input':text,'expected':expected,'send':response['send'],'reply':response.get('reply'),
                  'state':job['state'],'reason':job['reason'],'seconds':round(time.monotonic()-started,2)}
            report['cases'].append(item)
            if response['send']:
                with patch('profile_service.schedule_safely'):
                    permit=service.handle(request.model_copy(update={'action':'claim'}),config)
                assert permit['send'],'通过判断的草稿未获得一次性许可'
                assert not service.handle(request.model_copy(update={'action':'claim'}),config)['send']
        report['passed']=all(r['send']==r['expected'] for r in report['cases'])
    out=Path(os.environ.get('HIYORI_FLASH_HOME',str(Path.home()/'.local/share/hiyoribot/flash')))/'followup-model-check.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'],'cases':len(report['cases']),'qq_messages_sent':0,'report':str(out)},ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model',action='store_true',required=True,help='会产生少量DeepSeek API费用')
    parser.parse_args()
    live()
