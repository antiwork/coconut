"""How the backend calls the Swift helper of the team log (coconut/team_helper.py), with a stub helper.

scripts/test_team_log.py runs the real helper. This file checks the pipe protocol: one JSON object on standard
input, no argument, one JSON object back, the error codes, a helper that fails, and where the backend finds the helper.
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from coconut import team_helper
from coconut.team_helper import HELPER_ENVIRONMENT, TeamFormatError, TeamHelper, TeamHelperError

# The stub writes its arguments and its input next to itself, and answers from the `answer` file there.
STUB = '''#!{python}
import json, pathlib, sys
folder = pathlib.Path(__file__).parent
received = sys.stdin.read()
with (folder / 'calls.jsonl').open('a') as calls:
    calls.write(json.dumps({{'argv': sys.argv[1:], 'input': json.loads(received)}}) + '\\n')
answer = json.loads((folder / 'answer.json').read_text())
if answer.get('exit'):
    sys.exit(answer['exit'])
if answer.get('echo_open'):
    entries = json.loads(received)['entries']
    answer = {{'entries': [{{'epoch': 0, 'payload': entry['body']}} for entry in entries]}}
sys.stdout.write(answer['raw'] if 'raw' in answer else json.dumps(answer))
'''


class TeamHelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.stub = self.folder / 'stub' / 'team-helper'
        self.stub.parent.mkdir()
        self.stub.write_text(STUB.format(python=sys.executable))
        self.stub.chmod(0o700)
        self.data = self.folder / 'data'
        self.data.mkdir()
        patcher = mock.patch.dict(os.environ, {HELPER_ENVIRONMENT: str(self.stub)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.helper = TeamHelper(self.data)

    def answer(self, value: dict) -> None:
        (self.stub.parent / 'answer.json').write_text(json.dumps(value))

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in (self.stub.parent / 'calls.jsonl').read_text().splitlines()]

    def test_a_call_sends_one_object_on_standard_input_and_no_argument(self):
        self.answer({'proof': 'cHJvb2Y=', 'hash': 'a' * 64})
        self.assertEqual(self.helper.proof('b' * 64, 'c' * 64, 'a2V5'), {'proof': 'cHJvb2Y=', 'hash': 'a' * 64})
        self.assertEqual(self.calls(), [{'argv': [], 'input': {'operation': 'proof', 'room': 'b' * 64, 'token': 'c' * 64,
                                                               'member_key': 'a2V5'}}])
        self.assertTrue(self.helper.available)

    def test_a_seal_sends_the_payload_as_base64(self):
        self.answer({'body': 'Ym9keQ=='})
        self.assertEqual(self.helper.seal('b' * 64, 'e' * 32, 0, 'a2V5', 'bWVtYmVy', b'{"kind":"member"}'), 'Ym9keQ==')
        self.assertEqual(base64.b64decode(self.calls()[0]['input']['payload']), b'{"kind":"member"}')

    def test_an_error_code_of_the_helper_is_a_format_error_with_that_code(self):
        for code in ('malformed', 'unknown_version', 'unknown_epoch', 'seal', 'signature'):
            with self.subTest(code=code):
                self.answer({'error': code})
                with self.assertRaises(TeamFormatError) as raised:
                    self.helper.invite('coconut://team/secret-link')
                self.assertEqual((raised.exception.code, str(raised.exception)), (code, code))

    def test_a_helper_that_fails_gives_an_error_without_a_value_of_the_call(self):
        for answer in ({'exit': 1}, {'raw': 'not json'}, {'raw': '[1, 2]'}, {'raw': ''}):
            with self.subTest(answer=answer):
                self.answer(answer)
                with self.assertRaises(TeamHelperError) as raised:
                    self.helper.open_welcome('b' * 64, 'c2VjcmV0', 'd2VsY29tZQ==')
                self.assertEqual(str(raised.exception), 'The team helper did not finish its work.')

    def test_a_helper_that_does_not_finish_in_time_is_stopped(self):
        self.stub.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(10)\n')
        with mock.patch.object(team_helper, 'TIMEOUT_SECONDS', .5), self.assertRaises(TeamHelperError):
            self.helper.member()

    def test_open_gives_one_result_for_each_entry_in_batches_of_one_answer(self):
        self.answer({'echo_open': True})
        entries = [{'id': f'{number:032x}', 'member': 'f' * 64, 'body': base64.b64encode(b'%d' % number).decode(),
                    'seq': number + 1} for number in range(300)]
        results = self.helper.open('b' * 64, {'0': 'a2V5'}, entries)
        self.assertEqual(results[:2], [{'epoch': 0, 'payload': b'0'}, {'epoch': 0, 'payload': b'1'}])
        self.assertEqual(len(results), 300)
        # One call opens at most one answer of the relay, and it sends only the fields that the helper needs.
        calls = self.calls()
        self.assertEqual([len(call['input']['entries']) for call in calls], [256, 44])
        self.assertEqual(set(calls[0]['input']['entries'][0]), {'id', 'member', 'body'})

    def test_open_refuses_an_answer_that_does_not_fit_the_entries(self):
        for answer in ({'entries': []}, {'entries': [{'epoch': 0, 'payload': '***'}]}, {'entries': [{'epoch': 0}]},
                       {'entries': 'none'}):
            with self.subTest(answer=answer):
                self.answer(answer)
                with self.assertRaises(TeamHelperError):
                    self.helper.open('b' * 64, {}, [{'id': '0' * 32, 'member': 'f' * 64, 'body': 'Ym9keQ=='}])
        # A failure of one entry is a result, not an error.
        self.answer({'entries': [{'error': 'seal'}]})
        self.assertEqual(self.helper.open('b' * 64, {}, [{'id': '0' * 32, 'member': 'f' * 64, 'body': 'Ym9keQ=='}]),
                         [{'error': 'seal'}])

    def test_the_helper_of_the_app_comes_first_then_a_private_helper_in_the_data_folder(self):
        own = self.data / 'team' / 'team-helper'
        own.parent.mkdir(mode=0o700)
        own.write_text('')
        own.chmod(0o700)
        self.assertEqual(team_helper.helper_path(self.data), self.stub)
        for value in ('', str(self.folder / 'missing'), str(self.stub.parent)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {HELPER_ENVIRONMENT: value}):
                self.assertEqual(team_helper.helper_path(self.data), own)
        self.stub.chmod(0o600)
        self.assertEqual(team_helper.helper_path(self.data), own)

    def test_a_helper_in_the_data_folder_must_be_private_and_no_link(self):
        own = self.data / 'team' / 'team-helper'
        own.parent.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {HELPER_ENVIRONMENT: ''}):
            self.assertIsNone(team_helper.helper_path(self.data))
            self.assertFalse(TeamHelper(self.data).available)
            with self.assertRaises(TeamHelperError) as raised:
                TeamHelper(self.data).member()
            self.assertIn('scripts/team.py build', str(raised.exception))
            own.symlink_to(self.stub)
            self.assertIsNone(team_helper.helper_path(self.data))
            own.unlink()
            own.write_text('')
            for mode in (0o750, 0o705, 0o720):
                own.chmod(mode)
                self.assertIsNone(team_helper.helper_path(self.data), oct(mode))
            own.chmod(0o700)
            self.assertEqual(team_helper.helper_path(self.data), own)


if __name__ == '__main__':
    unittest.main()
