#!/usr/bin/env python3
"""Make the Sparkle feed of Coconut.app: the assets that the Worker coconut-updates (updates/) serves.

The Worker keeps no files between deployments, so each release builds the complete assets folder:

  mac/appcast.xml                     the feed, with the newest KEEP items of each channel
  mac/Coconut-<version>-<build>[-nightly]-<digest>.zip
                                      the archives of those items: the new one, and the others from the live feed.
                                      <digest> is the start of the SHA-256 of the file, so each file has its own URL.
  _headers                            the content types and cache times (updates/_headers)
  _redirects                          /mac/download to the newest archive for everyone (stable, else nightly)
  install                             the install script that the iPhone's message for Hermes runs (updates/install.sh)

  mac_feed.py latest-build --channel nightly
  printf '%s' "$SPARKLE_ED_PRIVATE_KEY" | mac_feed.py check-key
  mac_feed.py publish --archive Coconut-0.1.0-512-nightly.zip --channel nightly --signature <EdDSA> --output updates/public
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
import argparse
import base64
import hashlib
import plistlib
import re
import shutil
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile

from mac_app import CHANNELS, ED_PUBLIC_KEY, FEED_URL, MINIMUM_SYSTEM, ROOT

SPARKLE = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
ElementTree.register_namespace('sparkle', SPARKLE)
# Each channel keeps this many items. An app that is older than all of them still updates to the newest one.
KEEP = 5
BASE_URL = FEED_URL.rsplit('/', 1)[0]
# The hex digits of the SHA-256 that end each published archive name.
DIGEST_LENGTH = 16


def tag(name: str) -> str:
    return f'{{{SPARKLE}}}{name}'


@dataclass(frozen=True)
class Item:
    version: str
    build: int
    channel: str
    file: str
    length: int
    signature: str
    published: str
    minimum_system: str = MINIMUM_SYSTEM

    def element(self) -> ElementTree.Element:
        item = ElementTree.Element('item')
        ElementTree.SubElement(item, 'title').text = f'Coconut {self.version} ({self.build})'
        ElementTree.SubElement(item, 'pubDate').text = self.published
        ElementTree.SubElement(item, tag('version')).text = str(self.build)
        ElementTree.SubElement(item, tag('shortVersionString')).text = self.version
        ElementTree.SubElement(item, tag('minimumSystemVersion')).text = self.minimum_system
        if self.channel != 'stable':
            ElementTree.SubElement(item, tag('channel')).text = self.channel
        ElementTree.SubElement(item, 'enclosure', {
            'url': f'{BASE_URL}/{self.file}', 'length': str(self.length), 'type': 'application/octet-stream',
            tag('edSignature'): self.signature})
        return item


def parse(data: bytes) -> list[Item]:
    """Read the items of a feed that this script wrote. An empty feed has no items."""
    if not data.strip():
        return []
    items = []
    for element in ElementTree.fromstring(data).iter('item'):
        enclosure = element.find('enclosure')
        url = enclosure.get('url', '')
        if not url.startswith(BASE_URL + '/'):
            raise SystemExit(f'The feed names an archive outside {BASE_URL}: {url}')
        items.append(Item(version=element.findtext(tag('shortVersionString'), ''),
                          build=int(element.findtext(tag('version'), '0')),
                          channel=element.findtext(tag('channel')) or 'stable',
                          file=url.removeprefix(BASE_URL + '/'), length=int(enclosure.get('length', '0')),
                          signature=enclosure.get(tag('edSignature'), ''), published=element.findtext('pubDate', ''),
                          minimum_system=element.findtext(tag('minimumSystemVersion')) or MINIMUM_SYSTEM))
    return items


def merge(items: list[Item], new: Item, keep: int = KEEP) -> list[Item]:
    """Add the new item, replace an item of the same channel and build, and keep the newest `keep` of each channel."""
    items = [item for item in items if (item.channel, item.build) != (new.channel, new.build)] + [new]
    kept = []
    for channel in CHANNELS:
        kept += sorted((item for item in items if item.channel == channel), key=lambda item: item.build, reverse=True)[:keep]
    return sorted(kept, key=lambda item: (item.build, item.channel == 'stable'), reverse=True)


def render(items: list[Item]) -> bytes:
    feed = ElementTree.Element('rss', {'version': '2.0'})
    channel = ElementTree.SubElement(feed, 'channel')
    ElementTree.SubElement(channel, 'title').text = 'Coconut'
    ElementTree.SubElement(channel, 'link').text = FEED_URL
    ElementTree.SubElement(channel, 'description').text = 'Updates of Coconut.app'
    channel.extend(item.element() for item in items)
    ElementTree.indent(feed)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ElementTree.tostring(feed, encoding='utf-8') + b'\n'


def fetch(url: str) -> bytes | None:
    """Return the body of a live feed file, or None when the Worker has no such file."""
    # Cloudflare refuses the default user agent of urllib with 403.
    headers = {'Cache-Control': 'no-cache', 'User-Agent': 'coconut-mac-release/1'}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def app_info(archive: Path) -> dict:
    """The Info.plist of the app in an archive."""
    with zipfile.ZipFile(archive) as bundle:
        return plistlib.loads(bundle.read('Coconut.app/Contents/Info.plist'))


def published_name(archive: Path) -> str:
    """The archive name with the start of its SHA-256. A rebuild of the same build gets a new URL, so no client can
    combine a cached earlier file with the signature of the new one."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()[:DIGEST_LENGTH]
    return f'{archive.stem}-{digest}{archive.suffix}'


