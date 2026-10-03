"""一键启动本机数据库、Ollama 和网页；日志与 PID 留在仓库外。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import webbrowser

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOME = Path(os.getenv("HIYORI_LOCAL_HOME", "E:/unser/q"))
RUNTIME = LOCAL_HOME / "hiyoribot_runtime"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url: str):
    try:
        with OPENER.open(url, timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def start_process(arguments: list[str], name: str, env: dict | None = None) -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / f"{name}-stdout.log").open("ab") as out, (RUNTIME / f"{name}-stderr.log").open("ab") as err:
        process = subprocess.Popen(arguments, cwd=ROOT / "hiyoribot-backend", env=env,
                                   stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return process.pid


def wait_ready(url: str, seconds: int = 45) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        data = get_json(url)
        if data:
            return data
        time.sleep(0.5)
    raise RuntimeError(f"服务未就绪，请查看 {RUNTIME} 下的日志")


def start_database(config: dict) -> None:
    url = urlsplit(os.getenv("DATABASE_URL") or config.get("database_url", ""))
    if not url.hostname:
        raise ValueError("请先配置数据库连接")
    binary = LOCAL_HOME / "postgresql/Library/bin"
    if url.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("使用配置中的远程数据库。")
        return
    ready = [str(binary / "pg_isready.exe"), "-h", url.hostname, "-p", str(url.port or 5432)]
    if subprocess.run(ready, capture_output=True).returncode == 0:
        print("数据库已运行。")
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    print("正在启动数据库；异常退出后可能需要恢复，请稍等。", flush=True)
    result = subprocess.run([str(binary / "pg_ctl.exe"), "-D", str(LOCAL_HOME / "postgresql-data"),
                             "-l", str(RUNTIME / "postgresql.log"), "-w", "-t", "90", "start"],
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=100,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(f"数据库未就绪，请查看 {RUNTIME / 'postgresql.log'}")
    print("数据库已就绪。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--database-only", action="store_true")
    parser.add_argument("--stop", action="store_true", help="停止本项目网页服务，保留数据库和共享模型服务")
    args = parser.parse_args()
    if args.stop:
        health = get_json("http://127.0.0.1:8000/health")
        if not health or health.get("app") != "HiyoriBot":
            print("本项目网页服务未运行，未停止其他进程。")
            return
        pid = int(health["pid"])
        query = f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe","-NoProfile","-Command",query],capture_output=True,text=True,encoding="utf-8",check=True)
        process = json.loads(result.stdout)
        command = process.get("CommandLine","").casefold()
        executables = [ROOT/"hiyoribot-backend/venv/Scripts/python.exe",ROOT/"hiyoribot-backend/.venv/Scripts/python.exe"]
        if "-m uvicorn main:app" not in command or not any(str(path).casefold() in command for path in executables):
            raise ValueError("网页进程不是当前项目的 Python，未停止其他进程")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True, capture_output=True)
        print("网页服务已停止。数据库和 Ollama 保持运行。")
        return
    config = yaml.safe_load((ROOT / "hiyoribot-backend/config.yaml").read_text(encoding="utf-8")) or {}
    start_database(config)
    if args.database_only:
        return
    if config.get("provider") == "local":
        url = urlsplit(config.get("local_base_url", "http://127.0.0.1:11434/v1"))
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("本地模型必须绑定本机 HTTP 地址")
        endpoint = f"http://{url.netloc}/api/version"
        if not get_json(endpoint):
            print("正在启动本机模型服务。", flush=True)
            model_home = LOCAL_HOME / "hiyori_llm"
            env = {**os.environ, "OLLAMA_HOST": url.netloc, "OLLAMA_MODELS": str(model_home / "models"),
                   "OLLAMA_CONTEXT_LENGTH": "8192", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
                   "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NO_CLOUD": "1"}
            start_process([str(model_home / "ollama/ollama.exe"), "serve"], "ollama", env)
            wait_ready(endpoint)
    health = get_json("http://127.0.0.1:8000/health")
    if health and health.get("app") != "HiyoriBot":
        raise ValueError("8000 端口已有其他应用，请先处理端口冲突")
    if not health:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1",8000)) == 0:
                raise RuntimeError("8000 端口已有应用，但健康检查未通过，请先检查运行日志或端口冲突")
        print("正在启动网页服务。", flush=True)
        start_process([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"], "web")
        health = wait_ready("http://127.0.0.1:8000/health")
    if health.get("app") != "HiyoriBot" or not health.get("database"):
        raise RuntimeError("网页或数据库检查未通过，请查看运行日志")
    (RUNTIME / "services.json").write_text(json.dumps({"web_pid": health["pid"]}), encoding="utf-8")
    print("启动完成：http://127.0.0.1:8000/")
    if not args.no_browser:
        webbrowser.open("http://127.0.0.1:8000/")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, (ValueError, RuntimeError)) else "启动失败，请检查安装路径和运行日志。")
        raise SystemExit(1)
