import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service
from coconut import session_maintenance
from test_service import ControlledProvider

SOURCE = os.environ.get('COCONUT_HERMES_ROOT')
PYTHON = os.environ.get('COCONUT_HERMES_PYTHON')


class SessionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = ControlledProvider()
        self.calls = []
        self.provider.maintain_sessions = lambda bot, outcomes, days: self.calls.append((bot, outcomes, days)) or {}
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        home = self.service.root / 'bots' / self.bot['id'] / 'home'
        home.mkdir(parents=True, exist_ok=True)
        (home / 'state.db').write_bytes(b'')

    def task(self, **fields):
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], title='Work', status='completed', summary='',
                    created_at='2026-09-20T10:00:00Z', updated_at='2026-09-20T10:00:00Z', **fields)
        with self.service.transaction():
            self.service._put('tasks', task)
        return task

    def test_idle_outcomes_end_and_every_bot_gets_hermes_maintenance(self):
        idle = self.task(_execution_session_id='idle-work', _conversation_epoch='epoch-1')
        self.task(_execution_session_id='active-work')
        chat = self.task()
        finished = {'idle-work', chat['bot_id']}
        with patch.object(self.service, '_finished_outcomes', return_value=finished):
            self.service.maintain_native_sessions(current=0)
        [(bot, outcomes, days)] = self.calls
        self.assertEqual(bot, dict(id=self.bot['id'], session_id=self.bot['session_id']))
        # The chat has no execution session, so its conversation stays open.
        self.assertEqual(outcomes, [dict(execution_session_id=idle['_execution_session_id'], epoch='epoch-1')])
        self.assertEqual(days, session_maintenance.SESSION_RETENTION_DAYS)

    def test_an_outcome_that_resumes_before_its_bot_keeps_its_session(self):
        self.task(_execution_session_id='idle-work')
        # The second check, just before the bot, finds the outcome active again.
        with patch.object(self.service, '_finished_outcomes', side_effect=[{'idle-work'}, set()]):
            self.service.maintain_native_sessions(current=0)
        [(bot, outcomes, days)] = self.calls
        self.assertEqual(outcomes, [])

    def test_maintenance_runs_at_most_once_an_hour_and_skips_a_busy_service(self):
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            self.service.maintain_native_sessions(current=0)
            self.service.maintain_native_sessions(current=60)
            self.assertEqual(len(self.calls), 1)
            self.service.maintain_native_sessions(current=3601)
            self.assertEqual(len(self.calls), 2)
        with patch.object(self.service, '_finished_outcomes', return_value=None):
            self.service.maintain_native_sessions(current=7300)
        self.assertEqual(len(self.calls), 2)

    def test_a_bot_without_a_session_store_is_skipped(self):
        (self.service.root / 'bots' / self.bot['id'] / 'home' / 'state.db').unlink()
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            self.service.maintain_native_sessions(current=0)
        self.assertEqual(self.calls, [])


@unittest.skipUnless(SOURCE and PYTHON, 'Set COCONUT_HERMES_ROOT and COCONUT_HERMES_PYTHON for the native session check.')
class NativeSessionTests(unittest.TestCase):
    def run_native(self, program):
        with tempfile.TemporaryDirectory() as home:
            backend = Path(__file__).resolve().parents[1]
            env = dict(os.environ, HERMES_HOME=home,
                       PYTHONPATH=os.pathsep.join((str(backend), str(backend / 'coconut'), SOURCE)))
            result = subprocess.run([PYTHON, '-c', textwrap.dedent(program)], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_the_chat_survives_a_prune_of_its_compressed_root(self):
        self.run_native('''
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut.native_conversation import NativeConversation, TIP_KEY
            root = str(uuid4())
            request = dict(operation='start', bot=dict(id=str(uuid4()), session_id=root), task=dict(id='chat'), history=[])
            db = SessionDB()
            conversation = NativeConversation(request, SessionDB())
            assert conversation.session_id == root, conversation.session_id
            db.append_message(root, 'user', 'Use the blue label.')
            # Hermes compression ends the root and continues in a child.
            child = str(uuid4())
            db.create_session(child, source='coconut', parent_session_id=root)
            db.append_message(child, 'user', 'Summary: use the blue label.')
            db.end_session(root, 'compression')
            conversation.close()
            assert db.get_meta(TIP_KEY + root) == child
            # Hermes prunes the ended root. Resolution from the root alone would lose the child.
            assert db.prune_sessions(older_than_days=0) >= 1
            assert db.get_session(root) is None
            again = NativeConversation(request, SessionDB())
            assert again.session_id == child, again.session_id
            assert again.history[-1]['content'] == 'Summary: use the blue label.', again.history
            again.close()
        ''')

    def test_an_idle_outcome_session_ends_reopens_and_returns_after_a_prune(self):
        self.run_native('''
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut import native_sessions
            from coconut.native_conversation import NativeConversation, IDLE_END_REASON, session_root
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            history = [dict(id='m1', task_id='work', role='user', kind='text', text='Fix the upload row.'),
                       dict(id='m2', task_id='work', role='assistant', kind='text', text='I will fix it.')]
            request = dict(operation='resume', bot=bot, task=dict(id='work', _execution_session_id='work'), history=history)
            conversation = NativeConversation(request, SessionDB())
            conversation.close()
            db = SessionDB()
            chat = dict(operation='start', bot=bot, task=dict(id='chat'), history=[])
            NativeConversation(chat, SessionDB()).close()
            result = native_sessions.maintain(dict(bot=bot, outcomes=[dict(execution_session_id='work')], retention_days=7))
            assert result['ended'] == 1, result
            work = db.get_session(session_root(bot, execution='work'))
            assert work['end_reason'] == IDLE_END_REASON, work
            # Coconut never ends the chat.
            assert not db.get_session(bot['session_id'])['ended_at']
            resumed = NativeConversation(request, SessionDB())
            assert not db.get_session(resumed.session_id)['ended_at']
            resumed.close()
            # After Hermes prunes the idle session, a resume seeds the visible conversation again.
            native_sessions.maintain(dict(bot=bot, outcomes=[dict(execution_session_id='work')], retention_days=7))
            assert db.prune_sessions(older_than_days=0) >= 1
            assert db.get_session(session_root(bot, execution='work')) is None
            returned = NativeConversation(request, SessionDB())
            texts = [row['content'] for row in returned.history]
            assert texts == ['Fix the upload row.', 'I will fix it.'], texts
            returned.close()
        ''')


if __name__ == '__main__':
    unittest.main()
