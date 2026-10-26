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

    def test_the_bundle_carries_the_source_package_its_id_and_its_installer(self):
        resources = self.folder / 'Resources'
        resources.mkdir()
        package_id = mac_app.bundle_source(resources)
        self.assertEqual(json.loads((resources / 'package.json').read_text()), {'package_id': package_id})
        self.assertEqual((resources / 'deploy.py').read_bytes(), (mac_app.ROOT / 'scripts/deploy.py').read_bytes())
        sys.path.insert(0, str(mac_app.ROOT / 'scripts'))
        import deploy
        with (resources / 'coconut-source.tar.gz').open('rb') as stream:
            package = deploy.validate_package(stream)
        self.assertEqual(package.package_id, package_id)
        self.assertIn('scripts/host.py', package.files)

    def test_the_archive_name_has_the_version_the_build_and_a_nightly_suffix(self):
        self.assertEqual(mac_app.archive_name(self.app), 'Coconut-1.2.0-512.zip')
        self.assertEqual(mac_app.archive_name(self.app, 'nightly'), 'Coconut-1.2.0-512-nightly.zip')

    def test_the_bundle_names_the_feed_and_its_key_and_updates_by_itself(self):
        info = mac_app.info_plist('1.2.0', '512')
        self.assertEqual(info['SUFeedURL'], 'https://coconut-updates.gumroad.workers.dev/mac/appcast.xml')
        self.assertEqual(info['SUPublicEDKey'], 'bsZLk/GXxQHmjyBDgQ/RY31DmKYvGBLPmJhzOFCkAWE=')
        self.assertIs(info['SUEnableAutomaticChecks'], True)
        self.assertIs(info['SUAutomaticallyUpdate'], True)

    def test_sparkle_goes_in_without_the_xpc_services_of_a_sandboxed_app(self):
        framework = self.folder / 'Sparkle.framework'
        (framework / 'Versions/B/XPCServices/Installer.xpc').mkdir(parents=True)
        (framework / 'Versions/B/Sparkle').write_text('')
        (framework / 'Versions/Current').symlink_to('B')
        (framework / 'XPCServices').symlink_to('Versions/Current/XPCServices')
        destination = self.folder / 'Frameworks/Sparkle.framework'
        mac_app.add_sparkle(framework, destination)
        self.assertTrue((destination / 'Versions/Current').is_symlink())
        self.assertTrue((destination / 'Versions/B/Sparkle').is_file())
        self.assertFalse((destination / 'Versions/B/XPCServices').exists())
        self.assertFalse((destination / 'XPCServices').is_symlink())

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

    def test_a_release_signature_signs_the_inner_code_first_with_the_hardened_runtime_and_a_timestamp(self):
        with mock.patch.object(mac_app, 'run') as run:
            mac_app.sign(self.app, 'ABCDEF')
        commands = [call.args[0] for call in run.call_args_list]
        sparkle = self.app / 'Contents/Frameworks/Sparkle.framework'
        # Sparkle's helpers come before the framework, and everything comes before the app.
        self.assertEqual([command[-1] for command in commands],
                         [sparkle / 'Versions/B/Autoupdate', sparkle / 'Versions/B/Updater.app', sparkle,
                          self.app / 'Contents/Helpers/coconut', self.app / 'Contents/Helpers/tunnel-sidecar', self.app, self.app])
        for command in commands[:6]:
            self.assertEqual(command[:7], ['codesign', '--force', '--options', 'runtime', '--sign', 'ABCDEF', '--timestamp'])
        self.assertEqual(commands[6][:4], ['codesign', '--verify', '--deep', '--strict'])

    def test_an_ad_hoc_signature_has_no_timestamp_and_no_hardened_runtime(self):
        with mock.patch.object(mac_app, 'run') as run:
            mac_app.sign(self.app, '-')
        self.assertIn('--timestamp=none', run.call_args_list[0].args[0])
        # Library validation would refuse the ad-hoc Sparkle.framework, which has no Team ID.
        self.assertNotIn('runtime', run.call_args_list[0].args[0])

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