def matches_name(name: str, data: bytes) -> bool:
    """True when the data is the file that the name promises: the name ends with the start of its SHA-256."""
    match = re.search(rf'-([0-9a-f]{{{DIGEST_LENGTH}}})\.zip$', name)
    return match is not None and hashlib.sha256(data).hexdigest().startswith(match[1])


def new_item(archive: Path, channel: str, signature: str, now: datetime | None = None) -> Item:
    info = app_info(archive)
    return Item(version=info['CFBundleShortVersionString'], build=int(info['CFBundleVersion']), channel=channel,
                file=published_name(archive), length=archive.stat().st_size, signature=signature,
                published=format_datetime(now or datetime.now(timezone.utc)),
                minimum_system=info.get('LSMinimumSystemVersion', MINIMUM_SYSTEM))


def publish(archive: Path, channel: str, signature: str, output: Path, *, feed: bytes | None = None,
            download=fetch) -> list[Item]:
    """Write the complete assets folder for a release. Return the items of the new feed."""
    if not signature:
        raise SystemExit('The archive needs its EdDSA signature from sign_update.')
    new = new_item(archive, channel, signature)
    previous = parse(feed or b'')
    items = merge(previous, new)
    if output.exists():
        shutil.rmtree(output)
    folder = output / 'mac'
    folder.mkdir(parents=True)
    shutil.copy2(ROOT / 'updates/_headers', output / '_headers')
    shutil.copy2(ROOT / 'updates/install.sh', output / 'install')
    (output / '_redirects').write_text(redirects(items))
    # Clients can keep the earlier appcast for its cache time. The archives that it names stay until the next release,
    # also the ones that the new appcast drops.
    files = {item.file: item for item in [*items, *previous]}
    for item in files.values():
        target = folder / Path(item.file).name
        if item.file == new.file:
            shutil.copy2(archive, target)
            continue
        data = download(f'{BASE_URL}/{item.file}')
        # A kept archive must be the file that its signature covers. A changed or missing file stops the release.
        if data is None or len(data) != item.length or not matches_name(item.file, data):
            raise SystemExit(f'The live feed has no complete {item.file}. Coconut did not publish a feed that names it.')
        target.write_bytes(data)
    (folder / 'appcast.xml').write_bytes(render(items))
    return items


def redirects(items: list[Item]) -> str:
    """Point /mac/download at the newest stable archive, or at the newest nightly archive before the first stable
    release. A person who downloads the app by hand uses this link. The redirect is temporary: it changes with each
    release."""
    newest = next((item for channel in CHANNELS for item in items if item.channel == channel), None)
    return f'/mac/download /mac/{newest.file} 302\n' if newest else ''


def public_key(private_key: str) -> str:
    """The public key of a private key that Sparkle's generate_keys exported, in base64.

    The export holds the 32-byte seed, or the seed and the public key (64 bytes) in the format of earlier versions.
    This needs the package `cryptography`, which the release job installs.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    data = base64.b64decode(private_key.strip(), validate=True)
    if len(data) not in (32, 64):
        raise SystemExit('The Sparkle private key must hold 32 or 64 bytes.')
    key = Ed25519PrivateKey.from_private_bytes(data[:32]).public_key()
    return base64.b64encode(key.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def check_key(private_key: str) -> None:
    """Stop a release whose signing key does not match SUPublicEDKey. Sparkle would refuse each of its updates."""
    if public_key(private_key) != ED_PUBLIC_KEY:
        raise SystemExit('SPARKLE_ED_PRIVATE_KEY does not belong to SUPublicEDKey in scripts/mac_app.py.')


def latest_build(feed: bytes | None, channel: str) -> int:
    return max((item.build for item in parse(feed or b'') if item.channel == channel), default=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['latest-build', 'publish', 'check-key'])
    parser.add_argument('--channel', choices=CHANNELS, default='stable')
    parser.add_argument('--archive', type=Path, help='publish: the new archive.')
    parser.add_argument('--signature', help='publish: the EdDSA signature of the archive (sign_update -p).')
    parser.add_argument('--output', type=Path, default=ROOT / 'updates/public', help='publish: the assets folder.')
    args = parser.parse_args()
    if args.command == 'check-key':
        check_key(sys.stdin.read())
        print('The Sparkle key matches SUPublicEDKey.')
        return
    feed = fetch(FEED_URL)
    if args.command == 'latest-build':
        print(latest_build(feed, args.channel))
        return
    if args.archive is None:
        parser.error('publish needs --archive.')
    items = publish(args.archive, args.channel, (args.signature or '').strip(), args.output, feed=feed)
    for item in items:
        print(f'{item.channel:8} {item.version} ({item.build})  {item.file}  {item.length} bytes')
    digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    print(f'Published {args.archive.name} (sha256 {digest}) in the {args.channel} channel.', file=sys.stderr)


if __name__ == '__main__':
    main()
