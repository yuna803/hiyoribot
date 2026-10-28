"""启动flash后端及NoneBot；NapCat仍独立登录。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from start_hiyoribot import ROOT,HOME,health,stop

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop',action='store_true')
    args=parser.parse_args()
    old=health(8086)
    if args.stop:
        if old: stop(old,'HiyoriBotQQ')
        print('QQ网关已停止，后端与NapCat保留。')
        return
    subprocess.run([sys.executable,str(ROOT/'tools/start_hiyoribot.py')],check=True)
    if old:
        print('QQ网关已运行')
        return
    env_file=Path(os.getenv('HIYORI_QQ_ENV_FILE',str(HOME/'qq.env')))
    if not env_file.is_file():
        raise RuntimeError('未找到网关私有qq.env，请设置HIYORI_QQ_ENV_FILE')
    python=os.getenv('HIYORI_QQ_PYTHON',sys.executable)
    HOME.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'HIYORI_QQ_ENV_FILE':str(env_file),'HIYORI_QQ_STATE_DB':str(HOME/'gateway.sqlite3'),'PYTHONUTF8':'1'}
    with (HOME/'gateway-out.log').open('ab') as out,(HOME/'gateway-error.log').open('ab') as err:
        subprocess.Popen([python,str(ROOT/'hiyoribot-qq/bot.py')],cwd=ROOT/'hiyoribot-qq',env=env,stdout=out,stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    until=time.monotonic()+30
    while time.monotonic()<until:
        state=health(8086)
        if state and state.get('app')=='HiyoriBotQQ':
            print('QQ网关就绪；已连接NapCat。' if state['bot_ids'] else 'QQ网关就绪，等待NapCat连接。')
            return
        time.sleep(.5)
    raise RuntimeError('网关未就绪，请查看仓库外gateway-error.log')

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error,RuntimeError) else '启动/停止失败，请检查私有环境与日志')
        raise SystemExit(1)
