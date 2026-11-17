"""The live activity of a turn: the bounds of its feed, the redaction of its texts, its images and its cleanup."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut import turn_activity
from coconut.service import Service, ServiceError
from coconut.turn_activity import ActivityFeeds, describe, image_sources, redact, secret_file, thumbnail
from test_gateway_chat import Adapter, Provider

TASK = '0f4d3c2b-1a09-4f8e-9d7c-6b5a4e3d2c1b'
# The start of a JPEG. The service checks only that a copy is a JPEG of a small size.
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 200


def png(path, size=(1600, 1000), mode='RGB'):
    from PIL import Image
    Image.new(mode, size, (200, 120, 40) if mode == 'RGB' else (200, 120, 40, 128)).save(path)
    return str(path)


class RedactionTests(unittest.TestCase):
    def test_a_secret_never_stays_in_a_line(self):
        secrets = {
            'export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx && make deploy': 'sk-proj-abcdefghijklmnopqrstuvwx',
            'STRIPE_SECRET="rk_live_51Habcdefgh" bin/rails console': 'rk_live_51Habcdefgh',
            'curl -H "Authorization: Bearer abc.def-ghi_123456" https://api.example.com/v1/me': 'abc.def-ghi_123456',
            'git clone https://sahil:hunter2pass@github.com/antiwork/coconut.git': 'hunter2pass',
            'gh auth login --with-token ghp_abcdefghijklmnopqrstuvwxyz0123456789': 'ghp_abcdefghijklmnopqrstuvwxyz0123456789',
            'mysql --password=tr0ub4dor -u root': 'tr0ub4dor',
            'aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE': 'AKIAIOSFODNN7EXAMPLE',
            'https://example.com/callback?access_token=zzzz9999yyyy&state=1': 'zzzz9999yyyy',
            'echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlX2hlcmU': 'eyJhbGciOiJIUzI1NiJ9',
            'slack send xoxb-123456789012-abcdefghijkl hello': 'xoxb-123456789012-abcdefghijkl',
            '{"password": "correct horse"}': 'correct horse',
            '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----': 'b3BlbnNzaC1rZXk',
        }
        for text, secret in secrets.items():
            with self.subTest(text=text):
                clean = redact(text)
                self.assertNotIn(secret, clean)
                self.assertIn(turn_activity.REDACTED, clean)

    def test_a_line_without_a_secret_stays_as_it_is(self):
        for text in ('git log --author=sahil --oneline -5', 'python -m pytest backend/tests -k tokens',
                     'llm --max-tokens 4096 "summarize"', 'xcodebuild -scheme CoconutMac build',
                     '/Users/sahil/projects/coconut/ios/Coconut/Theme.swift', 'https://github.com/antiwork/coconut/pull/12',
                     'git show 6652b43a1f0c9d8e7b6a5f4e3d2c1b0a99887766'):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)

    def test_an_entry_says_what_the_call_does_in_one_short_line(self):
        entry = describe('terminal', {'command': 'TOKEN=abc123secret ./deploy.sh\nsecond line ' + 'x' * 600})
        self.assertEqual(entry['phase'], 'command')
        self.assertLessEqual(len(entry['detail']), turn_activity.LINE_CHARS)
        self.assertNotIn('abc123secret', entry['detail'])
        self.assertNotIn('\n', entry['detail'])
        self.assertEqual(describe('read_file', {'path': '/Users/x/app/Models.swift'}),
                         {'tool': 'read_file', 'phase': 'reading', 'detail': '/Users/x/app/Models.swift',
                          'path': '/Users/x/app/Models.swift'})
        self.assertEqual(describe('patch', {'path': 'a/b.py'})['phase'], 'editing')
        page = describe('web_extract', {'urls': ['https://example.com/a?api_key=k1234567&page=2']})
        self.assertEqual((page['phase'], page['url']), ('browsing', 'https://example.com/a?api_key=[redacted]&page=2'))
        self.assertEqual(describe('search_files', {'pattern': 'unread', 'path': 'ios'})['detail'], 'unread')
        # A file that the model writes never is a field of its entry.
        self.assertNotIn('content', describe('write_file', {'path': 'a.txt', 'content': 'the whole file'}))
        self.assertNotIn('the whole file', json.dumps(describe('write_file', {'path': 'a.txt', 'content': 'the whole file'})))


class ImageTests(unittest.TestCase):
    def test_only_a_tool_that_shows_images_names_them(self):
        result = json.dumps({'success': True, 'screenshot_path': '/tmp/shots/browser_screenshot_1.png'})
        self.assertEqual(image_sources('browser_vision', {}, result), ['/tmp/shots/browser_screenshot_1.png'])
        self.assertEqual(image_sources('vision_analyze', {'image_url': '/tmp/a.jpg', 'question': 'What?'}, '{}'), ['/tmp/a.jpg'])
        self.assertEqual(image_sources('image_generate', {}, {'success': True, 'image': '/tmp/out.webp'}), ['/tmp/out.webp'])
        self.assertEqual(image_sources('mcp__browser_exec', {}, 'Saved the screenshot to /tmp/cap/page.png\nok'),
                         ['/tmp/cap/page.png'])
        # A page on the web is no file of this Mac, and a command that lists images shows none.
        self.assertEqual(image_sources('vision_analyze', {'image_url': 'https://example.com/a.png'}, '{}'), [])
        self.assertEqual(image_sources('terminal', {'command': 'ls'}, '/tmp/a.png\n/tmp/b.png'), [])
        many = json.dumps({'images': ['/tmp/%d.png' % number for number in range(6)]})
        self.assertEqual(image_sources('image_generate', {}, many), ['/tmp/4.png', '/tmp/5.png'])

    def test_a_copy_is_a_small_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, size, mode in (('wide.png', (2400, 1500), 'RGB'), ('clear.png', (900, 900), 'RGBA'),
                                     ('page.png', (1200, 9000), 'RGB')):
                copy = thumbnail(png(Path(folder) / name, size, mode))
                self.assertTrue(copy.startswith(b'\xff\xd8\xff'), name)
                self.assertLessEqual(len(copy), turn_activity.IMAGE_BYTES)
                from PIL import Image
                with Image.open(BytesIO(copy)) as image:
                    self.assertLessEqual(max(image.size), turn_activity.IMAGE_SIDE)
            text = Path(folder) / 'notes.png'
            text.write_text('not an image')
            self.assertIsNone(thumbnail(str(text)))
            self.assertIsNone(thumbnail(str(Path(folder) / 'missing.png')))


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.feeds = ActivityFeeds(Path(self.temp.name) / 'activity', clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_call_runs_and_then_ends_with_its_time(self):
        self.assertTrue(self.feeds.start(TASK, 'call-1', 'terminal', {'command': 'git status --short'}))
        # A second start of the same call adds nothing.
        self.assertFalse(self.feeds.start(TASK, 'call-1', 'terminal', {'command': 'git status --short'}))
        view = self.feeds.view(TASK)
        self.assertEqual([(e['id'], e['phase'], e['detail'], e['status']) for e in view['entries']],
                         [(1, 'command', 'git status --short', 'running')])
        self.assertIsNone(view['ended_at'])
        self.feeds.finish(TASK, 'call-1', 'terminal', status='ok', duration_ms=2340)
        self.feeds.start(TASK, 'call-2', 'terminal', {'command': 'false'})
        self.feeds.finish(TASK, 'call-2', 'terminal', status='error', duration_ms=12,
                          error='exit 1 with API_KEY=sk-live-abcdefghijklmnopqrstu')
        first, second = self.feeds.view(TASK)['entries']
        self.assertEqual((first['status'], first['seconds']), ('ok', 2.3))
        self.assertEqual(second['status'], 'error')
        self.assertNotIn('sk-live', second['error'])
        # A result without its start, for example of a very fast call, still makes its entry.
        self.feeds.finish(TASK, 'call-3', 'read_file', {'path': '/a/b.py'}, duration_ms=3)
        self.assertEqual(self.feeds.view(TASK)['entries'][-1]['detail'], '/a/b.py')
        self.assertFalse(self.feeds.start(TASK, 'call-3', 'read_file', {'path': '/a/b.py'}))
        # A Hermes without call IDs: a result ends the oldest running call of its tool.
        self.feeds.start(TASK, '', 'patch', {'path': '/a/b.py'})
        self.feeds.finish(TASK, '', 'patch', duration_ms=40)
        self.assertEqual([e['status'] for e in self.feeds.view(TASK)['entries']], ['ok', 'error', 'ok', 'ok'])
        # Each change has a new revision, so the app knows a newer feed.
        self.assertEqual(self.feeds.view(TASK)['revision'], 8)

    def test_the_feed_keeps_only_its_newest_entries_files_and_images(self):
        for number in range(turn_activity.ENTRIES + 25):
            self.feeds.start(TASK, f'read-{number}', 'read_file', {'path': f'/repo/file{number}.py'})
            self.feeds.finish(TASK, f'read-{number}', 'read_file', duration_ms=5)
        view = self.feeds.view(TASK)
        self.assertEqual(len(view['entries']), turn_activity.ENTRIES)
        self.assertEqual(view['entries'][-1]['detail'], f'/repo/file{turn_activity.ENTRIES + 24}.py')
        self.assertEqual(len(view['files']), turn_activity.FILES)
        self.assertEqual(view['files'][-1]['path'], f'/repo/file{turn_activity.ENTRIES + 24}.py')
        folder = Path(self.temp.name) / 'activity' / TASK
        for number in range(turn_activity.IMAGES + 5):
            self.feeds.start(TASK, f'shot-{number}', 'browser_vision', {})
            self.feeds.finish(TASK, f'shot-{number}', 'browser_vision', images=[(f'shot{number}.png', JPEG)])
        view = self.feeds.view(TASK)
        self.assertEqual(len(view['images']), turn_activity.IMAGES)
        self.assertEqual(view['images'][-1]['name'], f'shot{turn_activity.IMAGES + 4}.png')
        # The folder holds the kept images only, and each entry names only images that exist.
        self.assertEqual(len(list(folder.glob('*.jpg'))), turn_activity.IMAGES)
        kept = {image['id'] for image in view['images']}
        self.assertTrue(all(set(entry.get('images', [])) <= kept for entry in view['entries']))
        self.assertEqual(self.feeds.image(TASK, view['images'][0]['id']), JPEG)
        self.assertLess(len(json.dumps(view)), 60_000)

    def test_an_image_must_be_a_small_jpeg_and_a_call_gives_two_at_most(self):
        self.feeds.start(TASK, 'a', 'browser_vision', {})
        self.feeds.finish(TASK, 'a', 'browser_vision', images=[
            ('big.jpg', b'\xff\xd8\xff' + b'0' * turn_activity.IMAGE_BYTES), ('page.png', b'\x89PNG\r\n\x1a\n' + b'0' * 50),
            ('text.jpg', 'no bytes')])
        self.assertEqual(self.feeds.view(TASK)['images'], [])
        self.feeds.start(TASK, 'b', 'browser_vision', {})
        self.feeds.finish(TASK, 'b', 'browser_vision', images=[('1.png', JPEG), ('2.png', JPEG), ('3.png', JPEG)])
        self.assertEqual([image['name'] for image in self.feeds.view(TASK)['images']], ['1.png', '2.png'])
        self.assertIsNone(self.feeds.image(TASK, 'f' * 32))
        # A name of a task is never a path.
        self.assertFalse(self.feeds.start('../../etc', 'c', 'terminal', {'command': 'ls'}))
        self.assertIsNone(self.feeds.view('../../etc'))

    def test_a_file_counts_its_touches_and_an_edit_wins(self):
        for tool in ('read_file', 'read_file', 'patch', 'read_file'):
            self.feeds.start(TASK, str(uuid4()), tool, {'path': '/repo/a.py'})
        self.feeds.start(TASK, 'other', 'read_file', {'path': '/repo/b.py'})
        files = self.feeds.view(TASK)['files']
        self.assertEqual([(row['path'], row['action'], row['count']) for row in files],
                         [('/repo/a.py', 'edit', 4), ('/repo/b.py', 'read', 1)])
        self.assertTrue(self.feeds.touched(TASK, '/repo/a.py'))
        self.assertFalse(self.feeds.touched(TASK, '/repo/c.py'))

    def test_a_feed_stays_some_hours_after_its_turn_and_then_goes(self):
        folder = Path(self.temp.name) / 'activity' / TASK
        self.feeds.start(TASK, 'a', 'browser_vision', {})
        self.feeds.finish(TASK, 'a', 'browser_vision', images=[('1.png', JPEG)])
        self.feeds.start(TASK, 'b', 'terminal', {'command': 'sleep 100'})
        self.assertTrue(self.feeds.end(TASK))
        self.assertFalse(self.feeds.end(TASK))
        view = self.feeds.view(TASK)
        self.assertIsNotNone(view['ended_at'])
        # A call that the end cut off does not run forever.
        self.assertEqual([entry['status'] for entry in view['entries']], ['ok', 'ok'])
        # A late result of the ended turn changes nothing.
        self.assertFalse(self.feeds.finish(TASK, 'b', 'terminal', duration_ms=5))
        self.now += turn_activity.KEPT_SECONDS - 60
        self.assertEqual(self.feeds.sweep(force=True), [])
        self.assertEqual(len(list(folder.glob('*.jpg'))), 1)
        # Another process of the service reads the same feed from its file.
        again = ActivityFeeds(Path(self.temp.name) / 'activity', clock=lambda: self.now)
        self.assertEqual(again.view(TASK), view)
        self.now += 120
        self.assertEqual(self.feeds.sweep(force=True), [TASK])
        self.assertFalse(folder.exists())
        self.assertIsNone(self.feeds.view(TASK))

    def test_a_feed_without_an_end_goes_a_day_after_its_last_change(self):
        other = str(uuid4())
        self.feeds.start(TASK, 'a', 'terminal', {'command': 'ls'})
        self.now += turn_activity.STALE_SECONDS - 60
        self.feeds.start(other, 'a', 'terminal', {'command': 'ls'})
        # A sweep runs at most once in ten minutes, unless it is forced.
        self.assertEqual(self.feeds.sweep(force=True), [])
        self.now += 120
        self.assertEqual(self.feeds.sweep(), [])
        self.assertEqual(self.feeds.sweep(force=True), [TASK])
        self.assertIsNotNone(self.feeds.view(other))
        # A folder that holds no feed goes too.
        broken = Path(self.temp.name) / 'activity' / str(uuid4())
        broken.mkdir()
        (broken / 'feed.json').write_text('{')
        os.utime(broken, (self.now - 3600, self.now - 3600))
        self.now += turn_activity.SWEEP_SECONDS + 1
        self.assertEqual(self.feeds.sweep(), [broken.name])

    def test_a_new_turn_of_a_task_starts_a_new_feed(self):
        self.feeds.start(TASK, 'a', 'browser_vision', {})
        self.feeds.finish(TASK, 'a', 'browser_vision', images=[('1.png', JPEG)])
        self.feeds.end(TASK)
        self.now += 30
        self.feeds.start(TASK, 'b', 'terminal', {'command': 'ls'})
        view = self.feeds.view(TASK)
        self.assertEqual(([entry['detail'] for entry in view['entries']], view['images'], view['ended_at']), (['ls'], [], None))
        self.assertEqual(list((Path(self.temp.name) / 'activity' / TASK).glob('*.jpg')), [])


class ServiceActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.service.gateway_enabled = lambda: True
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.home = self.service.profiles.home(self.bot)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def run_turn(self, script):
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Ship it.'})
        cursor = self.service.updates(0)['cursor']
        adapter = Adapter(self.service.gateway_bridge, self.home, script + [{'kind': 'done', 'outcome': 'success'}])
        adapter.start()
        self.service._run_task(task['id'])
        adapter.join(5)
        return task, cursor, adapter

    def test_the_tool_calls_of_a_turn_reach_the_app_as_its_live_activity(self):
        source = self.repo / 'Models.swift'
        source.write_text('let apiKey = "sk-live-abcdefghijklmnopqrstuvwx"\nstruct Model {}\n')
        (self.repo / '.env').write_text('STRIPE_SECRET=rk_live_1234567890abcdef\n')
        image = base64.b64encode(JPEG).decode()
        task, cursor, adapter = self.run_turn([
            {'kind': 'tool', 'tool': 'read_file', 'call_id': 'c1', 'args': {'path': str(source)}},
            {'kind': 'tool_result', 'tool': 'read_file', 'call_id': 'c1', 'status': 'ok', 'duration_ms': 40},
            {'kind': 'tool', 'tool': 'read_file', 'call_id': 'c2', 'args': {'path': str(self.repo / '.env')}},
            {'kind': 'tool_result', 'tool': 'read_file', 'call_id': 'c2', 'status': 'ok', 'duration_ms': 4},
            {'kind': 'tool', 'tool': 'terminal', 'call_id': 'c3',
             'args': {'command': 'curl -H "Authorization: Bearer abcdef123456" https://api.example.com'}},
            {'kind': 'tool_result', 'tool': 'terminal', 'call_id': 'c3', 'status': 'error', 'duration_ms': 900,
             'error': 'curl: (6) Could not resolve host'},
            {'kind': 'tool', 'tool': 'browser_vision', 'call_id': 'c4', 'args': {}},
            {'kind': 'tool_result', 'tool': 'browser_vision', 'call_id': 'c4', 'status': 'ok', 'duration_ms': 1500,
             'images': [{'name': 'browser_screenshot_1.png', 'jpeg': image}, {'name': 'bad.png', 'jpeg': '%%'}]},
            {'kind': 'send', 'message_id': 'm:1', 'text': 'Done.'}])
        self.assertEqual([answer['ok'] for answer in adapter.answers], [True] * 10)
        view = self.service.task_activity(task['id'])
        self.assertEqual([(e['tool'], e['phase'], e['status']) for e in view['entries']],
                         [('read_file', 'reading', 'ok'), ('read_file', 'reading', 'ok'), ('terminal', 'command', 'error'),
                          ('browser_vision', 'browsing', 'ok')])
        self.assertNotIn('abcdef123456', json.dumps(view))
        self.assertEqual(view['entries'][2]['error'], 'curl: (6) Could not resolve host')
        self.assertEqual([row['path'] for row in view['files']], [str(source), str(self.repo / '.env')])
        # The turn ended, so its feed has an end. The feed stays for the person to read.
        self.assertIsNotNone(view['ended_at'])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')
        # The image of the screenshot, as the app loads it.
        [shot] = view['images']
        self.assertEqual((shot['name'], shot['tool']), ('browser_screenshot_1.png', 'browser_vision'))
        self.assertEqual(self.service.activity_image(task['id'], shot['id']), JPEG)
        with self.assertRaises(ServiceError):
            self.service.activity_image(task['id'], 'f' * 32)
        with self.assertRaises(ServiceError):
            self.service.activity_image(task['id'], '../feed')
        # The updates name the task, and never send the feed itself.
        page = self.service.updates(cursor)
        self.assertEqual(page['activity_changed'], [task['id']])
        self.assertNotIn('reset', page)
        self.assertNotIn('entries', json.dumps(page))
        # A preview of a file of the feed: its text without the secrets in it.
        preview = self.service.activity_file(task['id'], str(source))
        self.assertEqual((preview['name'], preview['truncated']), ('Models.swift', False))
        self.assertIn('struct Model {}', preview['text'])
        self.assertNotIn('sk-live', preview['text'])
        # No preview of a file that holds secrets, of a file that the turn did not touch, or of a file that is gone.
        for path, status in ((str(self.repo / '.env'), 403), (str(self.repo / 'Other.swift'), 404), ('/etc/passwd', 404)):
            with self.assertRaises(ServiceError) as refused:
                self.service.activity_file(task['id'], path)
            self.assertEqual(refused.exception.status, status)
        source.unlink()
        with self.assertRaises(ServiceError):
            self.service.activity_file(task['id'], str(source))
        with self.assertRaises(ServiceError):
            self.service.task_activity(str(uuid4()))

    def test_a_task_without_tool_calls_has_an_empty_feed_and_a_big_file_shows_its_start(self):
        task, _, _ = self.run_turn([{'kind': 'send', 'message_id': 'm:1', 'text': 'Hello.'}])
        view = self.service.task_activity(task['id'])
        self.assertEqual((view['entries'], view['files'], view['images'], view['revision']), ([], [], [], 0))
        big, binary = self.repo / 'big.txt', self.repo / 'data.bin'
        big.write_text('line\n' * 40_000)
        binary.write_bytes(b'\x00\x01\x02' * 100)
        task, _, _ = self.run_turn([
            {'kind': 'tool', 'tool': 'read_file', 'call_id': 'c1', 'args': {'path': str(big)}},
            {'kind': 'tool', 'tool': 'read_file', 'call_id': 'c2', 'args': {'path': str(binary)}}])
        preview = self.service.activity_file(task['id'], str(big))
        self.assertTrue(preview['truncated'])
        self.assertEqual((len(preview['text']), preview['size']), (turn_activity.PREVIEW_BYTES, 200_000))
        with self.assertRaises(ServiceError) as refused:
            self.service.activity_file(task['id'], str(binary))
        self.assertEqual(refused.exception.status, 415)

    def test_the_name_of_a_file_says_whether_it_can_hold_secrets(self):
        for path in ('/repo/.env', '/repo/.env.production', '/Users/x/.ssh/config', '/repo/deploy.pem', '/repo/id_ed25519',
                     '/Users/x/.aws/config', '/repo/credentials.json', '/repo/api_tokens.txt', '/x/gateway/bridge.json'):
            self.assertTrue(secret_file(path), path)
        for path in ('/repo/Models.swift', '/repo/README.md', '/repo/PushTokenStore.swift', '/repo/environment.py',
                     '/repo/package.json'):
            self.assertFalse(secret_file(path), path)


if __name__ == '__main__':
    unittest.main()
