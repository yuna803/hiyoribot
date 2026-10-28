"""flash验证：默认临时SQLite测试，--live-model额外调用DeepSeek，不发送QQ。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'hiyoribot-backend'
sys.path.insert(0,str(BACKEND))

def live():
    import chat_service
    import group_service as groups
    import group_storage
    import main
    import storage
    import summary_service
    import user_scope
    config=main.resolve_config()
    with tempfile.TemporaryDirectory(prefix='flash-model-check-') as temp:
        home=Path(temp)
        storage.configure(home/'live.sqlite3')
        config=config.model_copy(update={'sqlite_path':str(home/'live.sqlite3'),'profile_home':str(home/'profiles'),
            'qq_profile_home':str(home/'profiles'),'qq_bot_id':'100001','qq_owner_id':'100002'})
        samples=[]
        with user_scope.activate('qq:100002'):
            cfg=user_scope.config_for(config)
            conv=storage.create_conversation('合成试听')
            for text in ('你好，妃爱。','今天有点累，只想随便聊聊。','你现在在忙什么？','帮我看一下Python怎么把字符串转成整数。'):
                result=chat_service.direct_reply({'message':text,'conversation_id':conv['id']},cfg)
                reply=result['reply']
                assert not any(x in reply for x in ('（','内心OS','【状态','递给你','拍了拍','晚饭','菜单','便当','围裙','回家','泡点','坐过来','作息表','学校')), '仍然加入未提供的场景：'+reply
                storage.save_turn(conv['id'],text,reply,agent_messages=result['agent_messages'],recalled_context=result['recalled_context'])
                samples.append({'input':text,'reply':reply})
            # 单独验证真实工具循环，可观察阶段记录，不收集模型内部思考。
            result=chat_service.direct_reply({'message':'请调用时间工具，告诉我现在几点。','conversation_id':conv['id']},cfg)
            assert any(t['name']=='get_current_time' for t in result['tool_calls'])
        rows=[dict(id=i,role='user' if i%2 else 'assistant',content='用户说周末想聊SQLite，助手建议先试试但尚未确认。'+str(i)) for i in range(1,51)]
        summary,covered=summary_service.summarize('',rows,config)
        assert len({r['id'] for r in covered})==50
        with patch('group_service.schedule'):
            for i in range(1,51):
                groups.ingest(groups.Ingest(bot_id='100001',group_id='900001',user_id=str(200000+i%10),message_id=str(i),
                    nickname='合成群友',message='我们正在讨论用SQLite保存聊天。'+str(i),text='我们正在讨论用SQLite保存聊天。'+str(i),sent_at=int(time.time())),config)
        old,batch,identifier=group_storage.begin_batch('100001','900001')
        decision,coverage=groups.observe(old,batch,config)
        group_storage.finish_batch(old,batch,identifier,decision,coverage)
        with user_scope.group('100001','900001'):
            group_reply=chat_service.group_reply('100001','900001',{**decision,'focus':'大家讨论SQLite，简短接话'},config)['reply']
        assert '哥哥' not in group_reply, '无本人参与的群聊不得喊哥哥'
        report={'samples':samples,'group_reply':group_reply,'summary':summary,'covered_messages':len({r['id'] for r in coverage}),
            'tool_loop':True,'qq_messages_sent':0}
        output=Path(os.environ.get('HIYORI_FLASH_HOME',str(Path.home()/'.local/share/hiyoribot/flash')))/'live-model-check.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({'passed':True,'report':str(output),'samples':len(samples),'covered_messages':50,'tool_loop':True,'qq_messages_sent':0},ensure_ascii=False))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model',action='store_true')
    parser.add_argument('--all',action='store_true',help='当前默认即全部flash后端测试')
    args=parser.parse_args()
    if args.live_model:
        live()
    else:
        raise SystemExit(subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=BACKEND,
            env={**os.environ,'PYTHONUTF8':'1'}).returncode)
if __name__=='__main__': main()
