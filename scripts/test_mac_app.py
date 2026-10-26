"""Unit tests of scripts/mac_app.py. They run no compiler, codesign or notary service."""
from pathlib import Path
from unittest import mock
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mac_app  # noqa: E402


def completed(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')


class MacAppTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.app = self.folder / 'Coconut.app'
        (self.app / 'Contents/Helpers').mkdir(parents=True)
        for name in ('coconut', 'tunnel-sidecar'):
            (self.app / 'Contents/Helpers' / name).write_text('')
        with (self.app / 'Contents/Info.plist').open('wb') as handle:
            plistlib.dump(mac_app.info_plist('1.2.0', '512'), handle)

    def tearDown(self):
        subprocess.run(['rm', '-rf', str(self.folder)], check=True)

    def test_the_bundle_is_a_menu_bar_app_for_macos_14(self):
        info = mac_app.info_plist('1.2.0', '512')
        self.assertEqual(info['CFBundleIdentifier'], 'com.gumroad.coconut.mac')
        self.assertEqual(info['CFBundleExecutable'], 'Coconut')
        self.assertEqual((info['CFBundleShortVersionString'], info['CFBundleVersion']), ('1.2.0', '512'))
        self.assertEqual(info['LSMinimumSystemVersion'], '14.0')
        self.assertIs(info['LSUIElement'], True)

    def test_the_archive_name_has_the_version_and_the_build(self):
        self.assertEqual(mac_app.archive_name(self.app), 'Coconut-1.2.0-512.zip')

    def test_an_archive_over_the_limit_stops_the_release(self):
        def zip_file(arguments):
            Path(arguments[-1]).write_bytes(b'x' * 2048)
        with mock.patch.object(mac_app, 'run', side_effect=zip_file):
            self.assertEqual(mac_app.archive(self.app, self.folder / 'a.zip', 4096).stat().st_size, 2048)
            with self.assertRaisesRegex(SystemExit, 'at most'):
                mac_app.archive(self.app, self.folder / 'b.zip', 1024)
            # The temporary archive for the notary service has no limit.
            mac_app.archive(self.app, self.folder / 'c.zip')
        self.assertEqual(mac_app.ARCHIVE_LIMIT, 25 * 1024 * 1024)

    def test_a_release_signature_signs_the_helpers_first_with_the_hardened_runtime_and_a_timestamp(self):
        with mock.patch.object(mac_app, 'run') as run:
            mac_app.sign(self.app, 'ABCDEF')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[-1] for command in commands],
                         [self.app / 'Contents/Helpers/coconut', self.app / 'Contents/Helpers/tunnel-sidecar', self.app, self.app])
        for command in commands[:3]:
            self.assertEqual(command[:7], ['codesign', '--force', '--options', 'runtime', '--sign', 'ABCDEF', '--timestamp'])
        self.assertEqual(commands[3][:4], ['codesign', '--verify', '--deep', '--strict'])

    def test_an_ad_hoc_signature_asks_for_no_timestamp(self):
        with mock.patch.object(mac_app, 'run') as run:
            mac_app.sign(self.app, '-')
        self.assertIn('--timestamp=none', run.call_args_list[0].args[0])

    def test_an_accepted_app_is_stapled_and_assessed(self):
        accepted = completed(json.dumps({'id': 'abc', 'status': 'Accepted'}))
        with mock.patch.object(mac_app, 'run') as run, \
                mock.patch.object(mac_app.subprocess, 'run', return_value=accepted) as submit:
            mac_app.notarize(self.app, Path('/keys/AuthKey.p8'), 'KEY', 'ISSUER')
        command = submit.call_args.args[0]
        self.assertEqual(command[:3], ['xcrun', 'notarytool', 'submit'])
        self.assertEqual(command[4:10], ['--key', '/keys/AuthKey.p8', '--key-id', 'KEY', '--issuer', 'ISSUER'])
        self.assertIn('--wait', command)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][0], 'ditto')
        self.assertEqual(commands[1:], [['xcrun', 'stapler', 'staple', self.app], ['xcrun', 'stapler', 'validate', self.app],
                                        ['spctl', '--assess', '--type', 'execute', '--verbose=2', self.app]])

    def test_a_refused_app_stops_the_release_with_the_notary_log(self):
        responses = [completed(json.dumps({'id': 'abc', 'status': 'Invalid', 'message': 'Processing complete'})),
                     completed('The binary is not signed with a valid Developer ID certificate.')]
        with mock.patch.object(mac_app, 'run') as run, mock.patch.object(mac_app.subprocess, 'run', side_effect=responses):
            with self.assertRaises(SystemExit) as raised:
                mac_app.notarize(self.app, Path('/keys/AuthKey.p8'), 'KEY', 'ISSUER')
        self.assertIn('Invalid', str(raised.exception))
        self.assertIn('not signed with a valid Developer ID', str(raised.exception))
        self.assertFalse(any(call.args[0][:2] == ['xcrun', 'stapler'] for call in run.call_args_list))

    def test_a_failed_submission_says_why(self):
        failed = subprocess.CompletedProcess([], 1, stdout='', stderr='Error: HTTP status code: 401. Unauthenticated.')
        with mock.patch.object(mac_app, 'run'), mock.patch.object(mac_app.subprocess, 'run', return_value=failed):
            with self.assertRaises(SystemExit) as raised:
                mac_app.notarize(self.app, Path('/keys/AuthKey.p8'), 'KEY', 'ISSUER')
        self.assertIn('401', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
