"""SQLite 初始化、状态查询、在线备份及受保护的恢复。"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hiyoribot-backend'))
import storage


def outside_repo(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError('路径必须为绝对路径')
    path = path.resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('数据库和备份必须位于仓库外')
    return path


def readonly(path):
    path = outside_repo(path)
    if not path.is_file():
        raise ValueError('数据库文件不存在')
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check(path):
    path = outside_repo(path)
    if not path.is_file():
        raise ValueError('备份文件不存在')
    manifest = json.loads(path.with_suffix(path.suffix + '.json').read_text(encoding='utf8'))
    if manifest.get('bytes') != path.stat().st_size or manifest.get('sha256') != checksum(path):
        raise ValueError('备份大小或 SHA256 不一致')
    with closing(readonly(path)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('备份完整性检查失败')
    for item in manifest.get('media',[]):
        if not re.fullmatch(r'[a-f0-9]{64}\.(jpg|png|gif|webp)',item['name']):
            raise ValueError('图库清单文件名无效')
        media=path.with_suffix('.media')/item['name']
        if not media.is_file() or media.stat().st_size!=item['bytes'] or checksum(media)!=item['sha256']:
            raise ValueError('图库文件丢失或校验失败')
    return manifest


def backup(database, folder, media_home=None,*,allow_missing=False):
    database, folder = outside_repo(database), outside_repo(folder)
    if not database.is_file():
        raise ValueError('数据库文件不存在')
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    target = folder / f'hiyoribot-{stamp}-{uuid4().hex[:8]}.sqlite3'
    if target == database:
        raise ValueError('备份不能覆盖源数据库')
    partial = target.with_suffix('.sqlite3.partial')
    manifest_path = target.with_suffix(target.suffix + '.json')
    try:
        with closing(readonly(database)) as source, closing(sqlite3.connect(partial)) as destination:
            source.backup(destination)
        partial.replace(target)
        manifest = {'format': 'sqlite', 'created_at': datetime.now(timezone.utc).isoformat(),
                    'bytes': target.stat().st_size, 'sha256': checksum(target)}
        media_home=outside_repo(media_home or database.parent/'media')
        manifest['media']=[]
        manifest['missing_media']=[]
        with closing(readonly(target)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='media_asset'").fetchone():
                for identifier,suffix,state in conn.execute("SELECT id,suffix,meme_state FROM media_asset WHERE state<>'expired'"):
                    name=f'{identifier}.{suffix}'
                    if not re.fullmatch(r'[a-f0-9]{64}\.(jpg|png|gif|webp)',name):
                        raise ValueError('图库文件名无效')
                    source=media_home/name
                    if not source.is_file():
                        if state=='approved':
                            if not allow_missing:
                                raise ValueError('批准图库文件缺失，不能生成完整备份')
                            manifest['missing_media'].append(name)
                        continue
                    destination=target.with_suffix('.media')/name
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copyfile(source,destination)
                    manifest['media'].append({'name':name,'bytes':destination.stat().st_size,'sha256':checksum(destination)})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        check(target)
    except BaseException:
        target.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        partial.unlink(missing_ok=True)
    return target


def restore(path, database, folder, confirmation,media_home=None):
    path, database = outside_repo(path), outside_repo(database)
    folder = outside_repo(folder)
    if path == database or (path.exists() and database.exists() and path.samefile(database)):
        raise ValueError('备份和目标数据库不能是同一文件')
    if confirmation != database.name:
        raise ValueError('--confirm-restore 必须与目标文件名一致')
    if not database.is_file():
        raise ValueError('目标数据库不存在，请先初始化')
    manifest=check(path)
    if manifest.get('missing_media'):
        raise ValueError('不能把缺少批准图库文件的快照作为完整恢复源')
    media_home=outside_repo(media_home or database.parent/'media')
    storage.configure(database)
    with storage.process_guard():
        # 灾难恢复时原图库可能已损坏；仍记录现存文件及缺失清单，不阻止恢复完整备份。
        safety = backup(database, folder,media_home,allow_missing=True)
        # 内容哈希命名的文件先恢复；数据库最后替换，不删除目标中的额外安全副本。
        for item in manifest.get('media',[]):
            media_home.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path.with_suffix('.media')/item['name'],media_home/item['name'])
        with closing(readonly(path)) as source, closing(storage._open_connection()) as target:
            source.backup(target)
    return safety


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'status', 'backup', 'check', 'restore'])
    parser.add_argument('--database', type=Path)
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--file', type=Path)
    parser.add_argument('--confirm-restore', default='')
    parser.add_argument('--media-home',type=Path,help='图库路径；默认数据库同级media目录')
    args = parser.parse_args(argv)
    try:
        if args.action == 'check':
            if not args.file:
                raise ValueError('check 需要 --file')
            check(args.file)
            print('备份 SHA256 和完整性检查通过')
            return 0
        if args.database is None:
            from main import load_config
            args.database = Path(load_config().sqlite_path)
        database = outside_repo(args.database)
        folder = outside_repo(args.folder or database.parent / 'backups')
        storage.configure(database)
        if args.action == 'init':
            storage.init_db()
            print(f'数据库已初始化：{database}')
        elif args.action == 'status':
            with closing(readonly(database)) as conn:
                integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
                tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            print(f'数据库：{database}\n完整性：{integrity}\n数据表：{tables}')
            if integrity != 'ok':
                return 1
        elif args.action == 'backup':
            print(f'备份完成：{backup(database, folder,args.media_home)}')
        else:
            if not args.file:
                raise ValueError('restore 需要 --file')
            print(f'恢复完成，恢复前备份：{restore(args.file, database, folder, args.confirm_restore,args.media_home)}')
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, json.JSONDecodeError):
        # 配置、驱动和路径异常可能包含敏感信息，不输出异常正文。
        print('操作失败：请检查路径、确认名称、数据库锁和备份完整性。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
