"""Open Coconut.app in the background when the Mac misses a build because the app does not run.

Sparkle runs inside Coconut.app (mac/Sources/CoconutApp/Updater.swift). The service runs from the install folder as a
login item and keeps running when the person quits the app, but then nothing reads the feed, and the Mac misses every
build. On 2026-11-24 a Mac stayed on the build of the day before, because Coconut was quit from its menu.

Every 30 minutes, while the app does not run, the service reads the feed of the app (SUFeedURL of its Info.plist). When
the feed has a newer build, the service opens the app with `open -g`, in the background, as `coconut update` does.
Sparkle's check is overdue by then, so Sparkle checks at once and installs without a question
(Updater.updater(_:willInstallUpdateOnQuit:immediateInstallationBlock:)), and the new app installs its source in the
service at launch (AppModel.start), after the running turns finish.

The service opens the app once for each build, so an app that the person quits again stays closed until the next build.
It opens nothing while the person has automatic updates off, and only as the login item of an app
(COCONUT_MANAGED_SERVICE, with the team helper of the app in COCONUT_TEAM_HELPER).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import threading
import time
import urllib.request
from xml.etree import ElementTree

LOG = logging.getLogger(__name__)
CHECK_SECONDS = 30 * 60
# The first check waits, so a service that an update of the app just started does not open the app that relaunches.
FIRST_CHECK_SECONDS = 10 * 60
HTTP_SECONDS = 20
FEED_BYTES = 2 * 1024 * 1024
# Cloudflare refuses Python's own user agent (Error 1010), so the request names Coconut.
HEADERS = {'user-agent': 'Coconut'}
SPARKLE = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'
# Automatic updates in the menu sets both keys of Sparkle (UpdatePreferences.swift). Without a value, they are on.
AUTOMATIC_KEYS = ('SUEnableAutomaticChecks', 'SUAutomaticallyUpdate')
# In the data folder: the build that the service opened the app for last.
OPENED_FILE = 'app-opened.json'


def app_bundle(environment=None):
    """The Coconut.app whose login item runs this service, else None."""
    environment = os.environ if environment is None else environment
    if environment.get('COCONUT_MANAGED_SERVICE') != '1' or not environment.get('COCONUT_TEAM_HELPER'):
        return None
    helper = Path(environment['COCONUT_TEAM_HELPER'])
    app = helper.parent.parent.parent
    if helper.parent.name != 'Helpers' or helper.parent.parent.name != 'Contents' or app.suffix != '.app':
        return None
    return app if (app / 'Contents/Info.plist').is_file() else None


def read_info(app):
    with (app / 'Contents/Info.plist').open('rb') as handle:
        return plistlib.load(handle)


def build_number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def running(executable):
    """Whether a process runs this executable. The Coconut of a simulator runs from another path."""
    output = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, timeout=30, check=True).stdout
    return any(line.strip() == str(executable) for line in output.splitlines())


def automatic_updates(bundle_id):
    """Whether the person has automatic updates on: both keys of Sparkle, which are on without a value."""
    for key in AUTOMATIC_KEYS:
        result = subprocess.run(['/usr/bin/defaults', 'read', bundle_id, key], capture_output=True, text=True,
                                timeout=30)
        if result.returncode == 0 and result.stdout.strip().lower() in ('0', 'false', 'no'):
            return False
    return True


def newest_build(feed):
    """The highest build number in the feed, or None."""
    request = urllib.request.Request(feed, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=HTTP_SECONDS) as response:
        document = ElementTree.fromstring(response.read(FEED_BYTES))
    builds = []
    for item in document.iter('item'):
        enclosure = item.find('enclosure')
        version = item.findtext(f'{SPARKLE}version') or (enclosure.get(f'{SPARKLE}version') if enclosure is not None
                                                          else None)
        if build_number(version) is not None:
            builds.append(build_number(version))
    return max(builds, default=None)


def open_in_background(app):
    subprocess.run(['/usr/bin/open', '-g', str(app)], capture_output=True, timeout=60, check=True)


class AppUpdates:
    """The check of the service that opens Coconut.app for a build that the app misses (see the module)."""

    def __init__(self, data_folder, *, environment=None, opener=None):
        self.data_folder = Path(data_folder)
        self.app = app_bundle(environment)
        self.opener = opener or open_in_background
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        if self.app is None or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._poll, name='coconut-app-updates', daemon=True)
        self.thread.start()

    def _poll(self):
        self.stopping.wait(FIRST_CHECK_SECONDS)
        while not self.stopping.is_set():
            try:
                self.check()
            except Exception:
                LOG.exception('The check for a newer Coconut.app failed. The next check retries.')
            self.stopping.wait(CHECK_SECONDS)

    def check(self):
        """Open the app in the background when it misses a build. Return that build, else None."""
        if self.app is None:
            return None
        info = read_info(self.app)
        if running(self.app / 'Contents/MacOS' / info.get('CFBundleExecutable', 'Coconut')):
            return None
        bundle_id, feed = info.get('CFBundleIdentifier'), info.get('SUFeedURL')
        current = build_number(info.get('CFBundleVersion'))
        if not bundle_id or not feed or current is None or not automatic_updates(bundle_id):
            return None
        newest = newest_build(feed)
        if newest is None or newest <= current or newest == self.opened():
            return None
        self.opener(self.app)
        self.remember(newest)
        LOG.info('Coconut.app %s did not run while build %s was out, so the service opened it in the background.',
                 current, newest)
        return newest

    def opened(self):
        """The build that the service opened the app for last, or None."""
        try:
            return json.loads((self.data_folder / OPENED_FILE).read_text()).get('build')
        except (OSError, ValueError, AttributeError):
            return None

    def remember(self, build):
        path = self.data_folder / OPENED_FILE
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps({'build': build, 'opened_at': time.time()}))
        os.replace(temporary, path)
