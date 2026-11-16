"""A cron delivery of a real Hermes into a named chat of a team, from a process without a gateway.

The team-only Mac has the bot Reporter, its Hermes home has the edit of `coconut team connect-chat --confirm`, and the
team-only service serves its routes on loopback. No gateway runs. A Hermes process resolves the target
`coconut:<bot>:team:<room>:<chat>` as cron does (resolve_send_target) and sends with `_send_to_platform`, which
calls `_send_via_adapter`. Without a live adapter, Hermes calls the sender of the plugin (team.send_standalone). The
text shows in the named chat as a message of the bot. It skips without a Hermes (hermes_env.py).
"""
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

from coconut import team_turns
from coconut.server import APIServer
from hermes_env import PYTHON, SOURCE, needs_hermes
from test_team_only import TeamOnlyMacs
from test_team_turns import AsThePerson

ROOT = Path(__file__).resolve().parents[2]
TEXT = 'The weekly report is ready.'
# The delivery of a cron job: the target of `deliver`, as cron splits it, and the send of the standalone lane.
DELIVER = '''
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
from gateway.config import Platform, load_gateway_config
from tools.send_message_tool import _send_to_platform, prepare_send_message_platforms, resolve_send_target
platform_name, rest = sys.argv[2].split(':', 1)
prepare_send_message_platforms()
chat_id, thread_id, error = resolve_send_target(platform_name, rest, pass_unresolved_references=True)
pconfig = load_gateway_config().platforms.get(Platform(platform_name))
result = error or asyncio.run(_send_to_platform(Platform(platform_name), pconfig, chat_id, sys.argv[3],
                                                thread_id=thread_id))
print(json.dumps({'chat_id': chat_id, 'thread_id': thread_id, 'result': result}))
'''


@needs_hermes
class NativeTeamCronDeliveryTests(AsThePerson, TeamOnlyMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # A Hermes home of its own, in the scratch folder of the test.
        self.hermes_home = Path(self.temp.name) / 'agent'
        self.hermes_home.mkdir()
        (self.hermes_home / 'config.yaml').write_text('{}\n')
        self.environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LANG') if key in os.environ}
        self.environment.update(HERMES_HOME=str(self.hermes_home), HERMES_DISABLE_LAZY_INSTALLS='1',
                                HERMES_DISABLE_TELEMETRY='1')
        self.room = self.team()
        self.bot = self.declare(home=self.hermes_home)
        self.share(self.room, self.bot)
        server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{server.server_address[1]}')
        edit = subprocess.run([PYTHON, '-I', '-B', str(ROOT / 'backend/coconut/team_chat_config.py'), 'connect',
                               SOURCE, str(self.hermes_home), str(self.only.root / 'gateway/bridge.json')],
                              env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(edit.returncode, 0, edit.stdout + edit.stderr)
        self.call('PATCH', f'/v1/team/bots/{self.bot["id"]}', {'chat': True})
        self.sync()

    def test_a_cron_delivery_without_a_gateway_reaches_a_named_chat(self):
        design = self.call('POST', f'/v1/team/rooms/{self.room}/chats', {'name': 'Design'}, service=self.first)['chat']
        self.sync()
        chat = team_turns.chat_id(self.room, self.bot['id'], design)
        run = subprocess.run([PYTHON, '-I', '-B', '-c', DELIVER, SOURCE, 'coconut:' + chat, TEXT],
                             env=self.environment, capture_output=True, text=True, timeout=120, cwd=self.temp.name)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        answer = json.loads(run.stdout.strip().splitlines()[-1])
        # Hermes gives the whole reference to the plugin as the chat ID.
        self.assertEqual((answer['chat_id'], answer['thread_id']), (chat, None))
        result = answer['result']
        self.assertEqual(result.get('error'), None, result)
        self.assertTrue(result['success'])
        self.assertTrue(result['message_id'].startswith('outside:'))
        self.sync()
        rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', {'chat': design},
                         service=self.first)['messages']
        self.assertEqual([(row['bot_name'], row['text']) for row in rows], [('Reporter', TEXT)])
        # The log of the delivery has no text of the message.
        self.assertNotIn(TEXT, run.stderr)


if __name__ == '__main__':
    unittest.main()
