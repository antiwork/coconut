"""The live activity of a running turn: what the agent does now, the files it touches and the images it sees.

The plugin reports each tool call of a Coconut turn twice (hermes_plugin/coconut/hooks.py): `tool` before the call
and `tool_result` after it, both to `POST /v1/gateway/events`. The bridge gives them to `TurnActivity.record_activity`,
which keeps one bounded feed for each task:

- `entries`: the newest tool calls, each with its phase, one short line (the command, the file, the query or the page),
  its state and its time.
- `files`: the files that the turn read or edited, newest last.
- `images`: small JPEG copies of the newest browser screenshots, vision images and generated images. The plugin makes
  each copy (`thumbnail`), so the service never opens an image of a tool.

Every text passes `redact` in the plugin and again in the service, so a token in a command never reaches the feed.
A feed never holds the content of a file or the output of a command.

The feed of a task lives in `<data>/activity/<task ID>/`: `feed.json` and one `.jpg` for each image. A new turn of the
task starts a new feed. A feed goes `KEPT_SECONDS` after its turn ended, and `STALE_SECONDS` after its last change when
no end came (`ActivityFeeds.sweep`).

The app reads `GET /v1/tasks/<task>/activity` while its live panel is open, and again for each `activity_changed` of
`GET /v1/updates`. The top of this module imports only the standard library, because the plugin loads the file by its
path (tools.backend).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

# The bounds of one feed.
ENTRIES = 80
FILES = 40
IMAGES = 12
# The longest line of an entry, and the longest path or address.
LINE_CHARS = 240
PATH_CHARS = 400
# One image copy: its longest side in points, and its size.
IMAGE_SIDE = 640
IMAGE_BYTES = 90_000
# The images of one tool call.
CALL_IMAGES = 2
# The largest source image that the plugin opens.
SOURCE_BYTES = 20 * 1024 * 1024
# A feed stays this long after its turn ended, so the person can still look at what a finished turn did.
KEPT_SECONDS = 6 * 3600
# A feed without an end, for example after a crash of the gateway, goes this long after its last change.
STALE_SECONDS = 24 * 3600
# The time between two sweeps of the folder.
SWEEP_SECONDS = 600
# The feeds that the service holds in memory. The others load from their file.
LOADED = 64
# A preview of a file that the turn touched.
PREVIEW_BYTES = 64 * 1024

REDACTED = '[redacted]'
# A name that says that its value is a secret. `max_tokens` and `author` are no such names.
_NAME = (r'[A-Za-z0-9_.-]*(?:secret|token(?!s)|passw(?:or)?d|passphrase|api[_-]?key|apikey|access[_-]?key|'
         r'private[_-]?key|credential|authorization|cookie|session[_-]?id|signature)[A-Za-z0-9_.-]*')
_VALUE = r'''(?:"[^"]*"|'[^']*'|[^\s&;|'"]+)'''
_PATTERNS = (
    # A private key block.
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)', re.DOTALL), REDACTED),
    # An Authorization header and a bare bearer token.
    (re.compile(r'(?i)\b(authorization\s*[:=]\s*["\']?\s*(?:bearer|basic|token)?\s*)[^\s"\']+'), r'\1' + REDACTED),
    (re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}'), r'\1' + REDACTED),
    # The user and the password of an address.
    (re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@'), r'\1' + REDACTED + '@'),
    # NAME=value, NAME: value and "name": "value" where the name says that the value is a secret.
    (re.compile(r'(?i)(["\']?\b' + _NAME + r'["\']?\s*[=:]\s*)' + _VALUE), r'\1' + REDACTED),
    # --token value and --password=value.
    (re.compile(r'(?i)(--?' + _NAME + r'[ =])' + _VALUE), r'\1' + REDACTED),
    # The tokens of common services, by their shape.
    (re.compile(r'\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{16,}'), REDACTED),
    (re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}'), REDACTED),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), REDACTED),
    (re.compile(r'\bglpat-[A-Za-z0-9_-]{16,}'), REDACTED),
    (re.compile(r'\bxox[abeprs]-[A-Za-z0-9-]{10,}'), REDACTED),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), REDACTED),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{30,}'), REDACTED),
    (re.compile(r'\bhf_[A-Za-z0-9]{24,}'), REDACTED),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}'), REDACTED),
)


def redact(text) -> str:
    """A text without the secrets that it names: tokens, passwords, keys, and the values of settings whose name says
    that they are secret. The rest of the text stays, so the line still says what the tool call does."""
    text = text if isinstance(text, str) else ''
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def one_line(text, limit=LINE_CHARS) -> str:
    """A redacted text as one line of at most `limit` characters."""
    line = ' '.join(redact(text).split())
    return line if len(line) <= limit else line[:limit - 1] + '…'


IMAGE_NAME = re.compile(r'\.(?:png|jpe?g|webp|gif|heic)$', re.IGNORECASE)
# An absolute path of an image in a text, for example in the result of a browser tool.
_IMAGE_PATH = re.compile(r'(?<![\w.])(~?/[^\s"\'<>|:*?\\]+\.(?:png|jpe?g|webp|gif))(?![\w/])', re.IGNORECASE)
# The keys of a tool result or of its arguments that name an image.
_IMAGE_KEYS = ('screenshot_path', 'screenshot', 'host_image', 'image', 'image_path', 'image_url', 'output_path',
               'file_path', 'path', 'images')


def makes_images(tool) -> bool:
    """Whether a tool shows or makes images: the browser, the vision tool, the image tools and the desktop."""
    name = str(tool or '').lower()
    return (name.startswith(('browser', 'vision', 'image', 'computer_use', 'screenshot'))
            or any(word in name for word in ('__browser', 'screenshot', 'image_gen', 'generate_image')))


def image_sources(tool, args, result) -> list[str]:
    """The local image files of a tool call, newest last and at most CALL_IMAGES: a path in its arguments or in its
    result. Only a tool that shows or makes images counts (makes_images), so a file list of a command adds none."""
    if not makes_images(tool):
        return []
    found: list[str] = []

    def add(value):
        if isinstance(value, str):
            value = value.strip()
            if value.startswith('file://'):
                value = value[7:]
            if value.startswith(('/', '~')) and IMAGE_NAME.search(value) and len(value) <= 1024:
                found.append(value)
        elif isinstance(value, list):
            for item in value[:8]:
                add(item.get('path') or item.get('url') if isinstance(item, dict) else item)

    def walk(value, depth=0):
        if isinstance(value, dict) and depth < 4:
            for key in _IMAGE_KEYS:
                add(value.get(key))
            for inner in value.values():
                if isinstance(inner, (dict, list)):
                    walk(inner, depth + 1)
        elif isinstance(value, list) and depth < 4:
            for inner in value[:8]:
                walk(inner, depth + 1)

    walk(args)
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = None
        if not isinstance(parsed, (dict, list)):
            # A text result, for example of a browser tool that prints the path of its screenshot.
            found.extend(match.group(1) for match in _IMAGE_PATH.finditer(result[:200_000]))
    walk(parsed)
    unique = list(dict.fromkeys(os.path.expanduser(path) for path in found))
    return unique[-CALL_IMAGES:]


def thumbnail(path) -> bytes | None:
    """A small JPEG copy of an image file, or None when the file is no image that Pillow opens. Pillow ships with
    Hermes, and the plugin calls this in the gateway. The copy holds at most IMAGE_BYTES."""
    try:
        from io import BytesIO
        from PIL import Image
        info = os.stat(path)
        if info.st_size <= 0 or info.st_size > SOURCE_BYTES:
            return None
        with Image.open(path) as image:
            if image.width * image.height > 60_000_000:
                return None
            image.draft('RGB', (IMAGE_SIDE, IMAGE_SIDE))
            # A page screenshot can be very tall. The copy shows its top, as the browser does.
            if image.height > image.width * 2:
                image = image.crop((0, 0, image.width, image.width * 2))
            image.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
            if image.mode in ('RGBA', 'LA', 'P'):
                # A JPEG has no transparency. The copy stands on white.
                image = image.convert('RGBA')
                paper = Image.new('RGB', image.size, (255, 255, 255))
                paper.paste(image, mask=image.getchannel('A'))
                image = paper
            else:
                image = image.convert('RGB')
            for quality in (72, 55, 40, 28):
                output = BytesIO()
                image.save(output, format='JPEG', quality=quality, optimize=True)
                if output.tell() <= IMAGE_BYTES:
                    return output.getvalue()
    except Exception:
        return None
    return None


def describe(tool, args) -> dict:
    """The fields of an entry that say what a tool call does: its phase, one line, and its file or its page."""
    try:
        from .turn_phase import tool_phase
    except ImportError:
        # The plugin loads this file by its path, without its package.
        tool_phase = None
    args = args if isinstance(args, dict) else {}
    name = str(tool or '')[:80]
    phase = tool_phase(name, args)[0] if tool_phase else 'working'

    def first(*keys):
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if key == 'urls' and isinstance(value, list) and value and isinstance(value[0], str):
                return value[0].strip()
        return ''

    entry = {'tool': name, 'phase': phase}
    if phase in ('reading', 'editing'):
        path = redact(first('path', 'file_path'))[:PATH_CHARS]
        entry.update(detail=one_line(path), path=path)
    elif phase in ('command', 'testing', 'github') and first('command', 'code'):
        entry['detail'] = one_line(first('command', 'code'))
    elif phase == 'searching':
        entry['detail'] = one_line(first('pattern', 'query'))
        if first('path'):
            entry['path'] = redact(first('path'))[:PATH_CHARS]
    elif phase == 'browsing':
        url = first('url', 'urls')
        if url:
            entry.update(detail=one_line(url), url=redact(url)[:PATH_CHARS])
        else:
            entry['detail'] = one_line(first('query', 'command', 'code'))
    else:
        entry['detail'] = one_line(first('image_url', 'path', 'query', 'command', 'code', 'url'))
    return entry


TASK_ID = re.compile(r'[A-Za-z0-9-]{8,64}')
IMAGE_ID = re.compile(r'[0-9a-f]{32}')


def stamp(seconds) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds)) + ('.%03dZ' % int(seconds % 1 * 1000))


class ActivityFeeds:
    """The feeds of the tasks, in memory and in `<root>/<task ID>/`. Every method is safe from any thread."""

    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self.lock = threading.Lock()
        self.feeds: dict[str, dict] = {}
        self.swept = 0.0

    # Reads.

    def view(self, task_id) -> dict | None:
        """The feed of a task as the app reads it, or None."""
        with self.lock:
            feed = self._feed(task_id)
            if feed is None:
                return None
            return {'task_id': task_id, 'revision': feed['revision'], 'started_at': stamp(feed['started']),
                    'ended_at': stamp(feed['ended']) if feed.get('ended') else None,
                    'entries': [dict(entry) for entry in feed['entries']],
                    'files': [dict(row) for row in feed['files']],
                    'images': [dict(image) for image in feed['images']]}

    def image(self, task_id, image_id) -> bytes | None:
        """The JPEG of an image of a feed, or None."""
        with self.lock:
            feed = self._feed(task_id)
            if feed is None or not any(image['id'] == image_id for image in feed['images']):
                return None
            try:
                return (self.root / task_id / (image_id + '.jpg')).read_bytes()
            except OSError:
                return None

    def touched(self, task_id, path) -> bool:
        """Whether the turn of a task read or edited this file."""
        with self.lock:
            feed = self._feed(task_id)
            return feed is not None and any(row['path'] == path for row in feed['files'])

    # Writes. Each returns True when the feed changed.

    def start(self, task_id, call_id, tool, args) -> bool:
        """A tool call starts."""
        if not TASK_ID.fullmatch(str(task_id or '')) or not tool:
            return False
        now = self.clock()
        with self.lock:
            feed = self._feed(task_id, create=True, now=now)
            call_id = str(call_id or '')[:120]
            if call_id and any(entry.get('call_id') == call_id for entry in feed['entries']):
                # The result of a fast call can come before its start.
                return False
            self._add(feed, call_id, tool, args, now)
            self._save(task_id, feed)
            return True

    def finish(self, task_id, call_id, tool, args=None, status='ok', duration_ms=None, error='', images=()) -> bool:
        """A tool call ended. `images` holds (name, JPEG bytes) of at most CALL_IMAGES images."""
        if not TASK_ID.fullmatch(str(task_id or '')) or not tool:
            return False
        now = self.clock()
        with self.lock:
            feed = self._feed(task_id)
            if feed is not None and feed.get('ended'):
                # A late result of a turn that ended. Its feed stays as it is.
                return False
            if feed is None:
                feed = self._feed(task_id, create=True, now=now)
            call_id = str(call_id or '')[:120]
            running = [entry for entry in feed['entries'] if entry['status'] == 'running']
            entry = (next((e for e in feed['entries'] if call_id and e.get('call_id') == call_id), None)
                     # A Hermes without call IDs: the oldest running call of this tool.
                     or (None if call_id else next((e for e in running if e['tool'] == str(tool)[:80]), None))
                     or self._add(feed, call_id, tool, args, now))
            entry['status'] = 'error' if status == 'error' else 'ok'
            if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and duration_ms >= 0:
                entry['seconds'] = round(min(duration_ms, 86_400_000) / 1000, 1)
            if entry['status'] == 'error' and error:
                entry['error'] = one_line(error, 160)
            folder = self.root / task_id
            for name, content in list(images)[:CALL_IMAGES]:
                if (not isinstance(content, bytes) or not content.startswith(b'\xff\xd8\xff')
                        or len(content) > IMAGE_BYTES):
                    continue
                identity = uuid.uuid4().hex
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / (identity + '.jpg')).write_bytes(content)
                except OSError:
                    continue
                feed['images'].append({'id': identity, 'entry': entry['id'], 'tool': entry['tool'],
                                       'name': one_line(os.path.basename(str(name or '')), 120), 'at': stamp(now)})
                entry.setdefault('images', []).append(identity)
            for old in feed['images'][:-IMAGES]:
                self._drop_image(task_id, feed, old['id'])
            feed['revision'] += 1
            feed['changed'] = now
            self._save(task_id, feed)
            return True

    def end(self, task_id) -> bool:
        """The turn of a task ended. Its feed stays for KEPT_SECONDS."""
        with self.lock:
            feed = self.feeds.get(task_id)
            if feed is None or feed.get('ended'):
                return False
            now = self.clock()
            for entry in feed['entries']:
                if entry['status'] == 'running':
                    # A call without a result, for example a call that the turn's end cut off.
                    entry['status'] = 'ok'
            feed.update(ended=now, changed=now, revision=feed['revision'] + 1)
            self._save(task_id, feed)
            return True

    def sweep(self, force=False) -> list[str]:
        """Remove each feed whose time is over. Return the tasks of the removed feeds."""
        now = self.clock()
        with self.lock:
            if not force and now - self.swept < SWEEP_SECONDS:
                return []
            self.swept = now
            removed = []
            try:
                folders = [path for path in self.root.iterdir() if path.is_dir() and not path.is_symlink()]
            except OSError:
                folders = []
            for folder in folders:
                feed = self.feeds.get(folder.name) or self._read(folder.name)
                if feed is not None:
                    over = (now - feed['ended'] > KEPT_SECONDS if feed.get('ended')
                            else now - feed.get('changed', feed['started']) > STALE_SECONDS)
                else:
                    # A folder without a readable feed, for example a write that a crash cut off.
                    try:
                        over = now - folder.stat().st_mtime > SWEEP_SECONDS
                    except OSError:
                        over = True
                if over:
                    shutil.rmtree(folder, ignore_errors=True)
                    self.feeds.pop(folder.name, None)
                    removed.append(folder.name)
            return removed

    # The parts. The caller holds the lock.

    def _feed(self, task_id, create=False, now=None):
        if not TASK_ID.fullmatch(str(task_id or '')):
            return None
        feed = self.feeds.get(task_id)
        if feed is None:
            feed = self._read(task_id)
            if feed is not None:
                self._hold(task_id, feed)
        if create and (feed is None or feed.get('ended')):
            # A new turn of the task starts a new feed, without the images of the turn before.
            shutil.rmtree(self.root / task_id, ignore_errors=True)
            feed = {'revision': 0, 'next': 1, 'started': now, 'changed': now, 'ended': None,
                    'entries': [], 'files': [], 'images': []}
            self._hold(task_id, feed)
        return feed

    def _hold(self, task_id, feed):
        self.feeds.pop(task_id, None)
        self.feeds[task_id] = feed
        while len(self.feeds) > LOADED:
            del self.feeds[next(iter(self.feeds))]

    def _add(self, feed, call_id, tool, args, now):
        entry = dict(describe(tool, args), id=feed['next'], status='running', at=stamp(now))
        if call_id:
            entry['call_id'] = call_id
        feed['next'] += 1
        feed['entries'].append(entry)
        for old in feed['entries'][:-ENTRIES]:
            # The images of an entry stay in the film strip after the entry left the list.
            feed['entries'].remove(old)
        path = entry.get('path')
        if path and entry['phase'] in ('reading', 'editing'):
            action = 'edit' if entry['phase'] == 'editing' else 'read'
            row = next((row for row in feed['files'] if row['path'] == path), None)
            if row is not None:
                feed['files'].remove(row)
                row.update(count=row['count'] + 1, at=entry['at'],
                           action='edit' if 'edit' in (row['action'], action) else 'read')
            else:
                row = {'path': path, 'action': action, 'count': 1, 'at': entry['at']}
            feed['files'].append(row)
            del feed['files'][:-FILES]
        feed['revision'] += 1
        feed['changed'] = now
        return entry

    def _drop_image(self, task_id, feed, image_id):
        feed['images'] = [image for image in feed['images'] if image['id'] != image_id]
        for entry in feed['entries']:
            if image_id in entry.get('images', ()):
                entry['images'].remove(image_id)
        try:
            (self.root / task_id / (image_id + '.jpg')).unlink()
        except OSError:
            pass

    def _save(self, task_id, feed):
        folder = self.root / task_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / 'feed.json.new'
            temporary.write_text(json.dumps(feed, separators=(',', ':')))
            os.replace(temporary, folder / 'feed.json')
        except OSError:
            # The feed stays in memory. A restart of the service loses it.
            pass

    def _read(self, task_id):
        try:
            feed = json.loads((self.root / task_id / 'feed.json').read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(feed, dict) or not all(isinstance(feed.get(key), list) for key in ('entries', 'files', 'images')):
            return None
        if not all(isinstance(feed.get(key), (int, float)) for key in ('revision', 'next', 'started')):
            return None
        return feed


# The names of files that hold secrets. The app shows no preview of them.
_SECRET_FOLDERS = ('.ssh', '.gnupg', '.aws', '.kube', '.docker', 'keychains', 'secrets')
_SECRET_NAME = re.compile(
    r'(?i)^(?:\.env.*|.*\.env|\.netrc|\.npmrc|\.pypirc|\.git-credentials|auth\.json|bridge\.json|credentials.*|'
    r'secrets?\..*|.*\.(?:pem|key|p12|pfx|keystore|jks|mobileprovision|p8)|id_(?:rsa|dsa|ecdsa|ed25519).*)$')
# A word in a name that says the same, except in the name of a source file, such as TokenStore.swift.
_SECRET_WORD = re.compile(r'(?i)secret|token|password|credential')
_SOURCE_NAME = re.compile(r'(?i)\.(?:py|swift|js|jsx|ts|tsx|go|rs|rb|java|kt|c|h|cc|cpp|m|mm|cs|php|sh|md|html|css|sql)$')


def secret_file(path) -> bool:
    """Whether the name or the folder of a file says that it holds secrets."""
    parts = Path(path).parts
    name = parts[-1] if parts else ''
    return (bool(_SECRET_NAME.match(name)) or any(part.lower() in _SECRET_FOLDERS for part in parts[:-1])
            or bool(_SECRET_WORD.search(name) and not _SOURCE_NAME.search(name)))


class TurnActivity:
    """The part of the service that keeps the live activity of its turns."""

    def _init_turn_activity(self):
        self.activity = ActivityFeeds(self.root / 'activity')
        # The feeds of turns that ended before this start are over when their time is.
        self.activity.sweep(force=True)

    def record_activity(self, task_id, event) -> None:
        """Take a `tool` or a `tool_result` event of a running turn from the bridge."""
        import base64
        if not isinstance(event, dict) or not isinstance(event.get('tool'), str) or not event['tool']:
            return
        call = event.get('call_id') if isinstance(event.get('call_id'), str) else ''
        if event.get('kind') == 'tool':
            changed = self.activity.start(task_id, call, event['tool'], event.get('args'))
        else:
            images = []
            for image in (event.get('images') if isinstance(event.get('images'), list) else [])[:CALL_IMAGES]:
                try:
                    images.append((image.get('name'), base64.b64decode(image.get('jpeg') or '', validate=True)))
                except (AttributeError, TypeError, ValueError):
                    continue
            changed = self.activity.finish(
                task_id, call, event['tool'], event.get('args'), event.get('status'), event.get('duration_ms'),
                event.get('error') if isinstance(event.get('error'), str) else '', images)
        if changed:
            self._activity_changed(task_id)
        self.activity.sweep()

    def end_activity(self, task_id) -> None:
        """The turn of a task ended. `_put` calls this in its transaction, so the event joins it."""
        # A start of the service writes tasks before it has its feeds.
        feeds = getattr(self, 'activity', None)
        if feeds is not None and feeds.end(task_id):
            self._event('activity.updated', {'task': task_id})

    def _activity_changed(self, task_id):
        with self.transaction():
            # The event names its task in its data only: an update with the task would send the whole task again.
            self._event('activity.updated', {'task': task_id})

    def _activity_task(self, task_id):
        from .service import ServiceError
        with self.lock:
            task = self._get('tasks', task_id)
            if self._get('bots', task['bot_id']).get('_deleted'):
                raise ServiceError(404, 'Task was not found.')
        return task

    def task_activity(self, task_id: str) -> dict:
        """GET /v1/tasks/<task>/activity: the feed of the newest turn of a task. A task without a feed has an empty one."""
        self._activity_task(task_id)
        return self.activity.view(task_id) or {'task_id': task_id, 'revision': 0, 'started_at': None, 'ended_at': None,
                                               'entries': [], 'files': [], 'images': []}

    def activity_image(self, task_id: str, image_id: str) -> bytes:
        """GET /v1/tasks/<task>/activity/images/<image>: the JPEG copy of an image of the feed."""
        from .service import ServiceError
        self._activity_task(task_id)
        content = self.activity.image(task_id, image_id) if IMAGE_ID.fullmatch(image_id) else None
        if content is None:
            raise ServiceError(404, 'This image is no longer kept.')
        return content

    def activity_file(self, task_id: str, path: str) -> dict:
        """GET /v1/tasks/<task>/activity/file?path=: the start of a text file that the turn read or edited, as it is
        on the Mac now. Only a file of the feed opens, never a file whose name says that it holds secrets, and its
        text passes `redact`."""
        from .service import ServiceError
        self._activity_task(task_id)
        if not isinstance(path, str) or not self.activity.touched(task_id, path):
            raise ServiceError(404, 'This turn did not touch this file.')
        place = os.path.expanduser(path)
        if not os.path.isabs(place):
            raise ServiceError(404, 'The Mac cannot find this file from its short path.')
        if secret_file(place) or secret_file(os.path.realpath(place)):
            raise ServiceError(403, 'Coconut shows no preview of a file that can hold secrets.')
        try:
            descriptor = os.open(place, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                import stat
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ServiceError(404, 'This is not a file.')
                content = source.read(PREVIEW_BYTES + 1)
        except OSError:
            raise ServiceError(404, 'The file is not on the Mac any more.') from None
        truncated = len(content) > PREVIEW_BYTES
        content = content[:PREVIEW_BYTES]
        if b'\x00' in content:
            raise ServiceError(415, 'This file is not text.')
        text = content.decode('utf-8', errors='ignore' if truncated else 'strict') if _is_text(content, truncated) else None
        if text is None:
            raise ServiceError(415, 'This file is not text.')
        return {'path': path, 'name': os.path.basename(place), 'size': info.st_size, 'truncated': truncated,
                'text': redact(text)}


def _is_text(content, truncated) -> bool:
    try:
        content.decode('utf-8')
        return True
    except UnicodeDecodeError as error:
        # A cut in the middle of a character is no fault of the file.
        return truncated and error.start >= len(content) - 4
