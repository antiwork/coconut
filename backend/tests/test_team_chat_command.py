"""`coconut team connect-chat` and `disconnect-chat` (scripts/team_chat.py, coconut/team_chat_config.py).

ConfigTests checks the edit of the config without Hermes. CommandTests runs scripts/team_chat.py against a team-only
service on a real loopback listener, with the edit of the installed Hermes in a made-up Hermes home. It compares each
file of that home before and after each command. It skips without a Hermes (hermes_env.py).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest import mock

from coconut import team_chat_config
from coconut.server import APIServer
from coconut.team_chat_config import Refused, with_team, without_team
from hermes_env import PYTHON, SOURCE, needs_hermes
from test_team_only import TeamOnlyMacs, make_hermes_home, snapshot

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import team_chat  # noqa: E402

CONFIG = Path('/home/config.yaml')
BRIDGE = '/data/gateway/bridge.json'
TEAM = {'enabled': True, 'extra': {'mode': 'team', 'bridge': BRIDGE}}


def without(config):
    """A disconnect of a config in which each part is proved: the config and the changes."""
    result, changes, problems = without_team(config, CONFIG, BRIDGE)
    assert problems == [], problems
    return result, changes


class ConfigTests(unittest.TestCase):
    def test_a_connect_adds_the_plugin_and_the_platform_and_a_disconnect_takes_them_away(self):
        config = {'model': {'default': 'example-model'}, 'plugins': {'enabled': ['example']},
                  'gateway': {'platforms': {'telegram': {'enabled': True}}}}
        connected, changes = with_team(config, CONFIG, BRIDGE)
        self.assertEqual(connected, {'model': {'default': 'example-model'}, 'plugins': {'enabled': ['example', 'coconut']},
                                     'gateway': {'platforms': {'telegram': {'enabled': True}, 'coconut': TEAM}}})
        self.assertEqual(changes, [f'Add coconut to plugins.enabled in {CONFIG}.',
                                   f'Set gateway.platforms.coconut in {CONFIG}: enabled true, extra.mode team, '
                                   f'extra.bridge {BRIDGE}.'])
        # A second connect changes nothing.
        self.assertEqual(with_team(connected, CONFIG, BRIDGE), (None, []))
        restored, changes = without(connected)
        self.assertEqual(restored, config)
        self.assertEqual(changes, [f'Remove coconut from plugins.enabled in {CONFIG}.',
                                   f'Remove gateway.platforms.coconut from {CONFIG}.'])
        self.assertEqual(without(config), (None, []))

    def test_an_empty_config_gets_the_blocks_and_loses_them_again(self):
        connected, _ = with_team({}, CONFIG, BRIDGE)
        self.assertEqual(connected, {'plugins': {'enabled': ['coconut']}, 'gateway': {'platforms': {'coconut': TEAM}}})
        # The list of the plugins stays: Hermes reads a missing list as "nothing chosen yet".
        self.assertEqual(without(connected)[0], {'plugins': {'enabled': []}})

    def test_the_keys_of_the_operator_in_the_block_stay(self):
        config = {'gateway': {'platforms': {'coconut': {'enabled': True, 'home_channel': 'x',
                                                         'extra': {'mode': 'team', 'bridge': BRIDGE, 'note': 1}}}}}
        restored, changes = without(config)
        self.assertEqual(restored, {'gateway': {'platforms': {'coconut': {'home_channel': 'x', 'extra': {'note': 1}}}}})
        self.assertEqual(changes, [f'Remove enabled, extra.mode and extra.bridge from gateway.platforms.coconut in '
                                   f'{CONFIG}.'])

    def test_the_keys_of_the_operator_left_by_a_disconnect_do_not_block_a_reconnect(self):
        connected, _ = with_team({}, CONFIG, BRIDGE)
        block = connected['gateway']['platforms']['coconut']
        block['home_channel'] = 'x'
        block['extra']['note'] = 1
        left, _ = without(connected)
        self.assertEqual(left['gateway']['platforms']['coconut'], {'home_channel': 'x', 'extra': {'note': 1}})
        # A disconnect of a block without the mode changes nothing more.
        self.assertEqual(without(left), (None, []))
        again, changes = with_team(left, CONFIG, BRIDGE)
        self.assertEqual(again['gateway']['platforms']['coconut'],
                         {'home_channel': 'x', 'enabled': True, 'extra': {'note': 1, 'mode': 'team', 'bridge': BRIDGE}})
        self.assertEqual(len(changes), 2)
        self.assertEqual(without(again)[0], left)

    def test_a_block_of_a_full_coconut_and_a_form_that_hermes_does_not_read_are_refused(self):
        full = {'gateway': {'platforms': {'coconut': {'enabled': True}}}}
        unread = ({'gateway': {'platforms': {'coconut': {'enabled': False, 'home_channel': 'x'}}}},
                  {'gateway': {'platforms': {'coconut': 'on'}}}, {'gateway': {'platforms': {'coconut': {'extra': 'team'}}}},
                  {'plugins': ['coconut']}, {'plugins': {'enabled': 'coconut'}}, {'gateway': []},
                  {'gateway': {'platforms': ['coconut']}})
        for config in (full, {'gateway': {'platforms': {'coconut': {'enabled': True, 'extra': {'bridge': '/x'}}}}},
                       *unread):
            with self.subTest(config=config), self.assertRaises(Refused):
                with_team(config, CONFIG, BRIDGE)
        # A disconnect proves that a block of a full Coconut has nothing of the team chat, and leaves it.
        self.assertEqual(without(full), (None, []))

    def test_a_disconnect_proves_each_part_by_itself_and_names_each_part_that_does_not_read(self):
        team = {'enabled': True, 'extra': {'mode': 'team', 'bridge': BRIDGE}}
        # The case of the review: a list that is a text, beside a block of the team chat.
        result, changes, problems = without_team({'plugins': {'enabled': 'coconut'},
                                                  'gateway': {'platforms': {'coconut': team}}}, CONFIG, BRIDGE)
        self.assertEqual(result, {'plugins': {'enabled': 'coconut'}})
        self.assertEqual(changes, [f'Remove gateway.platforms.coconut from {CONFIG}.'])
        self.assertEqual(problems, [f'plugins.enabled in {CONFIG} is not a list. Correct it, or remove coconut from it.'])
        # The mirror: a platforms section that is a list, beside a list with coconut.
        result, changes, problems = without_team({'plugins': {'enabled': ['coconut']},
                                                  'gateway': {'platforms': ['coconut']}}, CONFIG, BRIDGE)
        self.assertEqual(result, {'plugins': {'enabled': []}, 'gateway': {'platforms': ['coconut']}})
        self.assertEqual(problems, [f'gateway.platforms in {CONFIG} is not a mapping. Correct it.'])
        self.assertEqual(without_team(['a'], CONFIG, BRIDGE)[2], [f'{CONFIG} is not a mapping. Correct it.'])
        # A block with only the bridge of this Coconut is a part of the team chat too.
        self.assertEqual(without({'gateway': {'platforms': {'coconut': {'extra': {'bridge': BRIDGE}}}}})[0], {})


class HalfStateTests(TeamOnlyMacs, unittest.TestCase):
    """A step of connect-chat or disconnect-chat that fails leaves a record that blocks the removal, and a second run
    finishes the job. The edit of the Hermes home is a stand-in here."""

    def setUp(self):
        super().setUp()
        self.bot = self.declare()
        self.server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
        self.edits = []

    def edit(self, fail=False):
        def run(action, home, bridge):
            self.edits.append(action)
            if fail:
                raise team_chat.Stop('Coconut found no Hermes that it can use, so it changed nothing.')
            return [], []
        return mock.patch.object(team_chat, 'edit', side_effect=run)

    def flags(self):
        bot = self.call('GET', '/v1/team/bots')['bots'][0]
        return bot['chat'], bot['plugin']

    def command(self, *arguments) -> int:
        with mock.patch.object(sys, 'stdout', Writer([])), mock.patch.object(sys, 'stderr', Writer([])):
            return team_chat.main([*arguments, '--data-dir', str(self.only.root)])

    def test_a_failed_connect_blocks_the_removal_and_a_second_run_finishes_it(self):
        # The plan works, and the write fails.
        def run(action, home, bridge):
            if action == 'connect':
                raise team_chat.Stop('Coconut could not read or write the config.')
            return [], []
        with mock.patch.object(team_chat, 'edit', side_effect=run):
            self.assertEqual(self.command('connect', 'Reporter', '--confirm'), 1)
        # The home may have a part of the change, so the bot cannot go, and it does not answer.
        self.assertEqual(self.flags(), (False, True))
        self.assertEqual(self.refusal(self.call, 'DELETE', f'/v1/team/bots/{self.bot["id"]}')[0], 409)
        with self.edit():
            self.assertEqual(self.command('connect', 'Reporter', '--confirm'), 0)
        self.assertEqual(self.flags(), (True, True))

    def test_a_guard_on_a_home_without_anything_of_coconut_goes_with_disconnect_chat(self):
        for case in ('empty', 'foreign', 'deleted'):
            with self.subTest(case=case):
                self.call('PATCH', f'/v1/team/bots/{self.bot["id"]}', {'plugin': True})
                if case == 'foreign':
                    (self.home / 'plugins/coconut').mkdir(parents=True)
                elif case == 'deleted':
                    shutil.rmtree(self.home)
                # No Hermes is needed: the home cannot have a change of Coconut.
                with self.edit(fail=True):
                    self.assertEqual(self.command('disconnect', 'Reporter'), 0)
                self.assertEqual((self.flags(), self.edits), ((False, False), []))
        self.assertTrue(self.call('DELETE', f'/v1/team/bots/{self.bot["id"]}')['removed'])

    def test_a_failed_disconnect_blocks_the_removal_and_a_second_run_finishes_it(self):
        with self.edit():
            self.command('connect', 'Reporter', '--confirm')
        # The stand-in edit wrote nothing, so the home gets an entry of Coconut by hand.
        (self.home / 'config.yaml').write_text('plugins:\n  enabled: [coconut]\n')
        with self.edit(fail=True):
            self.assertEqual(self.command('disconnect', 'Reporter'), 1)
        # The bot answers no more, but its home may still have the plugin.
        self.assertEqual(self.flags(), (False, True))
        self.assertEqual(self.refusal(self.call, 'DELETE', f'/v1/team/bots/{self.bot["id"]}')[0], 409)
        with self.edit():
            self.assertEqual(self.command('disconnect', 'Reporter'), 0)
            self.assertEqual(self.command('disconnect', 'Reporter'), 0)
        self.assertEqual(self.flags(), (False, False))
        self.assertEqual(self.edits, ['plan', 'connect', 'disconnect', 'disconnect', 'disconnect'])
        self.assertEqual(self.call('DELETE', f'/v1/team/bots/{self.bot["id"]}')['removed'], True)


class Output:
    """The standard output and error of one run of scripts/team_chat.py."""

    def __init__(self, status, out, err):
        self.status, self.out, self.err = status, out, err


@needs_hermes
class CommandTests(TeamOnlyMacs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bot = self.declare()
        self.server = APIServer(('127.0.0.1', 0), self.only)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.only.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_address[1]}')
        self.bridge = (self.only.root / 'gateway/bridge.json').resolve()
        # A home in which Hermes ran, as the home of a production agent: Hermes makes its folders at each start.
        self.hermes('import hermes_cli.config')
        self.config = self.plugin_config()
        self.config_text = (self.home / 'config.yaml').read_text()

    def hermes(self, code: str, *arguments) -> str:
        """Run code under the Python of the installed Hermes, with the made-up home."""
        environment = {'HERMES_HOME': str(self.home), 'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'],
                       'HERMES_DISABLE_LAZY_INSTALLS': '1', 'HERMES_DISABLE_TELEMETRY': '1'}
        result = subprocess.run([PYTHON, '-I', '-B', '-c', f'import sys; sys.path.insert(0, {SOURCE!r}); {code}',
                                 *arguments], env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stdout

    def run_command(self, *arguments) -> Output:
        environment = {'COCONUT_HERMES_ROOT': SOURCE, 'COCONUT_HERMES_PYTHON': PYTHON, 'PATH': os.environ['PATH'],
                       'HOME': os.environ['HOME']}
        out, err = [], []
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(sys, 'stdout', Writer(out)), mock.patch.object(sys, 'stderr', Writer(err)):
            status = team_chat.main([*arguments, '--data-dir', str(self.only.root)])
        return Output(status, ''.join(out), ''.join(err))

    def record(self):
        return self.call('GET', '/v1/team/bots')['bots'][0]

    def plugin_config(self) -> dict:
        return json.loads(self.hermes('import json; from ruamel.yaml import YAML; '
                                      'print(json.dumps(YAML(typ="safe").load(open(sys.argv[1]))))',
                                      str(self.home / 'config.yaml')))

    def files(self) -> dict:
        """Each path of the home with its mode, its bytes and the target of a link. Hermes sets the mode of its
        folders again at each start, which changes only their times, so the times stay out."""
        return {path: (row[0], row[5], os.readlink(self.home / path) if (self.home / path).is_symlink() else None)
                for path, row in snapshot(self.home).items() if path != '.'}

    def changed(self, before: dict, after: dict) -> set[str]:
        """The changed paths, without the copies of config.yaml that Hermes keeps in backups/config at each start
        and before each write of its config."""
        return {path for path in set(before) | set(after) if before.get(path) != after.get(path)
                and not path.startswith('backups/config/')}

    def test_the_plan_changes_nothing_and_the_confirm_makes_exactly_the_listed_changes(self):
        before = snapshot(self.home)
        plan = self.run_command('connect', 'Reporter')
        self.assertEqual(plan.status, 0, plan.err)
        home, plugin = os.path.realpath(self.home), team_chat_config.PLUGIN
        self.assertEqual(plan.out.splitlines(), [
            f'This command changes nothing yet. With --confirm, it makes these changes in the Hermes home {home}:',
            f'1. Link {home}/plugins/coconut to {plugin}.',
            f'2. Add coconut to plugins.enabled in {home}/config.yaml.',
            f'3. Set gateway.platforms.coconut in {home}/config.yaml: enabled true, extra.mode team, extra.bridge '
            f'{self.bridge}.',
            'Then Reporter answers each member of its teams who names it in the team chat. It uses the tools that the '
            f'Hermes config gives to the channel coconut (platform_toolsets.coconut in {home}/config.yaml).',
            'Coconut does not restart the Hermes gateway. You restart it after the change.',
            'To make the change, run: coconut team connect-chat Reporter --confirm'])
        now = snapshot(self.home)
        self.assertEqual(sorted(path for path in set(before) | set(now) if before.get(path) != now.get(path)), [])
        self.assertFalse(self.record()['chat'])

        before = self.files()
        done = self.run_command('connect', 'reporter', '--confirm')
        self.assertEqual(done.status, 0, done.err)
        self.assertEqual(done.out.splitlines()[1:4], plan.out.splitlines()[1:4])
        self.assertIn('Next step: restart the Hermes gateway of', done.out)
        # config.yaml changes, and the link is new. Nothing else changes.
        self.assertEqual(self.changed(before, self.files()), {'config.yaml', 'plugins/coconut'})
        self.assertEqual(os.readlink(self.home / 'plugins/coconut'), str(plugin))
        old = self.config
        self.assertEqual(self.plugin_config(), dict(old, plugins={'enabled': ['coconut']},
                                                    gateway={'platforms': {'coconut': {
                                                        'enabled': True,
                                                        'extra': {'mode': 'team', 'bridge': str(self.bridge)}}}}))
        self.assertTrue(self.record()['chat'])
        # A removal of the bot waits for disconnect-chat.
        self.assertEqual(self.refusal(self.call, 'DELETE', f'/v1/team/bots/{self.bot["id"]}')[0], 409)

        gone = self.run_command('disconnect', 'Reporter')
        self.assertEqual(gone.status, 0, gone.err)
        self.assertEqual(gone.out.splitlines()[1:4], [
            f'1. Remove the link {home}/plugins/coconut.',
            f'2. Remove coconut from plugins.enabled in {home}/config.yaml.',
            f'3. Remove gateway.platforms.coconut from {home}/config.yaml.'])
        self.assertFalse(self.record()['chat'])
        self.assertFalse((self.home / 'plugins/coconut').is_symlink())
        # The list of the plugins stays, also when it is empty: Hermes reads a missing list as "nothing chosen yet".
        self.assertEqual(self.plugin_config(), dict(old, plugins={'enabled': []}))
        self.assertEqual(self.changed(before, self.files()), {'config.yaml'})

    def test_the_layout_of_a_production_config_stays_outside_the_three_changes(self):
        start = ('# The agent of the team.\n'
                 'platform_toolsets: {telegram: [web, file], api_server: [web]}  # flow, as the operator wrote it\n'
                 'model:\n'
                 '  provider: custom   # an inline comment\n'
                 '  default: example-model\n'
                 'plugins:\n'
                 '  enabled: [disk-cleanup]\n'
                 '\n'
                 '# The end.\n'
                 'agent: {max_turns: 40}\n')
        (self.home / 'config.yaml').write_text(start)
        self.assertEqual(self.run_command('connect', 'Reporter', '--confirm').status, 0)
        after = (self.home / 'config.yaml').read_text()
        # Each line of the start stays, except the list that gets coconut, and the new lines are only the platform.
        self.assertIn('  enabled: [disk-cleanup, coconut]\n', after)
        for line in start.splitlines():
            if 'disk-cleanup' not in line:
                self.assertIn(line + '\n', after)
        added = [line for line in after.splitlines() if line not in start.splitlines()]
        self.assertEqual(added[0], '  enabled: [disk-cleanup, coconut]')
        self.assertEqual([line.split(':')[0].strip() for line in added[1:]],
                         ['gateway', 'platforms', 'coconut', 'enabled', 'extra', 'mode', 'bridge'])
        self.assertEqual(self.run_command('disconnect', 'Reporter').status, 0)
        self.assertEqual((self.home / 'config.yaml').read_text(), start)

    def test_a_reconnect_keeps_the_keys_of_the_operator_and_ends_where_the_first_disconnect_ended(self):
        self.assertEqual(self.run_command('connect', 'Reporter', '--confirm').status, 0)
        # The operator adds a key to the block and one to `extra`, with the round-trip YAML of Hermes.
        self.hermes('from ruamel.yaml import YAML; yaml = YAML(); path = sys.argv[1]; data = yaml.load(open(path)); '
                    'block = data["gateway"]["platforms"]["coconut"]; block["home_channel"] = "ops"; '
                    'block["extra"]["note"] = "kept"; yaml.dump(data, open(path, "w"))', str(self.home / 'config.yaml'))
        for step in ('disconnect', 'connect', 'disconnect'):
            with self.subTest(step=step):
                result = self.run_command(step, 'Reporter', *(['--confirm'] if step == 'connect' else []))
                self.assertEqual(result.status, 0, result.err)
                if step == 'disconnect' and not hasattr(self, 'first_disconnect'):
                    self.first_disconnect = (self.home / 'config.yaml').read_bytes()
        block = self.plugin_config()['gateway']['platforms']['coconut']
        self.assertEqual(block, {'home_channel': 'ops', 'extra': {'note': 'kept'}})
        self.assertEqual((self.home / 'config.yaml').read_bytes(), self.first_disconnect)

    def test_a_home_that_is_not_coconuts_to_change_leaves_both_flags_and_the_bot_can_go(self):
        config = (self.home / 'config.yaml').read_text()
        for case in ('foreign plugin', 'full coconut', 'no config'):
            with self.subTest(case=case):
                if case == 'foreign plugin':
                    (self.home / 'plugins/coconut').mkdir()
                elif case == 'full coconut':
                    (self.home / 'config.yaml').write_text(config + 'gateway:\n  platforms:\n    coconut:\n'
                                                           '      enabled: true\n')
                else:
                    (self.home / 'config.yaml').unlink()
                before = snapshot(self.home)
                result = self.run_command('connect', 'Reporter', '--confirm')
                self.assertEqual(result.status, 1, result.out)
                self.assertIn('Coconut changed nothing' if case != 'full coconut' else 'Coconut did not change it',
                              result.err)
                self.assertEqual(snapshot(self.home), before)
                self.assertEqual((self.record()['chat'], self.record()['plugin']), (False, False))
                if case == 'foreign plugin':
                    shutil.rmtree(self.home / 'plugins/coconut')
                (self.home / 'config.yaml').write_text(config)
        self.assertTrue(self.call('DELETE', f'/v1/team/bots/{self.bot["id"]}')['removed'])

    def test_a_part_that_does_not_read_keeps_the_guard_until_a_second_run_after_the_correction(self):
        def write(text):
            (self.home / 'config.yaml').chmod(0o600)
            (self.home / 'config.yaml').write_text(text)
        cases = {
            # The case of the review: plugins.enabled is a text, and the block of the team chat is there.
            'enabled text': ('plugins:\n  enabled: coconut\n', 'plugins.enabled in',
                             'plugins:\n  enabled: []\n'),
            'platforms list': ('gateway:\n  platforms: [coconut]\n', 'gateway.platforms in', 'gateway: {}\n'),
            'no parse': ('plugins: [unclosed\n', 'does not read', 'model: {}\n'),
            'no read': (None, 'does not read', 'model: {}\n'),
        }
        for case, (text, named, corrected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.run_command('connect', 'Reporter', '--confirm').status, 0)
                if case == 'enabled text':
                    # The block of the team chat is there, and only the list has the wrong form.
                    write(text + f'gateway:\n  platforms:\n    coconut:\n      enabled: true\n      extra:\n'
                                 f'        mode: team\n        bridge: {self.bridge}\n')
                elif text is None:
                    (self.home / 'config.yaml').chmod(0)
                else:
                    write(text)
                result = self.run_command('disconnect', 'Reporter')
                self.assertEqual(result.status, 1, result.out)
                self.assertIn(named, result.err)
                self.assertIn('Then run again: coconut team disconnect-chat Reporter', result.err)
                self.assertEqual((self.record()['chat'], self.record()['plugin']), (False, True))
                self.assertEqual(self.refusal(self.call, 'DELETE', f'/v1/team/bots/{self.bot["id"]}')[0], 409)
                # The link of Coconut is gone already: the command removes what it can prove.
                self.assertFalse((self.home / 'plugins/coconut').is_symlink())
                if case == 'enabled text':
                    self.assertNotIn('mode: team', (self.home / 'config.yaml').read_text())
                write(corrected)
                again = self.run_command('disconnect', 'Reporter')
                self.assertEqual(again.status, 0, again.err)
                self.assertEqual((self.record()['chat'], self.record()['plugin']), (False, False))
                write(self.config_text)

    def test_a_mac_with_a_person_and_a_home_that_is_not_coconuts_are_refused(self):
        with mock.patch.object(team_chat.host, 'backend_module') as module:
            module.return_value.team_folder.return_value = False
            refused = self.run_command('connect', 'Reporter', '--confirm')
        self.assertEqual((refused.status, refused.err.strip()), (1, team_chat.FULL_MAC))
        before = snapshot(self.home)
        (self.home / 'plugins/coconut').mkdir()
        before = snapshot(self.home)
        taken = self.run_command('connect', 'Reporter', '--confirm')
        self.assertEqual(taken.status, 1)
        self.assertIn('is another plugin, not Coconut', taken.err)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.record()['chat'])
        shutil.rmtree(self.home / 'plugins/coconut')
        missing = self.run_command('connect', 'Nobody')
        self.assertEqual(missing.status, 1)
        self.assertIn('This Mac has no bot with the name or ID "Nobody"', missing.err)


class Writer:
    def __init__(self, parts):
        self.parts = parts

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass


if __name__ == '__main__':
    unittest.main()
