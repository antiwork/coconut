"""Unit tests of scripts/mac_feed.py. They use no network."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from unittest import mock
import plistlib
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mac_feed  # noqa: E402

BASE = mac_feed.BASE_URL


def item(build, **changes):
    values = dict(version='0.1.0', build=build, file=f'Coconut-0.1.0-{build}.zip', length=build,
                  signature=f'sig{build}', published='Mon, 26 Oct 2026 12:00:00 +0000')
    values.update(changes)
    return mac_feed.Item(**values)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder)

    def archive(self, build, size=2048):
        path = self.folder / f'Coconut-0.1.0-{build}.zip'
        with zipfile.ZipFile(path, 'w') as bundle:
            bundle.writestr('Coconut.app/Contents/Info.plist', plistlib.dumps(
                {'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': str(build), 'LSMinimumSystemVersion': '14.0'}))
            bundle.writestr('Coconut.app/Contents/padding', b'x' * size)
        return path

    def test_the_feed_has_the_sparkle_fields_and_no_channel(self):
        feed = ElementTree.fromstring(mac_feed.render([item(12), item(11)]))
        newest, older = feed.findall('channel/item')
        for entry in (newest, older):
            self.assertIsNone(entry.find(mac_feed.tag('channel')), 'An item without a channel reaches every app.')
        self.assertEqual(older.findtext(mac_feed.tag('version')), '11')
        self.assertEqual(older.findtext(mac_feed.tag('shortVersionString')), '0.1.0')
        self.assertEqual(older.findtext(mac_feed.tag('minimumSystemVersion')), mac_feed.MINIMUM_SYSTEM)
        enclosure = older.find('enclosure')
        self.assertEqual(enclosure.get('url'), f'{BASE}/Coconut-0.1.0-11.zip')
        self.assertEqual(enclosure.get(mac_feed.tag('edSignature')), 'sig11')
        self.assertEqual(enclosure.get('length'), '11')

    def test_a_feed_reads_back_the_items_that_it_holds(self):
        items = [item(12), item(11)]
        self.assertEqual(mac_feed.parse(mac_feed.render(items)), items)
        self.assertEqual(mac_feed.parse(b''), [])

    def test_a_feed_must_not_name_archives_outside_the_worker(self):
        data = mac_feed.render([item(11)]).replace(BASE.encode(), b'https://example.com/mac')
        with self.assertRaisesRegex(SystemExit, 'outside'):
            mac_feed.parse(data)

    def test_the_feed_keeps_its_newest_items(self):
        merged = mac_feed.merge([item(build) for build in range(1, 7)], item(8), keep=5)
        self.assertEqual([entry.build for entry in merged], [8, 6, 5, 4, 3])

    def test_the_feed_keeps_the_last_build_for_an_older_macos(self):
        older = [item(build, minimum_system='14.0') for build in (1, 2)]
        merged = mac_feed.merge(older + [item(build) for build in range(3, 8)], item(8), keep=5)
        self.assertEqual([entry.build for entry in merged], [8, 7, 6, 5, 4, 2])
        self.assertEqual(merged[-1].minimum_system, '14.0')

    def test_a_rebuild_replaces_the_item_of_the_same_build(self):
        merged = mac_feed.merge([item(8, signature='old'), item(7)], item(8, signature='new'))
        self.assertEqual([(entry.build, entry.signature) for entry in merged], [(8, 'new'), (7, 'sig7')])

    def test_the_latest_build(self):
        self.assertEqual(mac_feed.latest_build(mac_feed.render([item(9), item(8)])), 9)
        self.assertEqual(mac_feed.latest_build(None), 0)

    def test_the_new_item_reads_the_version_and_the_build_from_the_archive(self):
        archive = self.archive(512)
        new = mac_feed.new_item(archive, 'sig', datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
        self.assertEqual((new.version, new.build, new.file, new.length), ('0.1.0', 512, mac_feed.published_name(archive), archive.stat().st_size))
        self.assertEqual(new.published, 'Mon, 26 Oct 2026 12:00:00 +0000')

    def test_publish_writes_the_complete_assets_folder(self):
        old = self.archive(510)
        feed = mac_feed.render([mac_feed.new_item(old, 'old-signature')])
        new = self.archive(512, size=4096)
        requested = []
        def download(url):
            requested.append(url)
            return old.read_bytes()
        output = self.folder / 'public'
        items = mac_feed.publish(new, 'new-signature', output, feed=feed, download=download)
        self.assertEqual([entry.build for entry in items], [512, 510])
        old_name, new_name = mac_feed.published_name(old), mac_feed.published_name(new)
        self.assertEqual(requested, [f'{BASE}/{old_name}'])
        self.assertEqual(sorted(path.name for path in (output / 'mac').iterdir()), sorted(['appcast.xml', old_name, new_name]))
        self.assertEqual((output / 'mac' / new_name).read_bytes(), new.read_bytes())
        self.assertEqual((output / '_headers').read_text(), (mac_feed.ROOT / 'updates/_headers').read_text())
        self.assertEqual(mac_feed.parse((output / 'mac/appcast.xml').read_bytes()), items)

    def test_publish_writes_the_install_script_and_the_download_link(self):
        new = self.archive(512)
        output = self.folder / 'public'
        mac_feed.publish(new, 'sig', output, feed=None, download=lambda url: None)
        self.assertEqual((output / 'install').read_text(), (mac_feed.ROOT / 'updates/install.sh').read_text())
        self.assertEqual((output / '_redirects').read_text(), f'/mac/download /mac/{mac_feed.published_name(new)} 302\n')
        self.assertIn('/install\n  Content-Type: text/plain', (output / '_headers').read_text())

    def test_each_published_archive_name_ends_with_its_digest(self):
        archive = self.archive(512)
        name = mac_feed.published_name(archive)
        self.assertRegex(name, r'^Coconut-0\.1\.0-512-[0-9a-f]{16}\.zip$')
        self.assertTrue(mac_feed.matches_name(name, archive.read_bytes()))
        # A rebuild of the same build has other bytes, so it gets another URL.
        rebuilt = self.archive(512, size=2049)
        self.assertNotEqual(mac_feed.published_name(rebuilt), name)
        self.assertFalse(mac_feed.matches_name('Coconut-0.1.0-512.zip', archive.read_bytes()))

    def test_the_archives_of_the_earlier_feed_stay_for_one_more_release(self):
        old = self.archive(512, size=2048)
        feed = mac_feed.render([mac_feed.new_item(old, 'old-signature')])
        old_bytes, old_name = old.read_bytes(), mac_feed.published_name(old)
        rebuilt = self.archive(512, size=2049)
        output = self.folder / 'public'
        items = mac_feed.publish(rebuilt, 'new-signature', output, feed=feed, download=lambda url: old_bytes)
        # The rebuild replaces the item, but a client with the cached earlier appcast still finds its archive.
        self.assertEqual([entry.file for entry in items], [mac_feed.published_name(rebuilt)])
        self.assertEqual((output / 'mac' / old_name).read_bytes(), old_bytes)
        self.assertNotIn(old_name.encode(), (output / 'mac/appcast.xml').read_bytes())

    def test_publish_stops_when_a_kept_archive_is_missing_or_changed(self):
        old = self.archive(510)
        feed = mac_feed.render([mac_feed.new_item(old, 'old-signature')])
        new = self.archive(512)
        same_length = bytearray(old.read_bytes())
        same_length[-1] ^= 1
        for data in (None, b'changed', bytes(same_length)):
            with self.subTest(data=data), self.assertRaisesRegex(SystemExit, 'no complete'):
                mac_feed.publish(new, 'sig', self.folder / 'public', feed=feed, download=lambda url: data)

    @unittest.skipUnless(importlib.util.find_spec('cryptography'), 'The release job installs cryptography.')
    def test_the_key_check_accepts_only_the_key_of_the_bundle(self):
        import base64
        seed = bytes(range(32))
        with mock.patch.object(mac_feed, 'ED_PUBLIC_KEY', mac_feed.public_key(base64.b64encode(seed).decode())):
            mac_feed.check_key(base64.b64encode(seed).decode())
            # The earlier format appends the public key to the seed.
            public = base64.b64decode(mac_feed.ED_PUBLIC_KEY)
            mac_feed.check_key(base64.b64encode(seed + public).decode())
            with self.assertRaisesRegex(SystemExit, 'does not belong'):
                mac_feed.check_key(base64.b64encode(bytes(32)).decode())

    def test_publish_needs_a_signature(self):
        with self.assertRaisesRegex(SystemExit, 'signature'):
            mac_feed.publish(self.archive(512), '', self.folder / 'public', feed=None, download=lambda url: None)


class RedirectTests(unittest.TestCase):
    def test_the_download_link_names_the_newest_archive(self):
        self.assertEqual(mac_feed.redirects([item(514), item(512)]), '/mac/download /mac/Coconut-0.1.0-514.zip 302\n')
        self.assertEqual(mac_feed.redirects([]), '')


# A fake `coconut` tool for the app in the test archives. `status` reads the state that the fake `open` writes.
# TEST_NO_SETTINGS stands for an app without the settings commands.
FAKE_TOOL = """#!/bin/bash
case "$1" in
  help) printf 'pair <code>\\nCoconut.app --set-up sets up Coconut\\n' ;;
  status) printf '{"service": "%s", "relay": "connected"}\\n' "$(cat "$COCONUT_TEST_STATE" 2>/dev/null || echo stopped)" ;;
  settings) [ -n "$TEST_NO_SETTINGS" ] && exit 64; echo "settings $2 $3" >> "$TEST_LOG" ;;
  pair) echo "paired $2" ;;
