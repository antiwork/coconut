"""Unit tests of scripts/testflight.py. They use no network."""
from pathlib import Path
from unittest import mock
import base64
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testflight  # noqa: E402


def decode(part):
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))


def raw_to_der(raw):
    def integer(value):
        value = value.lstrip(b'\0') or b'\0'
        if value[0] & 0x80:
            value = b'\0' + value
        return bytes([0x02, len(value)]) + value
    body = integer(raw[:32]) + integer(raw[32:])
    return bytes([0x30, len(body)]) + body


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.key = self.folder / 'AuthKey.p8'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
                        '-out', str(self.key)], check=True, capture_output=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder)

    def test_the_token_has_the_claims_of_app_store_connect_and_a_valid_es256_signature(self):
        value = testflight.token(self.key, 'KEY123', 'issuer-1', now=1_000)
        header, claims, signature = value.split('.')
        self.assertEqual(json.loads(decode(header)), {'alg': 'ES256', 'kid': 'KEY123', 'typ': 'JWT'})
        self.assertEqual(json.loads(decode(claims)),
                         {'iss': 'issuer-1', 'iat': 1_000, 'exp': 2_200, 'aud': 'appstoreconnect-v1'})
        raw = decode(signature)
        self.assertEqual(len(raw), 64)
        public = self.folder / 'public.pem'
        subprocess.run(['openssl', 'pkey', '-in', str(self.key), '-pubout', '-out', str(public)], check=True)
        der = self.folder / 'signature.der'
        der.write_bytes(raw_to_der(raw))
        result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public), '-signature', str(der)],
                                input=f'{header}.{claims}'.encode(), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_short_integer_keeps_its_place_in_the_raw_signature(self):
        raw = bytes(31) + b'\x01' + b'\x80' + bytes(31)
        self.assertEqual(testflight.der_to_raw(raw_to_der(raw)), raw)

    def test_a_signature_that_is_not_der_fails(self):
        with self.assertRaises(ValueError):
            testflight.der_to_raw(b'\x01\x02')


