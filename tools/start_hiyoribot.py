"""启动/停止本机flash SQLite QQ后端，不管理PostgreSQL或GPU服务。"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]
HOME=Path(os.getenv('HIYORI_FLASH_HOME',str(Path.home()/'.local/share/hiyoribot/flash')))
CONFIG=Path(os.getenv('HIYORI_CONFIG',str(HOME/'config.yaml')))

def health(port=8000):
    try:
        with httpx.Client(trust_env=False,timeout=2) as c:
            r=c.get(f'http://127.0.0.1:{port}/health')
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError,ValueError):
        return None

def stop(state,expected):
    if not state or state.get('app')!=expected:
        raise RuntimeError('目标端口不是本项目服务或服务未运行')
    pid=int(state['pid'])
    if os.name=='nt':
        # 校验PID仍是本项目Python，避免PID复用时误停其他应用。
        query=f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object CommandLine | ConvertTo-Json -Compress"
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',query],capture_output=True,text=True,check=True)
        command=(json.loads(result.stdout) or {}).get('CommandLine','').casefold()
        if not ('python' in command and ('bot.py' in command if expected=='HiyoriBotQQ' else 'uvicorn main:app' in command)):
            raise RuntimeError('进程命令不匹配，未停止')
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,check=True)
    else:
        args=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
        if 'python' not in args or ('bot.py' if expected=='HiyoriBotQQ' else 'uvicorn main:app') not in args:
            raise RuntimeError('进程命令不匹配，未停止')
        os.kill(pid,signal.SIGTERM)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop',action='store_true')
    parser.add_argument('--no-browser',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    old=health()
    if args.stop:
        if old:
            stop(old,'HiyoriBot')
        print('QQ后端已停止；原数据库服务不作变更。')
        return
    if old:
        if old.get('version')!='flash.1':
            raise RuntimeError('8000端口仍是旧版，请先停止旧后端')
        print('flash后端已运行')
        return
    if not CONFIG.is_file():
        raise RuntimeError('未找到私有config.yaml，请设置HIYORI_CONFIG')
    HOME.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'HIYORI_CONFIG':str(CONFIG),'PYTHONUTF8':'1'}
    with (HOME/'backend-out.log').open('ab') as out,(HOME/'backend-error.log').open('ab') as err:
        subprocess.Popen([sys.executable,'-m','uvicorn','main:app','--host','127.0.0.1','--port','8000','--no-access-log'],
            cwd=ROOT/'hiyoribot-backend',env=env,stdout=out,stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    until=time.monotonic()+30
    while time.monotonic()<until:
        state=health()
        if state and state.get('version')=='flash.1':
            print('flash后端就绪：127.0.0.1:8000，仅QQ接口，SQLite')
            return
        time.sleep(.5)
    raise RuntimeError('后端未就绪，请查看仓库外backend-error.log')

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error,RuntimeError) else '启动/停止失败，请查看私有日志')
        raise SystemExit(1)
