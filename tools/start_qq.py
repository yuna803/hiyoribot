"""启动现有后端和本机NoneBot网关；QQ连接端由NapCat独立运行。"""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.getenv('HIYORI_LOCAL_HOME','E:/unser/q'))/'hiyoribot_runtime/qq'
PYTHON=RUNTIME/'venv/Scripts/python.exe'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEALTH='http://127.0.0.1:8086/health'


def health():
    try:
        with OPENER.open(HEALTH,timeout=2) as response:
            return json.load(response)
    except (OSError,ValueError):
        return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop',action='store_true',help='只停止QQ网关，保留网页、数据库和NapCat')
    args=parser.parse_args()
    state=health()
    if state and state.get('app')!='HiyoriBotQQ':
        raise RuntimeError('8086端口不是本项目的QQ网关')
    if args.stop:
        if not state:
            print('QQ网关未运行。')
            return
        pid=int(state['pid'])
        query=f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object CommandLine | ConvertTo-Json -Compress"
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',query],capture_output=True,text=True,check=True)
        command=json.loads(result.stdout).get('CommandLine','').casefold()
        if str(PYTHON).casefold() not in command or 'bot.py' not in command:
            raise RuntimeError('进程不是本项目的私有网关环境，未停止其他程序')
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],check=True,capture_output=True)
        print('QQ网关已停止，网页、数据库和NapCat保持运行。')
        return
    if not PYTHON.is_file() or not (RUNTIME/'.env').is_file():
        raise RuntimeError('请先安装QQ网关环境并填写私有.env，详见hiyoribot-qq/README.md')
    subprocess.run([sys.executable,str(ROOT/'tools/start_hiyoribot.py'),'--no-browser'],check=True)
    if not state:
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',8086))==0:
                raise RuntimeError('8086端口被其他应用占用')
        with (RUNTIME/'stdout.log').open('ab') as out,(RUNTIME/'stderr.log').open('ab') as err:
            env={**os.environ,'HIYORI_QQ_ENV_FILE':str(RUNTIME/'.env')}
            subprocess.Popen([str(PYTHON),str(ROOT/'hiyoribot-qq/bot.py')],cwd=ROOT/'hiyoribot-qq',env=env,
                             stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=health()
            if state and state.get('app')=='HiyoriBotQQ':
                break
            time.sleep(.5)
        else:
            raise RuntimeError(f'QQ网关未就绪，请检查{RUNTIME}内的日志')
    print('QQ网关已就绪，监听127.0.0.1:8086。')
    print('NapCat已连接。' if state['bot_ids'] else '等待NapCat反向WebSocket连接；QQ未登录时请先扫码。')


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error,RuntimeError) else '启动或停止失败，请检查私有运行日志。')
        raise SystemExit(1)
