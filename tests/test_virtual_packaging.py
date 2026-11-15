"""Pinned driver inputs must be verified before a packaged payload is replaced."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from goggles_bridge.app.virtual_display import virtual_driver

spec = importlib.util.spec_from_file_location('virtual_windows_build', Path(__file__).resolve().parents[1]/'packaging/build_windows.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PackagingTests(unittest.TestCase):
    def test_missing_or_incomplete_webview_stops_before_building(self):
        for module in (None, SimpleNamespace(), SimpleNamespace(create_window=None, start=lambda: None)):
            with self.subTest(module=module), patch.dict(sys.modules, {'webview': module}), \
                 patch.object(build, 'prepare_vdd') as prepare, \
                 patch.object(build.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'dependencies|not pywebview'):
                    build.main()
                prepare.assert_not_called()
                run.assert_not_called()

    def test_bad_archive_hash_cannot_replace_existing_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root/'packaging/cache'
            cache.mkdir(parents=True)
            (cache/'vdd-25.7.23.zip').write_bytes(b'tampered')
            target = root/'vendor/vdd'
            target.mkdir(parents=True)
            (target/'MttVDD.dll').write_bytes(b'original')
            with patch.object(build, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'archive checksum'):
                    build.prepare_vdd()
            self.assertEqual((target/'MttVDD.dll').read_bytes(), b'original')

    def test_duplicate_or_invalid_driver_member_is_rejected_before_extraction(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                cache = root/'packaging/cache'
                cache.mkdir(parents=True)
                archive = cache/'vdd-25.7.23.zip'
                with zipfile.ZipFile(archive, 'w') as bundle:
                    bundle.writestr('one/MttVDD.dll', b'fixture')
                    if duplicate:
                        bundle.writestr('two/MttVDD.dll', b'fixture')
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                with patch.object(build, 'ROOT', root), patch.object(build, 'VDD_SHA256', digest), \
                     patch.object(virtual_driver, 'HASHES', {'MttVDD.dll': '0'*64}):
                    with self.assertRaises(ValueError):
                        build.prepare_vdd()
                self.assertFalse((root/'vendor').exists())


if __name__ == '__main__':
    unittest.main()
