"""只在新建的 profile_test_ 临时库验证群旁听；--live-model 会调用已配置模型，不发送QQ。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'hiyoribot-backend'
sys.path.insert(0,str(BACKEND))


def live_probe():
    import group_profile
    import group_service as service
    import group_storage as store
    import main
    import profile_storage
    import storage
    import user_scope
    from psycopg.types.json import Jsonb
    storage.init_db()
    config=main.resolve_config()
    card=json.loads((ROOT/'rolecards/izumi-hiyori.json').read_text(encoding='utf8'))
    storage.update_character(**{k:card[k] for k in ('name','description','personality','background','speaking_style','system_prompt')})
    with TemporaryDirectory(prefix='hiyori-group-profile-') as folder:
        config=config.model_copy(update={'qq_bot_id':'810001','qq_owner_id':'820003','qq_profile_home':folder})
        # 合成公开群，不调用网关，也不使用任何真实群号或用户事实。
        with patch('group_service.schedule'):
            for n in range(1,51):
                body=service.Ingest(bot_id='810001',group_id='830001',user_id=str(820010+n%10),message_id=str(n),
                    nickname='合成参与者',message='我们在聊周末想尝试什么茶，欢迎妃爱推荐。'+str(n),
                    text='我们在聊周末想尝试什么茶，欢迎妃爱推荐。'+str(n),sent_at=int(time.time()))
                service.ingest(body,config)
        old,rows,batch=store.begin_batch('810001','830001')
        decision,coverage=service.observe(old,rows,config)
        store.finish_batch(old,rows,batch,decision,coverage)
        reply=service.generate(old,rows,{**decision,'focus':'群友在聊周末喝茶，简短接一句。'},config)
        store.save_candidate(old,rows,batch,reply,None)
        candidate=store.candidates('810001')['items'][0]['id']
        # 注入一个新合成事件，验证真实API的发送前复核JSON。
        with patch('group_service.schedule'):
            service.ingest(body.model_copy(update={'message_id':'51','message':'我们还在聊喝茶。','text':'我们还在聊喝茶。'}),config)
        review=service.claim(service.Claim(bot_id='810001',candidate_id=candidate),config)
        # 实际模型校验旁听画像，但不依赖模型一定提取事实。
        user='820099'
        with storage.connect() as conn:
            conn.execute('INSERT INTO qq_passive_profile(bot_id,user_id) VALUES(%s,%s)',('810001',user))
            for n in range(10):
                conn.execute('''INSERT INTO qq_group_message(bot_id,group_id,message_id,user_id,nickname,content,
                    plain_text,sent_at,profile_eligible) VALUES('810001','830002',%s,%s,'合成用户',%s,%s,now(),true)''',
                    (str(100+n),user,'我平时喜欢无糖茶，尤其是红茶。'+str(n),'我平时喜欢无糖茶，尤其是红茶。'+str(n)))
            maximum=conn.execute("SELECT max(id) AS n FROM qq_group_message WHERE group_id='830002'").fetchone()['n']
            conn.execute("INSERT INTO qq_group_state(bot_id,group_id,through_id) VALUES('810001','830002',%s)",(maximum,))
        group_profile.update('810001',user,config,service.GroupModel)
        with user_scope.activate('qq:'+user):
            portrait=profile_storage.get()
        result=dict(observation_schema=True,covered_messages=len({r['id'] for r in coverage}),
                    candidate_chars=len(reply),review_schema=isinstance(review['send'],bool),
                    passive_revision=portrait['revision'],passive_affinity=portrait['affinity'],
                    direct_updates=portrait['direct_updates'],qq_messages_sent=0)
        assert result['covered_messages']==50 and result['passive_affinity']==50 and result['direct_updates']==0
        print(json.dumps(result,ensure_ascii=False))


def main():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    import yaml
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model',action='store_true')
    parser.add_argument('--all',action='store_true',help='运行整个后端测试集')
    parser.add_argument('--child-live',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.child_live:
        assert os.getenv('PROFILE_TEST_DATABASE')=='1'
        import storage
        with storage._open_connection() as conn:
            assert conn.execute('SELECT current_database() AS n').fetchone()['n'].startswith('profile_test_')
        live_probe()
        return
    config=yaml.safe_load((BACKEND/'config.yaml').read_text(encoding='utf8'))
    dsn=os.getenv('DATABASE_URL') or config['database_url']
    name='profile_test_'+uuid4().hex[:12]
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            env={**os.environ,'DATABASE_URL':make_conninfo(dsn,dbname=name),'PROFILE_TEST_DATABASE':'1','RUN_DB_TEST':'1','PYTHONUTF8':'1'}
            command=([sys.executable,str(Path(__file__).resolve()),'--child-live'] if args.live_model else
                     [sys.executable,'-m','unittest','discover','-s','tests','-v']+([] if args.all else ['-p','test_group.py']))
            result=subprocess.run(command,cwd=BACKEND,env=env)
            raise SystemExit(result.returncode)
        finally:
            assert name.startswith('profile_test_') and len(name)==25
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__=='__main__':
    main()
