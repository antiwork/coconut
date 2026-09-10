import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[2] / 'scripts/iphone.py'
spec = importlib.util.spec_from_file_location('iphone_tools', script)
iphone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iphone)


class IPhoneToolsTests(unittest.TestCase):
    def test_configure_rejects_an_unsupported_bundle_before_saving_or_inspecting_the_device(self):
        args = SimpleNamespace(team='66LJACJ2U7', bundle_id='com.example.other', version='0.1.0', device='phone')
        with patch.object(iphone, 'signing_identities', return_value=[{'team': args.team}]) as identities, \
             patch.object(iphone, 'device_details', return_value={'identifier': 'phone', 'hardwareProperties': {'platform': 'iOS'}}) as details, \
             patch.object(iphone, 'save_config') as save, patch.object(iphone, 'CONFIG') as config:
            config.exists.return_value = False
            with self.assertRaisesRegex(SystemExit, 'com.gumroad.coconut'):
                iphone.configure(args)
            identities.assert_not_called()
            details.assert_not_called()
            save.assert_not_called()

    def test_testflight_refuses_another_team_before_build_or_upload(self):
        for team, bundle in [('F8698S24F5', 'com.gumroad.coconut'), ('66LJACJ2U7', 'another.app')]:
            with patch.object(iphone, 'build') as build, patch.object(iphone, 'xcode') as xcode:
                with self.assertRaisesRegex(SystemExit, "Gumroad team"):
                    iphone.testflight({'team': team, 'bundle_id': bundle})
                build.assert_not_called()
                xcode.assert_not_called()

    def test_testflight_upload_restricts_distribution_and_records_only_success(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'Coconut.xcarchive'
            app = archive / 'Products/Applications/Coconut.app'
            app.mkdir(parents=True)
            info = {'CFBundleVersion': '23', 'CoconutPushEnvironment': 'production',
                    'ITSAppUsesNonExemptEncryption': False}
            (app / 'Info.plist').write_bytes(plistlib.dumps(info))
            options = Path(directory) / 'TestFlightOptions.plist'

            def upload(arguments, log_name):
                values = plistlib.loads(options.read_bytes())
                self.assertTrue(values['testFlightInternalTestingOnly'])
                self.assertEqual(values['destination'], 'upload')
                self.assertEqual(values['teamID'], '66LJACJ2U7')
                self.assertEqual(arguments[arguments.index('-archivePath') + 1], str(archive))
                self.assertIn('-allowProvisioningUpdates', arguments)
                self.assertEqual(log_name, 'testflight.log')

            with patch.object(iphone, 'ARCHIVE', archive), patch.object(iphone, 'TESTFLIGHT_OPTIONS', options):
                with patch.object(iphone, 'verify_app'), patch.object(iphone, 'save_config') as save:
                    for failure in [False, True]:
                        config = {'team': '66LJACJ2U7', 'bundle_id': 'com.gumroad.coconut'}
                        save.reset_mock()
                        effect = SystemExit('Apple sign-in required') if failure else upload
                        with patch.object(iphone, 'xcode', side_effect=effect), redirect_stdout(io.StringIO()):
                            if failure:
                                with self.assertRaises(SystemExit):
                                    iphone.testflight(config, skip_build=True)
                                self.assertNotIn('testflight_uploaded_build', config)
                                save.assert_not_called()
                            else:
                                iphone.testflight(config, skip_build=True)
                                self.assertEqual(config['testflight_uploaded_build'], '23')
                                save.assert_called_once()

    def test_testflight_refuses_a_development_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)
            app = archive / 'Products/Applications/Coconut.app'
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CoconutPushEnvironment': 'development'}))
            with patch.object(iphone, 'ARCHIVE', archive), patch.object(iphone, 'verify_app'):
                with patch.object(iphone, 'xcode') as xcode:
                    with self.assertRaisesRegex(SystemExit, 'Release archive'):
                        iphone.testflight({'team': '66LJACJ2U7', 'bundle_id': 'com.gumroad.coconut'}, skip_build=True)
                    xcode.assert_not_called()

    def test_next_number_survives_failed_attempt_and_existing_build(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'Coconut.app'
            app.mkdir()
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion': '12'}))
            with patch.object(iphone, 'APP', app), patch.object(iphone, 'ARCHIVE', Path(directory) / 'archive'):
                self.assertEqual(iphone.next_build_number({'last_build_number': 10}), 13)
                self.assertEqual(iphone.next_build_number({'last_build_number': 14}), 15)

    def test_coredevice_missing_phone_gives_retry_without_rebuild(self):
        result = subprocess.CompletedProcess([], 1, stdout='', stderr='CoreDevice error 1011: cannot locate device')
        with patch.object(iphone.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(SystemExit, 'install --skip-build'):
                iphone.device_command(['device', 'install', 'app'])

    def test_device_command_cleans_child_environment_without_changing_parent(self):
        environment = {'PATH': '/usr/bin', 'DEVICECTL_CHILD_COCONUT_PAIR_CODE': 'private-code',
                       'DEVICECTL_CHILD_OTHER': 'value', 'COCONUT_VERSION': '0.1.0'}

        def completed(command, **kwargs):
            self.assertEqual(kwargs['env'], {'PATH': '/usr/bin', 'COCONUT_VERSION': '0.1.0'})
            self.assertNotIn('--environment-variables', command)
            output = Path(command[command.index('--json-output') + 1])
            output.write_text(json.dumps({'result': {'process': {'processIdentifier': 42}}}))
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        with patch.dict(os.environ, environment, clear=True):
            with patch.object(iphone.subprocess, 'run', side_effect=completed):
                result = iphone.device_command(['device', 'process', 'launch', '--device', 'phone', 'dev.local.coconut'])
            self.assertEqual(dict(os.environ), environment)
            self.assertEqual(result['process']['processIdentifier'], 42)

    def test_failed_install_never_launches_or_records_success(self):
        config = {'device': 'phone', 'bundle_id': 'dev.local.coconut', 'built_number': '2'}
        with patch.object(iphone, 'verify_app'), patch.object(iphone, 'save_config') as save:
            with patch.object(iphone, 'device_command', side_effect=SystemExit('offline')) as command:
                with redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit):
                        iphone.install(config, skip_build=True)
                self.assertEqual(command.call_count, 1)
                self.assertNotIn('installed', output.getvalue())
                self.assertNotIn('installed_build_number', config)
                save.assert_not_called()

    def test_launch_failure_keeps_actual_install_record(self):
        config = {'device': 'phone', 'bundle_id': 'dev.local.coconut', 'built_number': '2'}
        with patch.object(iphone, 'verify_app'), patch.object(iphone, 'save_config') as save:
            with patch.object(iphone, 'device_command', side_effect=[{}, SystemExit('locked')]) as command:
                with redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(SystemExit):
                        iphone.install(config, skip_build=True)
                self.assertEqual(config['installed_build_number'], '2')
                self.assertNotIn('launched', output.getvalue())
                save.assert_called_once()
                self.assertNotIn('--environment-variables', command.call_args.args[0])

    def test_signing_mismatch_stops_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'Coconut.app'
            app.mkdir()
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'wrong.app'}))
            signature = subprocess.CompletedProcess([], 0, stdout='', stderr='TeamIdentifier=F8698S24F5\n')
            with patch.object(iphone, 'run', return_value=signature):
                with self.assertRaisesRegex(SystemExit, 'bundle ID'):
                    iphone.verify_app({'team': 'F8698S24F5', 'bundle_id': 'dev.local.coconut'}, app)
                with self.assertRaisesRegex(SystemExit, 'signing team'):
                    iphone.verify_app({'team': 'OTHERTEAM1', 'bundle_id': 'dev.local.coconut'}, app)

    def test_profile_expiry_and_device_membership_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'Coconut.app'
            app.mkdir()
            (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'dev.local.coconut'}))
            for expiry, devices, message in [
                (datetime.now(timezone.utc) - timedelta(days=1), ['phone-udid'], 'expired'),
                (datetime.now(timezone.utc) + timedelta(days=1), ['another-phone'], 'selected iPhone'),
            ]:
                profile = plistlib.dumps({'ExpirationDate': expiry.replace(tzinfo=None), 'TeamIdentifier': ['F8698S24F5'],
                                           'ProvisionedDevices': devices})
                results = [subprocess.CompletedProcess([], 0),
                           subprocess.CompletedProcess([], 0, stderr='TeamIdentifier=F8698S24F5\n'),
                           subprocess.CompletedProcess([], 0, stdout=profile)]
                config = {'team': 'F8698S24F5', 'bundle_id': 'dev.local.coconut', 'device_udid': 'phone-udid'}
                with self.subTest(message=message), patch.object(iphone, 'run', side_effect=results):
                    with self.assertRaisesRegex(SystemExit, message):
                        iphone.verify_app(config, app)


if __name__ == '__main__':
    unittest.main()
