"""本机数据库备份、校验和事务恢复；凭据只通过子进程环境传递。"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import unquote, urlsplit, parse_qs
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.getenv("HIYORI_LOCAL_HOME", "E:/unser/q"))


def connection(database: str | None = None) -> tuple[dict, str]:
    path = ROOT / "hiyoribot-backend/config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    config = config or {}
    url = urlsplit(os.getenv("DATABASE_URL") or config.get("database_url", ""))
    if url.scheme not in ("postgresql", "postgres") or not url.hostname or not url.username:
        raise ValueError("请配置有效的 PostgreSQL 连接")
    original = unquote(url.path.lstrip("/"))
    env = {**os.environ, "PGHOST": url.hostname, "PGPORT": str(url.port or 5432),
           "PGUSER": unquote(url.username), "PGDATABASE": database or original,
           "PGCONNECT_TIMEOUT": "5"}
    if url.password:
        env["PGPASSWORD"] = unquote(url.password)
    parameters = parse_qs(url.query)
    variables = {"sslmode":"PGSSLMODE","sslrootcert":"PGSSLROOTCERT","sslcert":"PGSSLCERT",
                 "sslkey":"PGSSLKEY","target_session_attrs":"PGTARGETSESSIONATTRS","application_name":"PGAPPNAME"}
    for name,values in parameters.items():
        if name not in variables or len(values) != 1:
            raise ValueError("连接串包含备份工具尚不支持的参数，请显式调整连接配置")
        env[variables[name]] = values[0]
    return env, original


def outside_repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("数据库备份必须放在代码仓库外")
    return path


def run(executable: Path, arguments: list[str], env: dict, diagnostic: Path) -> bytes:
    if not executable.is_file():
        raise ValueError("未找到 PostgreSQL 备份工具，请配置 --pg-bin")
    result = subprocess.run([str(executable), *arguments], env=env, capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_bytes(result.stderr)
        raise RuntimeError(f"数据库工具失败；诊断保存在 {diagnostic}")
    return result.stdout


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup(folder: Path, pg_bin: Path, env: dict) -> Path:
    folder = outside_repo(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    target = folder / f"hiyoribot-{stamp}-{uuid4().hex[:8]}.dump"
    partial = target.with_suffix(".dump.partial")
    try:
        run(pg_bin / "pg_dump.exe", ["-Fc", "-f", str(partial)],
            env, target.with_suffix(".error.log"))
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)
    metadata = {"database": env["PGDATABASE"], "bytes": target.stat().st_size,
                "sha256": checksum(target), "created_at": stamp, "format": "postgres-custom"}
    target.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    check(target, pg_bin, env)
    return target


def check(path: Path, pg_bin: Path, env: dict) -> dict:
    path = outside_repo(path)
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    if path.stat().st_size != metadata["bytes"] or checksum(path) != metadata["sha256"]:
        raise ValueError("备份大小或 SHA256 不一致，拒绝恢复")
    with path.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise ValueError("备份不是 PostgreSQL 自定义归档")
    listing = run(pg_bin / "pg_restore.exe", ["--list", str(path)], env, path.with_suffix(".error.log"))
    return metadata


def restore(path: Path, folder: Path, pg_bin: Path, env: dict, confirmation: str, original: str) -> Path:
    check(path, pg_bin, env)
    if confirmation != env["PGDATABASE"]:
        raise ValueError("--confirm-restore 必须写目标数据库名称")
    if env["PGDATABASE"] == original:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:8000/health", timeout=2) as response:
                running = json.load(response).get("app") == "HiyoriBot"
        except (OSError, ValueError):
            running = False
        if running:
            raise ValueError("请先用停止脚本关闭网页服务，再恢复数据库")
    # 恢复前再备份目标；--single-transaction 使恢复失败时整个事务回滚。
    safety = backup(folder, pg_bin, env)
    run(pg_bin / "pg_restore.exe", ["--clean", "--if-exists", "--no-owner",
                                   "--single-transaction", "--exit-on-error", "--dbname", env["PGDATABASE"],
                                   str(outside_repo(path))],
        env, outside_repo(folder) / "restore.error.log")
    return safety


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "check", "restore"])
    parser.add_argument("--file", type=Path)
    parser.add_argument("--folder", type=Path, default=DEFAULT_HOME / "hiyoribot_backups")
    parser.add_argument("--pg-bin", type=Path, default=DEFAULT_HOME / "postgresql/Library/bin")
    parser.add_argument("--database-name", help="仅用于明确指定目标数据库，例如恢复验证的临时库")
    parser.add_argument("--confirm-restore", default="")
    parser.add_argument("--interactive",action="store_true",help="交互确认目标，恢复时停网页，完成后重启网页")
    args = parser.parse_args()
    restart_web = False
    try:
        environment, original_name = connection(args.database_name)
        if args.action == "backup":
            print(f"备份完成：{backup(args.folder, args.pg_bin, environment)}")
        elif args.file is None:
            parser.error("check/restore 需要 --file")
        elif args.action == "check":
            check(args.file, args.pg_bin, environment)
            print("归档与 SHA256 校验通过。")
        else:
            if args.interactive:
                check(args.file,args.pg_bin,environment)
                expected = environment["PGDATABASE"]
                print(f"即将恢复到数据库 {expected}，恢复前会再备份当前数据。")
                args.confirm_restore = input("请输入目标数据库名称以确认：").strip()
                if args.confirm_restore != expected:
                    raise ValueError("名称不符，恢复已取消")
                subprocess.run([sys.executable,str(ROOT/"tools/start_hiyoribot.py"),"--stop"],check=True)
                restart_web = True
            safety = restore(args.file, args.folder, args.pg_bin, environment, args.confirm_restore, original_name)
            print(f"恢复完成。恢复前的备份：{safety}")
    except (ValueError, RuntimeError) as error:
        print(str(error))
        raise SystemExit(1)
    except (OSError, subprocess.SubprocessError):
        # 不把连接异常中的凭据输出到终端；详细数据库诊断仅存在仓库外。
        print("操作失败，请核对参数、目标数据库、备份校验和仓库外诊断日志。")
        raise SystemExit(1)
    finally:
        if restart_web:
            subprocess.run([sys.executable,str(ROOT/"tools/start_hiyoribot.py"),"--no-browser"])
