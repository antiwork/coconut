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


def item(build, channel='stable', **changes):
    suffix = '' if channel == 'stable' else '-' + channel
    values = dict(version='0.1.0', build=build, channel=channel, file=f'Coconut-0.1.0-{build}{suffix}.zip', length=build,
                  signature=f'sig{build}', published='Mon, 26 Oct 2026 12:00:00 +0000')
    values.update(changes)
    return mac_feed.Item(**values)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder)

    def archive(self, build, channel='stable', size=2048):
        suffix = '' if channel == 'stable' else '-' + channel
        path = self.folder / f'Coconut-0.1.0-{build}{suffix}.zip'
        with zipfile.ZipFile(path, 'w') as bundle:
            bundle.writestr('Coconut.app/Contents/Info.plist', plistlib.dumps(
                {'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': str(build), 'LSMinimumSystemVersion': '14.0'}))
            bundle.writestr('Coconut.app/Contents/padding', b'x' * size)
        return path

    def test_the_feed_has_the_sparkle_fields_and_marks_only_nightly_items(self):
        feed = ElementTree.fromstring(mac_feed.render([item(12, 'nightly'), item(11)]))
        nightly, stable = feed.findall('channel/item')
        self.assertEqual(nightly.findtext(mac_feed.tag('channel')), 'nightly')
        self.assertIsNone(stable.find(mac_feed.tag('channel')), 'A stable item reaches every app, so it has no channel.')
        self.assertEqual(stable.findtext(mac_feed.tag('version')), '11')
        self.assertEqual(stable.findtext(mac_feed.tag('shortVersionString')), '0.1.0')
        self.assertEqual(stable.findtext(mac_feed.tag('minimumSystemVersion')), '14.0')
        enclosure = stable.find('enclosure')
        self.assertEqual(enclosure.get('url'), f'{BASE}/Coconut-0.1.0-11.zip')
        self.assertEqual(enclosure.get(mac_feed.tag('edSignature')), 'sig11')
        self.assertEqual(enclosure.get('length'), '11')

    def test_a_feed_reads_back_the_items_that_it_holds(self):
        items = [item(12, 'nightly'), item(11)]
        self.assertEqual(mac_feed.parse(mac_feed.render(items)), items)
        self.assertEqual(mac_feed.parse(b''), [])

    def test_a_feed_must_not_name_archives_outside_the_worker(self):
        data = mac_feed.render([item(11)]).replace(BASE.encode(), b'https://example.com/mac')
        with self.assertRaisesRegex(SystemExit, 'outside'):
            mac_feed.parse(data)

    def test_each_channel_keeps_its_newest_items(self):
        items = [item(build, 'nightly') for build in range(1, 7)] + [item(build) for build in (3, 5)]
        merged = mac_feed.merge(items, item(8, 'nightly'), keep=5)
        self.assertEqual([entry.build for entry in merged if entry.channel == 'nightly'], [8, 6, 5, 4, 3])
        self.assertEqual([entry.build for entry in merged if entry.channel == 'stable'], [5, 3])

    def test_a_rebuild_replaces_the_item_of_the_same_channel_and_build(self):
        merged = mac_feed.merge([item(8, 'nightly', signature='old'), item(8)], item(8, 'nightly', signature='new'))
        self.assertEqual([(entry.channel, entry.signature) for entry in merged], [('stable', 'sig8'), ('nightly', 'new')])

    def test_the_latest_build_of_a_channel(self):
        feed = mac_feed.render([item(9), item(8, 'nightly')])
        self.assertEqual(mac_feed.latest_build(feed, 'nightly'), 8)
        self.assertEqual(mac_feed.latest_build(feed, 'stable'), 9)
        self.assertEqual(mac_feed.latest_build(None, 'nightly'), 0)

    def test_the_new_item_reads_the_version_and_the_build_from_the_archive(self):
        archive = self.archive(512, 'nightly')
        new = mac_feed.new_item(archive, 'nightly', 'sig', datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
        self.assertEqual((new.version, new.build, new.file, new.length), ('0.1.0', 512, mac_feed.published_name(archive), archive.stat().st_size))
        self.assertEqual(new.published, 'Mon, 26 Oct 2026 12:00:00 +0000')

    def test_publish_writes_the_complete_assets_folder(self):
        old = self.archive(510, 'nightly')
        feed = mac_feed.render([mac_feed.new_item(old, 'nightly', 'old-signature')])
        new = self.archive(512, 'nightly', size=4096)
        requested = []
        def download(url):
            requested.append(url)
            return old.read_bytes()
        output = self.folder / 'public'
        items = mac_feed.publish(new, 'nightly', 'new-signature', output, feed=feed, download=download)
        self.assertEqual([entry.build for entry in items], [512, 510])
        old_name, new_name = mac_feed.published_name(old), mac_feed.published_name(new)
        self.assertEqual(requested, [f'{BASE}/{old_name}'])
        self.assertEqual(sorted(path.name for path in (output / 'mac').iterdir()), sorted(['appcast.xml', old_name, new_name]))
        self.assertEqual((output / 'mac' / new_name).read_bytes(), new.read_bytes())
        self.assertEqual((output / '_headers').read_text(), (mac_feed.ROOT / 'updates/_headers').read_text())
        self.assertEqual(mac_feed.parse((output / 'mac/appcast.xml').read_bytes()), items)

    def test_each_published_archive_name_ends_with_its_digest(self):
        archive = self.archive(512, 'nightly')
        name = mac_feed.published_name(archive)
        self.assertRegex(name, r'^Coconut-0\.1\.0-512-nightly-[0-9a-f]{16}\.zip$')
        self.assertTrue(mac_feed.matches_name(name, archive.read_bytes()))
        # A rebuild of the same build has other bytes, so it gets another URL.
        rebuilt = self.archive(512, 'nightly', size=2049)
        self.assertNotEqual(mac_feed.published_name(rebuilt), name)
        self.assertFalse(mac_feed.matches_name('Coconut-0.1.0-512.zip', archive.read_bytes()))

    def test_the_archives_of_the_earlier_feed_stay_for_one_more_release(self):
        old = self.archive(512, size=2048)
        feed = mac_feed.render([mac_feed.new_item(old, 'stable', 'old-signature')])
        old_bytes, old_name = old.read_bytes(), mac_feed.published_name(old)
        rebuilt = self.archive(512, size=2049)
        output = self.folder / 'public'
        items = mac_feed.publish(rebuilt, 'stable', 'new-signature', output, feed=feed, download=lambda url: old_bytes)
        # The rebuild replaces the item, but a client with the cached earlier appcast still finds its archive.
        self.assertEqual([entry.file for entry in items], [mac_feed.published_name(rebuilt)])
        self.assertEqual((output / 'mac' / old_name).read_bytes(), old_bytes)
        self.assertNotIn(old_name.encode(), (output / 'mac/appcast.xml').read_bytes())

    def test_publish_stops_when_a_kept_archive_is_missing_or_changed(self):
        old = self.archive(510)
        feed = mac_feed.render([mac_feed.new_item(old, 'stable', 'old-signature')])
        new = self.archive(512)
        same_length = bytearray(old.read_bytes())
        same_length[-1] ^= 1
        for data in (None, b'changed', bytes(same_length)):
            with self.subTest(data=data), self.assertRaisesRegex(SystemExit, 'no complete'):
                mac_feed.publish(new, 'stable', 'sig', self.folder / 'public', feed=feed, download=lambda url: data)

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
            mac_feed.publish(self.archive(512), 'stable', '', self.folder / 'public', feed=None, download=lambda url: None)


if __name__ == '__main__':
    unittest.main()
