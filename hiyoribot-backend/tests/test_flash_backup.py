import importlib.util
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import storage
import media_service
import base64
import io
from PIL import Image
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('database_backup', ROOT / 'tools' / 'database_backup.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / 'state.sqlite3'
        self.folder = Path(self.tmp.name) / 'backups'
        self.assertEqual(tool.main(['init', '--database', str(self.database)]), 0)

    def test_status_missing_and_wal_backup_restore(self):
        self.assertEqual(tool.main(['status', '--database', str(self.database)]), 0)
        missing = Path(self.tmp.name) / 'missing.sqlite3'
        self.assertEqual(tool.main(['status', '--database', str(missing)]), 1)
        self.assertFalse(missing.exists())
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO conversation(id,subject_key,title) VALUES('test','web:owner','wal record')")
        writer.commit()
        self.assertTrue(Path(str(self.database) + '-wal').exists())
        saved = tool.backup(self.database, self.folder)
        self.assertEqual(tool.main(['check', '--file', str(saved)]), 0)
        with closing(sqlite3.connect(saved)) as conn:
            self.assertEqual(conn.execute('SELECT title FROM conversation').fetchone()[0], 'wal record')
        writer.execute("UPDATE conversation SET title='after backup' WHERE id='test'")
        writer.commit()
        writer.close()
        with self.assertRaises(ValueError):
            tool.restore(saved, self.database, self.folder, 'wrong')
        safety = tool.restore(saved, self.database, self.folder, self.database.name)
        tool.check(safety)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT title FROM conversation').fetchone()[0], 'wal record')
        with closing(sqlite3.connect(safety)) as conn:
            self.assertEqual(conn.execute('SELECT title FROM conversation').fetchone()[0], 'after backup')

    def test_tampered_backup_and_same_file_rejected(self):
        saved = tool.backup(self.database, self.folder)
        with saved.open('ab') as stream:
            stream.write(b'tampered')
        with self.assertRaises(ValueError):
            tool.check(saved)
        with self.assertRaises(ValueError):
            tool.restore(saved, self.database, self.folder, self.database.name)
        with self.assertRaises(ValueError):
            tool.restore(self.database, self.database, self.folder, self.database.name)

    def test_running_process_blocks_restore(self):
        saved = tool.backup(self.database, self.folder)
        script = ("import sys,storage; storage.configure(sys.argv[1]); "
                  "guard=storage.process_guard(); guard.__enter__(); print('locked',flush=True); "
                  "sys.stdin.readline(); guard.__exit__(None,None,None)")
        process = subprocess.Popen([sys.executable, '-c', script, str(self.database)],
            cwd=ROOT / 'hiyoribot-backend', stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            with self.assertRaises(RuntimeError):
                tool.restore(saved, self.database, self.folder, self.database.name)
        finally:
            process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_media_manifest_copy_restore_and_tamper(self):
        cfg=SimpleNamespace(media_home=None,qq_owner_id='100001')
        out=io.BytesIO();Image.new('RGB',(8,8),'red').save(out,format='PNG')
        with patch.object(media_service,'kick'):
            identifier=media_service.ingest(media_service.Upload(bot_id='100002',user_id='100001',
                message_id='1',slot=0,data=base64.b64encode(out.getvalue()).decode(),manual=True),cfg)['asset_id']
        media_service.manage(media_service.Manage(bot_id='100002',user_id='100001',command='/表情 通过 '+identifier),cfg)
        saved=tool.backup(self.database,self.folder)
        manifest=tool.check(saved)
        self.assertEqual(len(manifest['media']),1)
        original=self.database.parent/'media'/manifest['media'][0]['name']
        original.unlink()
        safety=tool.restore(saved,self.database,self.folder,self.database.name)
        self.assertEqual(original.read_bytes(),out.getvalue())
        self.assertEqual(tool.check(safety)['missing_media'],[original.name])
        with self.assertRaises(ValueError):
            tool.restore(safety,self.database,self.folder,self.database.name)
        (saved.with_suffix('.media')/original.name).write_bytes(b'tampered')
        with self.assertRaises(ValueError):tool.check(saved)


if __name__ == '__main__':
    unittest.main()
