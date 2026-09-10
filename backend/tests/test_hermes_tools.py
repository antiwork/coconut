import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('hermes_installer', Path(__file__).resolve().parents[2] / 'scripts/hermes.py')
hermes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hermes)


class HermesToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / '.runtime'
        for name, value in (('ROOT', self.root), ('RUNTIME', self.runtime)):
            replacement = patch.object(hermes, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.target = 'aarch64-apple-darwin'
        self.prefix = 'uv-' + self.target

    def archive(self, entries=None):
        path = self.root / 'release.tar.gz'
        if entries is None:
            entries = [(self.prefix + '/' + name, b'#!/bin/sh\nprintf "uv 0.12.10\\n"\n', tarfile.REGTYPE, '')
                       for name in ('uv', 'uvx')]
        with tarfile.open(path, 'w:gz') as archive:
            for name, content, kind, link in entries:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = link
                entry.mode = 0o777
                entry.size = len(content) if kind == tarfile.REGTYPE else 0
                archive.addfile(entry, io.BytesIO(content) if entry.size else None)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_valid_archive_copies_exact_executables_with_private_permissions(self):
        archive, checksum = self.archive()
        destination = self.runtime / 'verified'
        hashes = hermes.extract_uv(archive, destination, self.target, checksum)
        self.assertEqual(set(path.name for path in destination.iterdir()), {'uv', 'uvx'})
        for name in ('uv', 'uvx'):
            binary = destination / name
            self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
            self.assertEqual(hashes[name], hashlib.sha256(binary.read_bytes()).hexdigest())
        self.assertEqual(subprocess.check_output([str(destination / 'uv'), '--version'], text=True).strip(), 'uv 0.12.10')

    def test_wrong_checksum_rejects_archive_before_creating_output(self):
        archive, _ = self.archive()
        destination = self.runtime / 'unverified'
        with self.assertRaisesRegex(SystemExit, 'checksum'):
            hermes.extract_uv(archive, destination, self.target, '0' * 64)
        self.assertFalse(destination.exists())

    def test_tar_paths_and_links_cannot_write_outside_destination(self):
        protected = self.root / 'protected'
        protected.write_text('Keep this file.')
        cases = [('../protected', tarfile.REGTYPE, ''),
                 (str(protected), tarfile.REGTYPE, ''),
                 (self.prefix + '/../../protected', tarfile.REGTYPE, ''),
                 (self.prefix + '/uv', tarfile.SYMTYPE, str(protected)),
                 (self.prefix + '/uv', tarfile.LNKTYPE, str(protected)),
                 (self.prefix + '/uv', tarfile.FIFOTYPE, '')]
        for index, (name, kind, link) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                archive, checksum = self.archive([(name, b'Changed', kind, link)])
                destination = self.runtime / str(index)
                with self.assertRaisesRegex(SystemExit, 'unexpected path or file type'):
                    hermes.extract_uv(archive, destination, self.target, checksum)
                self.assertEqual(protected.read_text(), 'Keep this file.')
                self.assertEqual(list(destination.iterdir()), [])

    def test_duplicate_and_missing_archive_entries_are_rejected(self):
        entry = (self.prefix + '/uv', b'executable', tarfile.REGTYPE, '')
        for index, entries in enumerate(([entry], [entry, entry])):
            with self.subTest(entries=len(entries)):
                archive, checksum = self.archive(entries)
                with self.assertRaises(SystemExit):
                    hermes.extract_uv(archive, self.runtime / str(index), self.target, checksum)

    def test_destination_file_symlink_does_not_overwrite_target(self):
        archive, checksum = self.archive()
        protected = self.root / 'protected'
        protected.write_text('Keep this file.')
        destination = self.runtime / 'verified'
        destination.mkdir(parents=True)
        (destination / 'uv').symlink_to(protected)
        with self.assertRaises(FileExistsError):
            hermes.extract_uv(archive, destination, self.target, checksum)
        self.assertEqual(protected.read_text(), 'Keep this file.')

    def test_runtime_symlink_and_outside_destination_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.runtime.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'symbolic link'):
            hermes.private_directory(self.runtime / 'tools')
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaisesRegex(SystemExit, 'only inside this project'):
            hermes.private_directory(self.root.parent / 'outside-project')

    def test_managed_uv_verifies_bytes_before_install_and_reuses_verified_files(self):
        archive, checksum = self.archive()
        with patch.object(hermes.shutil, 'which', return_value=None), \
             patch.object(hermes, 'uv_target', return_value=self.target), \
             patch.dict(hermes.UV_SHA256, {self.target: checksum}), \
             patch.object(hermes, 'download_uv', side_effect=lambda url, path: path.write_bytes(archive.read_bytes())) as download:
            installed = Path(hermes.managed_uv())
            self.assertTrue(installed.is_file())
            self.assertEqual(hermes.managed_uv(), str(installed))
            self.assertEqual(download.call_count, 1)
            receipt = json.loads((installed.parent / 'install.json').read_text())
            self.assertEqual(receipt['archive_sha256'], checksum)
            self.assertFalse(list((self.runtime / 'tools').glob('uv-download-*')))
            installed.write_bytes(b'changed')
            with self.assertRaisesRegex(SystemExit, 'changed or is incomplete'):
                hermes.managed_uv()

    def test_failed_checksum_installs_nothing_and_removes_staging_files(self):
        archive, _ = self.archive()
        with patch.object(hermes.shutil, 'which', return_value=None), \
             patch.object(hermes, 'uv_target', return_value=self.target), \
             patch.object(hermes, 'download_uv', side_effect=lambda url, path: path.write_bytes(archive.read_bytes())):
            with self.assertRaisesRegex(SystemExit, 'checksum'):
                hermes.managed_uv()
        self.assertEqual(list((self.runtime / 'tools').iterdir()), [])

    def test_cached_receipt_symlink_is_rejected_without_reading_its_target(self):
        archive, checksum = self.archive()
        with patch.object(hermes.shutil, 'which', return_value=None), \
             patch.object(hermes, 'uv_target', return_value=self.target), \
             patch.dict(hermes.UV_SHA256, {self.target: checksum}), \
             patch.object(hermes, 'download_uv', side_effect=lambda url, path: path.write_bytes(archive.read_bytes())):
            installed = Path(hermes.managed_uv())
            receipt = installed.parent / 'install.json'
            protected = self.root / 'protected'
            protected.write_bytes(receipt.read_bytes())
            receipt.unlink()
            receipt.symlink_to(protected)
            with self.assertRaisesRegex(SystemExit, 'changed or is incomplete'):
                hermes.managed_uv()
            self.assertTrue(protected.is_file())

    def test_existing_managed_symlink_cannot_redirect_installation(self):
        tools = self.runtime / 'tools'
        tools.mkdir(parents=True)
        protected = self.root / 'protected'
        protected.mkdir()
        (tools / f'uv-{hermes.UV_VERSION}-{self.target}').symlink_to(protected, target_is_directory=True)
        with patch.object(hermes.shutil, 'which', return_value=None), \
             patch.object(hermes, 'uv_target', return_value=self.target), \
             self.assertRaisesRegex(SystemExit, 'symbolic link'):
            hermes.managed_uv()
        self.assertEqual(list(protected.iterdir()), [])

    def test_existing_path_uv_does_not_create_runtime_or_download(self):
        with patch.object(hermes.shutil, 'which', return_value='/existing/uv'), \
             patch.object(hermes, 'download_uv') as download:
            self.assertEqual(hermes.managed_uv(), '/existing/uv')
            download.assert_not_called()
        self.assertFalse(self.runtime.exists())

    def test_macos_architecture_maps_to_pinned_release(self):
        for machine, target in (('arm64', 'aarch64'), ('aarch64', 'aarch64'), ('x86_64', 'x86_64')):
            with self.subTest(machine=machine), patch.object(hermes.sys, 'platform', 'darwin'), \
                 patch.object(hermes.platform, 'machine', return_value=machine):
                self.assertEqual(hermes.uv_target(), target + '-apple-darwin')
        with patch.object(hermes.sys, 'platform', 'linux'), self.assertRaisesRegex(SystemExit, 'Apple Silicon and Intel'):
            hermes.uv_target()

    def test_missing_command_line_tools_fail_before_running_git(self):
        with patch.object(hermes.sys, 'platform', 'darwin'), \
             patch.object(hermes.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaisesRegex(SystemExit, 'xcode-select --install'):
                hermes.prerequisites()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args[0][0], ['/usr/bin/xcode-select', '-p'])


if __name__ == '__main__':
    unittest.main()