class BuildNumberTests(unittest.TestCase):
    def test_the_next_build_follows_the_highest_build_of_every_page(self):
        pages = {
            '/builds?filter%5Bapp%5D=6810507745&fields%5Bbuilds%5D=version&limit=200': {
                'data': [{'attributes': {'version': '41'}}, {'attributes': {'version': '7'}}],
                'links': {'next': testflight.API + '/builds?cursor=2'}},
            '/builds?cursor=2': {'data': [{'attributes': {'version': '120'}}, {'attributes': {'version': '1.2'}}],
                                 'links': {}},
        }
        with mock.patch.object(testflight, 'get', side_effect=lambda path, bearer: pages[path]):
            self.assertEqual(testflight.next_build('token'), 121)

    def test_the_first_build_is_one(self):
        with mock.patch.object(testflight, 'get', return_value={'data': []}):
            self.assertEqual(testflight.next_build('token'), 1)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.archive = self.folder / 'Coconut.xcarchive'
        self.app = self.archive / 'Products/Applications/Coconut.app'
        self.extension = self.app / 'PlugIns/CoconutNotificationService.appex'
        self.extension.mkdir(parents=True)
        self.write(self.app, CFBundleIdentifier='com.gumroad.coconut', CoconutPushEnvironment='production',
                   ITSAppUsesNonExemptEncryption=False, CFBundleVersion='42', CFBundleShortVersionString='2026.10.27')
        self.write(self.extension, CFBundleVersion='42')
        self.share = self.app / 'PlugIns/CoconutShare.appex'
        self.share.mkdir(parents=True)
        self.write(self.share, CFBundleVersion='42')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder)

    def write(self, bundle, **values):
        (bundle / 'Info.plist').write_bytes(plistlib.dumps(values))

    def test_the_entitlements_get_the_values_of_a_release_build_of_the_gumroad_team(self):
        app = plistlib.loads(testflight.expand(testflight.ENTITLEMENTS['.']))
        self.assertEqual(app['aps-environment'], 'production')
        self.assertEqual(app['keychain-access-groups'],
                         ['66LJACJ2U7.com.gumroad.coconut', '66LJACJ2U7.com.gumroad.coconut.push'])
        extension = plistlib.loads(testflight.expand(
            testflight.ENTITLEMENTS['PlugIns/CoconutNotificationService.appex']))
        self.assertEqual(extension['keychain-access-groups'], ['66LJACJ2U7.com.gumroad.coconut.push'])
        # The share extension reads the list of the app in the shared group, and the pairing in the group of the app.
        share = plistlib.loads(testflight.expand(testflight.ENTITLEMENTS['PlugIns/CoconutShare.appex']))
        self.assertEqual(share['keychain-access-groups'],
                         ['66LJACJ2U7.com.gumroad.coconut.push', '66LJACJ2U7.com.gumroad.coconut'])

    def test_the_extension_is_signed_before_the_app(self):
        with mock.patch.object(testflight.subprocess, 'run') as run:
            testflight.sign(self.archive, 42)
        signed = [call.args[0][-1] for call in run.call_args_list if '--sign' in call.args[0]]
        self.assertEqual(signed, [str(self.extension), str(self.share), str(self.app)])

    def test_both_bundles_get_the_keychain_group_of_their_entitlements(self):
        # An archive without a signature has the group without the team prefix, as in TestFlight builds 2026-10-27.
        for bundle in (self.app, self.extension, self.share):
            info = plistlib.loads((bundle / 'Info.plist').read_bytes())
            info['CoconutPushKeychainGroup'] = 'com.gumroad.coconut.push'
            (bundle / 'Info.plist').write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        with mock.patch.object(testflight.subprocess, 'run'):
            testflight.sign(self.archive, 42)
        for bundle in (self.app, self.extension, self.share):
            data = (bundle / 'Info.plist').read_bytes()
            self.assertTrue(data.startswith(b'bplist'))
            self.assertEqual(plistlib.loads(data)['CoconutPushKeychainGroup'], '66LJACJ2U7.com.gumroad.coconut.push')
            self.assertEqual(plistlib.loads(data)['CFBundleVersion'], '42')

    def test_entitlements_without_the_keychain_group_are_refused(self):
        with self.assertRaises(SystemExit):
            testflight.set_keychain_group(self.extension, plistlib.dumps({'keychain-access-groups': ['66LJACJ2U7.other']}))

    def test_a_development_archive_is_refused(self):
        self.write(self.app, CFBundleIdentifier='com.gumroad.coconut', CoconutPushEnvironment='development',
                   ITSAppUsesNonExemptEncryption=False, CFBundleVersion='42')
        with mock.patch.object(testflight.subprocess, 'run') as run, self.assertRaises(SystemExit):
            testflight.sign(self.archive, 42)
        run.assert_not_called()

    def test_an_extension_with_another_build_number_is_refused(self):
        self.write(self.extension, CFBundleVersion='41')
        with mock.patch.object(testflight.subprocess, 'run'), self.assertRaises(SystemExit):
            testflight.sign(self.archive, 42)

    def test_an_unknown_extension_is_refused(self):
        (self.app / 'PlugIns/Widget.appex').mkdir()
        with mock.patch.object(testflight.subprocess, 'run') as run, self.assertRaises(SystemExit):
            testflight.sign(self.archive, 42)
        run.assert_not_called()

    def test_the_upload_is_for_internal_testers_only(self):
        options = plistlib.loads(testflight.export_options())
        self.assertEqual(options['destination'], 'upload')
        self.assertEqual(options['signingStyle'], 'automatic')
        self.assertTrue(options['testFlightInternalTestingOnly'])


