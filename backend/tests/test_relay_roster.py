"""Run with COCONUT_HERMES_ROOT. One turn must not remove the contacts that another turn of the same bot uses."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from hermes_env import needs_hermes, SOURCE


@needs_hermes
class RelayRosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.current, self.peer = {'id': str(uuid4()), 'name': 'Desk'}, {'id': str(uuid4()), 'name': 'Dev', 'role': 'Code'}
        patches = (patch.dict(os.environ, HERMES_HOME=str(self.home)),
                   patch.object(sys, 'path', [str(Path(__file__).parents[1]), str(Path(__file__).parents[1] / 'coconut'), SOURCE, *sys.path]))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def relay(self, operation, peers):
        from coconut.native_relay import NativeRelay
        request = dict(operation=operation, bot=self.current, task=dict(id=str(uuid4()), _available_bots=peers))
        return NativeRelay(request, lambda kind, payload: {})

    def roster_ids(self):
        from tools.bot_relay import read_remote_roster
        return [row['connection_id'] for row in read_remote_roster(self.home)]

    def test_a_context_turn_leaves_the_roster_of_the_work_turns_alone(self):
        self.relay('start', [self.peer]).publish_roster()
        self.assertEqual(self.roster_ids(), [self.peer['id']])
        # A decision check has no contact list. It used to write an empty roster for the whole profile.
        self.relay('context', []).publish_roster()
        self.assertEqual(self.roster_ids(), [self.peer['id']])

    def test_a_work_turn_publishes_its_contacts(self):
        self.relay('start', [self.peer]).publish_roster()
        self.assertEqual(self.roster_ids(), [self.peer['id']])
        self.relay('resume', []).publish_roster()
        self.assertEqual(self.roster_ids(), [])

    def test_a_turn_sends_to_its_own_contacts_when_the_shared_roster_changes(self):
        turn = self.relay('start', [self.peer])
        turn.publish_roster()
        # Another worker of the same bot rewrites the shared file with no contacts.
        from tools.bot_relay import write_remote_roster
        write_remote_roster(self.home, [])
        result = json.loads(turn.enqueue(dict(target=self.peer['id'], message='Publish the approved draft.')))
        self.assertEqual(result, {'status': 'queued'})

    def test_a_target_outside_the_turn_contacts_is_still_refused(self):
        turn = self.relay('start', [self.peer])
        turn.publish_roster()
        result = json.loads(turn.enqueue(dict(target=str(uuid4()), message='Hello.')))
        self.assertIn('current contact list', result['error'])


if __name__ == '__main__':
    unittest.main()