esac
"""
# A tool that pairs with a code, in an app that ignores --set-up. Its service does not run.
NO_SETUP_TOOL = """#!/bin/bash
case "$1" in
  help) printf 'pair <code>\\n' ;;
  status) printf '{"service": "not_installed", "relay": "not_installed"}\\n' ;;
  *) exit 64 ;;
esac
"""
# An earlier tool: no code pairing and no settings commands.
OLD_TOOL = """#!/bin/bash
case "$1" in
  help) printf 'pair      Print a one-time iPhone setup link.\\n' ;;
  status) printf '{"service": "running", "relay": "connected"}\\n' ;;
  pair) echo "Setup link: coconut://pair/abc" ;;
  *) exit 64 ;;
esac
"""
STUBS = {
    'codesign': """#!/bin/bash
case "$1" in
  --verify) exit "${TEST_CODESIGN_VERIFY:-0}" ;;
  --display) printf 'Identifier=com.gumroad.coconut.mac\\nTeamIdentifier=%s\\n' "${TEST_TEAM:-66LJACJ2U7}" >&2 ;;
esac
""",
    'spctl': '#!/bin/bash\nexit "${TEST_SPCTL:-0}"\n',
    'open': '#!/bin/bash\necho "open $*" >> "$TEST_LOG"\necho running > "$COCONUT_TEST_STATE"\n',
    'pgrep': '#!/bin/bash\nexit 1\n',
    'defaults': '#!/bin/bash\necho "defaults $*" >> "$TEST_LOG"\n',
}
CODE = 'aaaqeayeaudaocajbifqydiob4'


@unittest.skipUnless(sys.platform == 'darwin', 'The install script runs on macOS.')
class InstallScriptTests(unittest.TestCase):
    """Run updates/install.sh with a feed of file URLs and stubs for the signature checks and for `open`."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        for name, text in STUBS.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        # A space in a folder name must not split it.
        self.applications = [self.folder / 'Applications', self.folder / 'home dir/Applications']
        self.log = self.folder / 'log'
        self.log.touch()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder)

    def make_app(self, path: Path, tool: str = FAKE_TOOL) -> Path:
        (path / 'Contents/Helpers').mkdir(parents=True)
        (path / 'Contents/Helpers/coconut').write_text(tool)
        (path / 'Contents/Helpers/coconut').chmod(0o755)
        return path

    def archive(self, build: int, tool: str = FAKE_TOOL) -> Path:
        """A zipped Coconut.app with a fake tool, named like a published archive."""
        import subprocess
        app = self.make_app(self.folder / f'build{build}/Coconut.app', tool)
        path = self.folder / f'Coconut-0.1.0-{build}.zip'
        subprocess.run(['ditto', '-c', '-k', '--keepParent', str(app), str(path)], check=True)
        named = self.folder / mac_feed.published_name(path)
        path.rename(named)
        return named

    def feed(self, *builds: int, tool: str = FAKE_TOOL, minimums: dict | None = None) -> str:
        """Write a feed of items with file URLs, from the newest build down. Return its URL. `minimums` maps a build
        to the oldest macOS that it supports."""
        items = []
        for build in builds:
            path = self.archive(build, tool)
            items.append(mac_feed.Item(version='0.1.0', build=build, file=path.name,
                                       length=path.stat().st_size, signature='sig', published='Mon, 26 Oct 2026',
                                       minimum_system=(minimums or {}).get(build, '14.0')))
        text = mac_feed.render(items).decode().replace(mac_feed.BASE_URL, self.folder.as_uri())
        (self.folder / 'appcast.xml').write_text(text)
        return (self.folder / 'appcast.xml').as_uri()

    def run_script(self, *arguments: str, feed: str = 'file:///missing/appcast.xml', **environment: str):
        import os
        import subprocess
        values = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin', HOME=str(self.folder / 'home dir'),
                      COCONUT_FEED_URL=feed, COCONUT_APP_FOLDERS=':'.join(map(str, self.applications)),
                      COCONUT_TEST_STATE=str(self.folder / 'state'), TEST_LOG=str(self.log), **environment)
        return subprocess.run(['bash', '-s', '--', *arguments], input=(mac_feed.ROOT / 'updates/install.sh').read_text(),
                              env=values, capture_output=True, text=True, timeout=60)

    def note(self, result) -> str:
        lines = result.stdout.splitlines()
        return lines[lines.index('Coconut: Note for Hermes:') + 1]

    def test_a_fresh_install_takes_the_newest_build_opens_the_app_and_pairs(self):
        feed = self.feed(514, 512)
        result = self.run_script(CODE, feed=feed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Downloading Coconut-0.1.0-514-', result.stderr)
        self.assertIn(f'Installed Coconut.app in {self.applications[0]}.', result.stderr)
        self.assertTrue((self.applications[0] / 'Coconut.app/Contents/Helpers/coconut').is_file())
        log = self.log.read_text()
        self.assertIn(f'open -g {self.applications[0]}/Coconut.app --args --set-up', log)
        self.assertIn('settings automatic-updates on', log)
        self.assertNotIn('channel', log)
        self.assertIn(f'paired {CODE}\n', result.stdout)
        for line in result.stdout.splitlines():
            self.assertTrue(line.startswith('Coconut: ') or line == f'paired {CODE}', line)

    def test_the_note_tells_hermes_how_to_turn_automatic_updates_off_and_how_to_update(self):
        result = self.run_script(CODE, feed=self.feed(512))
        tool = f'{self.applications[0]}/Coconut.app/Contents/Helpers/coconut'
        self.assertEqual(self.note(result),
                         'Coconut: Coconut is installed and paired. Automatic updates are on. Ask the person whether to keep '
                         f'them on. To turn them off, run: {tool} settings automatic-updates off. To update Coconut at any '
                         f'time, run: {tool} update.')
        self.assertTrue(result.stdout.endswith(self.note(result) + '\n'))

    def test_an_app_without_the_settings_commands_still_pairs(self):
        result = self.run_script(CODE, feed=self.feed(514), TEST_NO_SETTINGS='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('defaults write', self.log.read_text())
        self.assertIn(f'paired {CODE}', result.stdout)
        self.assertIn('cannot change its update settings yet', self.note(result))

    def test_a_mac_takes_the_newest_build_that_its_macos_can_open(self):
        feed = self.feed(516, 514, 512, minimums={516: '99.0', 514: '99.0'})
        result = self.run_script(CODE, feed=feed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Downloading Coconut-0.1.0-512-', result.stderr)

    def test_a_mac_that_no_build_supports_installs_nothing(self):
        feed = self.feed(518, minimums={518: '99.0'})
        result = self.run_script(CODE, feed=feed)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No Coconut.app in the update feed runs on macOS', result.stderr)

    def test_an_empty_feed_installs_nothing(self):
        (self.folder / 'appcast.xml').write_text(mac_feed.render([]).decode())
        result = self.run_script(CODE, feed=(self.folder / 'appcast.xml').as_uri())
        self.assertEqual(result.returncode, 1)
        self.assertIn('The update feed has no build.', result.stderr)

    def test_an_app_that_cannot_pair_with_a_code_is_replaced(self):
        self.make_app(self.applications[1] / 'Coconut.app', OLD_TOOL)
        result = self.run_script(CODE, feed=self.feed(512))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('is too old for this setup, so Coconut installs the newest version', result.stdout)
        self.assertIn('pair <code>', (self.applications[1] / 'Coconut.app/Contents/Helpers/coconut').read_text())
        self.assertFalse((self.applications[0] / 'Coconut.app').exists())
        self.assertIn(f'paired {CODE}', result.stdout)

    def test_an_earlier_app_in_a_protected_folder_stays_and_the_new_app_goes_in_a_writable_one(self):
        self.make_app(self.applications[0] / 'Coconut.app', OLD_TOOL)
        self.applications[0].chmod(0o555)
        try:
            result = self.run_script(CODE, feed=self.feed(512))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('pair <code>', (self.applications[1] / 'Coconut.app/Contents/Helpers/coconut').read_text())
            self.assertIn(f'paired {CODE}', result.stdout)
            # A second run takes the app that can pair, and downloads nothing.
            again = self.run_script(CODE)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn(f'Coconut.app is already in {self.applications[1]}.', again.stdout)
        finally:
            self.applications[0].chmod(0o755)

    def test_an_app_that_cannot_set_up_by_itself_is_replaced_when_the_service_does_not_run(self):
        self.make_app(self.applications[0] / 'Coconut.app', NO_SETUP_TOOL)
        result = self.run_script(CODE, feed=self.feed(512))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('is too old for this setup', result.stdout)
        self.assertIn('--set-up', (self.applications[0] / 'Coconut.app/Contents/Helpers/coconut').read_text())
        self.assertIn(f'paired {CODE}', result.stdout)

    def test_a_feed_without_code_pairing_stops_before_a_false_pairing(self):
        result = self.run_script(CODE, feed=self.feed(512, tool=OLD_TOOL))
        self.assertEqual(result.returncode, 1)
        self.assertIn('is too old for this setup. A newer build adds what it needs.', result.stderr)
        self.assertNotIn('Setup link', result.stdout)

    def test_a_changed_download_installs_nothing(self):
        feed = self.feed(512)
        archive = next(self.folder.glob('Coconut-0.1.0-512-*.zip'))
        archive.write_bytes(archive.read_bytes()[:-1] + b'x')
        result = self.run_script(CODE, feed=feed)
        self.assertEqual(result.returncode, 1)
        self.assertIn('wrong SHA-256', result.stderr)
        self.assertFalse(any((folder / 'Coconut.app').exists() for folder in self.applications))

    def test_an_app_that_fails_a_signature_check_installs_nothing(self):
        feed = self.feed(512)
        for environment, message in [({'TEST_CODESIGN_VERIFY': '1'}, 'invalid code signature'),
                                     ({'TEST_TEAM': 'AAAAAAAAAA'}, 'not signed by Gumroad'),
                                     ({'TEST_SPCTL': '3'}, 'Gatekeeper refused')]:
            with self.subTest(message):
                result = self.run_script(CODE, feed=feed, **environment)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertFalse(any((folder / 'Coconut.app').exists() for folder in self.applications))

    def test_without_write_access_to_the_first_folder_the_app_goes_in_the_second(self):
        self.applications[0].mkdir()
        self.applications[0].chmod(0o555)
        try:
            result = self.run_script(CODE, feed=self.feed(512))
        finally:
            self.applications[0].chmod(0o755)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.applications[1] / 'Coconut.app').is_dir())
        self.assertIn('home\\ dir/Applications/Coconut.app/Contents/Helpers/coconut settings', self.note(result))

    def test_a_second_run_keeps_the_installed_app_and_pairs(self):
        self.assertEqual(self.run_script(CODE, feed=self.feed(512)).returncode, 0)
        self.log.write_text('')
        # The feed URL does not exist, so a second download would fail.
        result = self.run_script(CODE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Coconut.app is already in', result.stdout)
        # The service runs, so the app opens only for its automatic updates, without the setup.
        self.assertEqual(self.log.read_text().splitlines()[0], f'open -g {self.applications[0]}/Coconut.app')
        self.assertIn(f'paired {CODE}', result.stdout)

    def test_without_a_code_the_script_only_installs(self):
        result = self.run_script(feed=self.feed(512))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.note(result).startswith('Coconut: Coconut is installed. Automatic updates are on.'))
        self.assertIn('To pair an iPhone', result.stdout)
        self.assertNotIn('paired', result.stdout)

    def test_an_invalid_code_or_option_stops_before_the_download(self):
        for arguments, message in [(['not-a-code'], 'not a Coconut pairing code'), (['--channel', 'nightly', CODE], 'Unknown option'),
                                   (['--force', CODE], 'Unknown option')]:
            with self.subTest(arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_the_script_passes_shellcheck(self):
        import shutil
        import subprocess
        if not shutil.which('shellcheck'):
            self.skipTest('shellcheck is not installed.')
        subprocess.run(['shellcheck', str(mac_feed.ROOT / 'updates/install.sh')], check=True)


if __name__ == '__main__':
    unittest.main()
