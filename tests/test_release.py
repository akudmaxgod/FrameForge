import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from imagegen.instance import InstanceLock
from imagegen.release_ops import prepare_update, sha256, verify
from imagegen.release_paths import data_root, component_root


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.old=self.root/'Предыдущая версия';self.old.mkdir()
        self.data=self.old/'data';self.data.mkdir()
        for name,content in {'app.sqlite':b'synthetic database','settings.json':b'{"test":true}',
                             'secrets/pixabay.dat':b'synthetic encrypted bytes',
                             'projects/example/image.png':b'synthetic media'}.items():
            p=self.data/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
        self.new=self.root/'Новая версия';self.new.mkdir()
        (self.new/'code.txt').write_text('release',encoding='utf-8')
        (self.new/'SHA256SUMS.json').write_text(json.dumps({'version':'test','files':{'code.txt':sha256(self.new/'code.txt')}}))

    def tearDown(self):self.temp.cleanup()

    def test_offline_update_preserves_all_data_and_encrypted_secret(self):
        originals={p.relative_to(self.data).as_posix():sha256(p) for p in self.data.rglob('*') if p.is_file()}
        result=prepare_update(self.old,self.new)
        self.assertTrue(result['ok'])
        backup=Path(result['backup'])/'data'
        for name,value in originals.items():
            self.assertEqual(sha256(self.data/name),value)
            self.assertEqual(sha256(backup/name),value)
        config=json.loads((self.new/'.frameforge-profile.json').read_text(encoding='utf-8'))
        self.assertEqual(config['data_path'],str(self.data))
        self.assertEqual(config['components_path'],str(self.old))
        self.assertNotIn('synthetic encrypted bytes',json.dumps(config))

    def test_running_instance_blocks_update(self):
        with InstanceLock(self.data/'worker.lock'):
            with self.assertRaises(Exception):prepare_update(self.old,self.new)
        self.assertFalse((self.new/'.frameforge-profile.json').exists())

    def test_corrupt_release_blocks_before_backup(self):
        (self.new/'code.txt').write_text('tampered')
        with self.assertRaises(ValueError):prepare_update(self.old,self.new)
        self.assertFalse((self.new/'.frameforge-profile.json').exists())
        self.assertFalse((self.root/'FrameForge-backups').exists())

    def test_manifest_cannot_escape_root(self):
        (self.new/'SHA256SUMS.json').write_text(json.dumps({'version':'test','files':{'../secret':'x'}}))
        with self.assertRaises(ValueError):verify(self.new)

    def test_configured_paths_are_independent_of_source_code(self):
        with patch.dict(os.environ,{'FRAMEFORGE_DATA':str(self.data),'FRAMEFORGE_COMPONENTS':str(self.old)}):
            self.assertEqual(data_root(),self.data)
            self.assertEqual(component_root(),self.old)
            self.assertEqual(data_root(self.root/'explicit'),self.root/'explicit')


if __name__=='__main__':unittest.main()

