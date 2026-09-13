import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from coconut.google_cli import install, VERSION


class GoogleCLITests(unittest.TestCase):
    def archive(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            item = tarfile.TarInfo('gog'); data = b'#!/bin/sh\nexit 0\n'; item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        return stream.getvalue()

    def test_install_checks_archive_and_reuses_private_executable(self):
        archive = self.archive(); name = f'gogcli_{VERSION}_darwin_arm64.tar.gz'
        with tempfile.TemporaryDirectory() as directory, patch('coconut.google_cli.platform.system', return_value='Darwin'), patch('coconut.google_cli.platform.machine', return_value='arm64'), patch('coconut.google_cli.download', side_effect=[(hashlib.sha256(archive).hexdigest()+'  '+name).encode(), archive]) as download:
            path = Path(install(Path(directory)/'tools'))
            self.assertEqual(path.read_bytes(), b'#!/bin/sh\nexit 0\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(install(path.parent), str(path))
            self.assertEqual(download.call_count, 2)

    def test_checksum_failure_does_not_install(self):
        with tempfile.TemporaryDirectory() as directory, patch('coconut.google_cli.platform.system', return_value='Darwin'), patch('coconut.google_cli.platform.machine', return_value='arm64'), patch('coconut.google_cli.download', side_effect=[b'bad  gogcli_0.39.1_darwin_arm64.tar.gz', self.archive()]):
            with self.assertRaises(ValueError): install(Path(directory)/'tools')
            self.assertFalse((Path(directory)/'tools/gog').exists())