class NotesTests(unittest.TestCase):
    def test_a_moment_is_a_date_and_a_sha_is_a_ref(self):
        self.assertTrue(testflight.is_moment('2026-11-13T04:40:33-08:00'))
        self.assertFalse(testflight.is_moment('86b3cffe'))

    def test_a_ref_windows_the_log_and_a_moment_uses_since(self):
        seen = []
        with mock.patch.object(testflight, 'git', side_effect=lambda *arguments: seen.append(arguments) or ''):
            testflight.commits('86b3cffe')
            testflight.commits('2026-11-13T04:40:33-08:00')
            testflight.commits(None)
        self.assertIn('86b3cffe..HEAD', seen[0])
        self.assertIn('--since=2026-11-13T04:40:33-08:00', seen[1])
        self.assertIn('-40', seen[2])

    def test_docs_ci_and_tests_never_reach_the_notes(self):
        self.assertFalse(testflight.user_facing(['docs/iphone-install.md']))
        self.assertFalse(testflight.user_facing(['.github/workflows/testflight.yml']))
        self.assertFalse(testflight.user_facing(['scripts/test_testflight.py']))
        self.assertFalse(testflight.user_facing(['ios/CoconutTests/AChatTests.swift']))
        self.assertTrue(testflight.user_facing(['ios/Coconut/ChatView.swift', 'README.md']))

    def test_the_notes_lead_with_the_newest_changes_to_try(self):
        text = testflight.compose(['Newest change', 'Older change'])
        self.assertTrue(text.startswith('What to test\n• Newest change'))
        self.assertIn("What's new", text)
        self.assertLessEqual(text.index('Newest change'), text.index('Older change'))

    def test_a_long_list_is_cut_with_a_count_of_what_is_left(self):
        text = testflight.compose([f'Change number {number} with a sentence after it' for number in range(80)])
        self.assertLessEqual(len(text), testflight.NOTES_LIMIT)
        self.assertIn('more in the changelog', text)

    def test_no_change_says_so_instead_of_showing_an_empty_box(self):
        self.assertEqual(testflight.compose([]), 'Bug fixes and improvements.')


class SetNotesTests(unittest.TestCase):
    def test_an_existing_localization_is_patched(self):
        before = {'data': [{'id': 'LOC', 'attributes': {'locale': 'en-US', 'whatsNew': 'old notes'}}]}
        after = {'data': [{'id': 'LOC', 'attributes': {'locale': 'en-US', 'whatsNew': 'notes'}}]}
        with mock.patch.object(testflight, 'build_id', return_value='BUILD'), \
                mock.patch.object(testflight, 'get', side_effect=[before, after]), \
                mock.patch.object(testflight, 'request') as request:
            saved = testflight.set_notes('bearer', 42, 'notes')
        self.assertEqual(saved, 'notes')
        self.assertEqual(request.call_args[0][0], 'PATCH')
        self.assertEqual(request.call_args[0][1], '/betaBuildLocalizations/LOC')
        self.assertEqual(request.call_args[0][3]['data']['attributes']['whatsNew'], 'notes')

    def test_a_missing_localization_is_created(self):
        requests = []
        def respond(method, path, bearer, body=None):
            requests.append((method, path, body))
            return {}
        documents = [{'data': []}, {'data': [{'id': 'LOC', 'attributes': {'locale': 'en-US', 'whatsNew': 'notes'}}]}]
        with mock.patch.object(testflight, 'build_id', return_value='BUILD'), \
                mock.patch.object(testflight, 'get', side_effect=lambda *arguments: documents.pop(0)), \
                mock.patch.object(testflight, 'request', side_effect=respond):
            testflight.set_notes('bearer', 42, 'notes')
        method, path, body = requests[0]
        self.assertEqual((method, path), ('POST', '/betaBuildLocalizations'))
        self.assertEqual(body['data']['relationships']['build']['data']['id'], 'BUILD')
        self.assertEqual(body['data']['attributes']['whatsNew'], 'notes')


if __name__ == '__main__':
    unittest.main()
