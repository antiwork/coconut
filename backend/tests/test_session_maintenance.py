from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import threading
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
        self.calls, self.vacuums = [], []
        self.provider.maintain_sessions = lambda bot, conversations, outcomes, days, allow_vacuum=False: self.calls.append((bot, conversations, outcomes, days)) or self.vacuums.append(allow_vacuum) or {}
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        home = self.service.root / 'bots' / self.bot['id'] / 'home'
        home.mkdir(parents=True, exist_ok=True)
        (home / 'state.db').write_bytes(b'')

    def task(self, **fields):
        task = dict(id=str(uuid4()), bot_id=self.bot['id'], title='Work', status='completed', summary='',
                    created_at='2026-09-20T10:00:00Z', updated_at='2026-09-20T10:00:00Z') | fields
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
        [(bot, conversations, outcomes, days)] = self.calls
        self.assertEqual(bot, dict(id=self.bot['id'], session_id=self.bot['session_id']))
        # The chat has no execution session, so its conversation stays open.
        self.assertEqual(outcomes, [dict(execution_session_id=idle['_execution_session_id'], epoch='epoch-1')])
        # Every conversation gets its tip recorded before the prune: the chat and all work, idle or not.
        self.assertEqual(sorted((c['execution_session_id'] or '', c['epoch'] or '') for c in conversations),
                         [('', ''), ('active-work', ''), ('idle-work', 'epoch-1')])
        self.assertEqual(days, session_maintenance.SESSION_RETENTION_DAYS)

    def test_an_outcome_that_resumes_before_its_bot_keeps_its_session(self):
        self.task(_execution_session_id='idle-work')
        # The second check, just before the bot, finds the outcome active again.
        with patch.object(self.service, '_finished_outcomes', side_effect=[{'idle-work'}, set()]):
            self.service.maintain_native_sessions(current=0)
        [(bot, conversations, outcomes, days)] = self.calls
        self.assertEqual(outcomes, [])

    def test_status_only_and_old_tasks_do_not_select_sessions(self):
        self.task(_execution_session_id='owner-work', _work_status_only=True)
        self.task(_execution_session_id='old-work', updated_at='2026-07-01T10:00:00Z', created_at='2026-07-01T10:00:00Z')
        self.task(_execution_session_id='running-work', status='working', updated_at='2026-07-01T10:00:00Z')
        # The owner group is active, but the status-only task sits in its own group under its own id.
        with patch.object(self.service, '_finished_outcomes', return_value={'owner-work', 'old-work', 'running-work'}):
            self.service.maintain_native_sessions(current=datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp())
        [(bot, conversations, outcomes, days)] = self.calls
        self.assertNotIn('owner-work', [o['execution_session_id'] for o in outcomes])
        # The first pass records every conversation, even an old finished one.
        self.assertIn('old-work', [c['execution_session_id'] for c in conversations])
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            self.service.maintain_native_sessions(current=datetime(2026, 9, 25, 2, tzinfo=timezone.utc).timestamp())
        (bot, conversations, outcomes, days) = self.calls[-1]
        self.assertNotIn('old-work', [c['execution_session_id'] for c in conversations])
        # An unfinished conversation keeps its tip at any age.
        self.assertIn('running-work', [c['execution_session_id'] for c in conversations])

    def pending_vacuum(self, during=None, fail=False):
        """Make the first pass report a due VACUUM. `during` runs while the second pass has the bot on hold."""
        def maintain(bot, conversations, outcomes, days, allow_vacuum=False):
            self.calls.append(bot)
            self.vacuums.append(allow_vacuum)
            if not allow_vacuum:
                return dict(recorded=0, vacuum_pending=True)
            if during:
                during()
            if fail:
                raise RuntimeError('the store is locked')
            return dict(vacuumed=True)
        self.provider.maintain_sessions = maintain

    def run_maintenance(self, current=0):
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            return self.service.maintain_native_sessions(current=current)

    def test_a_due_vacuum_holds_new_turns_of_its_bot_and_only_that_bot(self):
        queued = self.task(status='queued', conversation_bot_id=self.bot['id'])
        other = self.service.mutate('POST', '/v1/bots', {'name': 'Tuner', 'role': ''}, str(uuid4()))
        (self.service.root / 'bots' / other['id'] / 'home').mkdir(parents=True, exist_ok=True)
        elsewhere = self.task(status='queued', bot_id=other['id'], conversation_bot_id=other['id'])
        seen = []
        self.pending_vacuum(during=lambda: seen.append((self.service._dispatch_eligible(queued, 0), self.service._dispatch_eligible(elsewhere, 0),
                                                        set(self.service._store_hold))))
        results = self.run_maintenance()
        # The held bot starts no turn during the rewrite. Another bot is not held.
        self.assertEqual(seen, [(False, True, {self.bot['id']})])
        self.assertEqual(self.vacuums[:2], [False, True])
        self.assertTrue(results[self.bot['id']]['vacuumed'])
        self.assertFalse(results[self.bot['id']]['vacuum_pending'])
        # The hold ends with the rewrite.
        self.assertEqual(self.service._store_hold, set())
        self.assertTrue(self.service._dispatch_eligible(queued, 0))

    def test_the_hold_ends_when_the_rewrite_fails(self):
        self.pending_vacuum(fail=True)
        with self.assertLogs(level='ERROR'):
            self.run_maintenance()
        self.assertEqual(self.service._store_hold, set())

    def test_a_failure_while_waiting_for_turns_still_ends_the_hold(self):
        self.pending_vacuum()
        with patch.object(self.service, '_bot_has_live_turn', side_effect=RuntimeError('unexpected')), \
                self.assertLogs(level='ERROR'):
            self.run_maintenance()
        self.assertEqual(self.service._store_hold, set())
        self.assertEqual(self.vacuums, [False])

    def test_a_pass_that_ends_without_a_vacuum_waits_long_before_the_next_hold(self):
        # The second pass finds another process on the store and skips the VACUUM.
        def maintain(bot, conversations, outcomes, days, allow_vacuum=False):
            self.vacuums.append(allow_vacuum)
            return dict(vacuum_pending=True) if not allow_vacuum else dict(vacuumed=False, vacuum_skipped='Another process holds the store.')
        self.provider.maintain_sessions = maintain
        first = self.run_maintenance(current=0)
        self.assertEqual(first[self.bot['id']]['vacuum_skipped'], 'Another process holds the store.')
        self.run_maintenance(current=3601)
        # An hour later the first pass reports a due VACUUM again, but the bot does not go on hold.
        self.assertEqual(self.vacuums, [False, True, False])
        self.run_maintenance(current=session_maintenance.VACUUM_DEFERRED_SECONDS + 1)
        self.assertEqual(self.vacuums[3:], [False, True])

    def test_a_turn_that_does_not_end_keeps_the_bot_running_and_maintenance_tries_again_soon(self):
        task = self.task(status='working')
        finished = threading.Event()
        worker = threading.Thread(target=finished.wait, daemon=True)
        worker.start()
        self.addCleanup(finished.set)
        self.service.running[task['id']] = worker
        self.pending_vacuum()
        with patch.object(session_maintenance, 'VACUUM_DRAIN_SECONDS', 0):
            self.run_maintenance(current=1000)
        self.assertEqual(self.vacuums, [False])
        self.assertEqual(self.service._store_hold, set())
        self.assertEqual(self.service._next_session_maintenance, 1000 + session_maintenance.VACUUM_RETRY_SECONDS)

    def test_the_vacuum_waits_for_a_running_turn_to_end(self):
        task = self.task(status='working')
        finished = threading.Event()
        worker = threading.Thread(target=finished.wait, daemon=True)
        worker.start()
        self.service.running[task['id']] = worker
        self.pending_vacuum()
        threading.Timer(0.3, finished.set).start()
        with patch.object(session_maintenance, 'VACUUM_DRAIN_SECONDS', 30):
            self.run_maintenance()
        self.assertEqual(self.vacuums, [False, True])

    def test_a_running_decision_context_keeps_its_bot_from_the_vacuum(self):
        task = self.task(_execution_session_id='asking', status='needs_you')
        context = dict(id='context', task_id=task['id'], bot_id=self.bot['id'], status='working')
        self.service._put('contexts', context)
        finished = threading.Event()
        worker = threading.Thread(target=finished.wait, daemon=True)
        worker.start()
        self.addCleanup(finished.set)
        self.service.context_threads['context'] = worker
        self.pending_vacuum()
        with patch.object(session_maintenance, 'VACUUM_DRAIN_SECONDS', 0):
            self.run_maintenance()
        self.assertEqual(self.vacuums, [False])

    def test_a_pending_decision_does_not_keep_its_bot_from_the_vacuum(self):
        self.task(_execution_session_id='asking', status='needs_you')
        self.pending_vacuum()
        self.run_maintenance()
        self.assertEqual(self.vacuums, [False, True])

    def test_a_claim_for_another_bot_does_not_delay_the_drain(self):
        self.service._kanban_claim_bots = {'another-bot'}
        self.assertFalse(self.service._bot_has_live_turn(self.bot['id']))
        # A claim for this bot can still start a turn, so the bot has one.
        self.service._kanban_claim_bots = {self.bot['id']}
        self.assertTrue(self.service._bot_has_live_turn(self.bot['id']))

    def test_a_pass_without_a_due_vacuum_holds_nothing(self):
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            self.service.maintain_native_sessions(current=0)
        self.assertEqual(self.vacuums, [False])
        self.assertEqual(self.service._store_hold, set())

    def test_a_live_status_only_turn_keeps_its_owner_session_open(self):
        self.task(_execution_session_id='owner-work')
        self.task(_execution_session_id='owner-work', _work_status_only=True, status='working')
        with patch.object(self.service, '_finished_outcomes', return_value={'owner-work'}):
            self.service.maintain_native_sessions(current=0)
        [(bot, conversations, outcomes, days)] = self.calls
        self.assertEqual(outcomes, [])

    def test_a_reseed_list_holds_tasks_that_had_a_turn_and_the_current_task(self):
        owner = self.task(_execution_session_id='work-x')
        done = self.task(_execution_session_id='work-x')
        queued = self.task(_execution_session_id='work-x', _work_status_only=True, status='queued')
        current = self.task(_execution_session_id='work-x', status='queued')
        with self.service.lock:
            ids = set(self.service._execution_task_ids(current))
        self.assertEqual(ids, {owner['id'], done['id'], current['id']})
        self.assertNotIn(queued['id'], ids)

    def test_a_pending_restart_stops_the_bot_loop(self):
        self.service.restart_pending = True
        with patch.object(self.service, '_finished_outcomes', return_value=set()):
            self.service.maintain_native_sessions(current=0)
        self.assertEqual(self.calls, [])

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
            # A VACUUM needs free disk. These programs must not depend on the disk of the machine that runs them.
            roomy = ("import collections, shutil\n"
                     "shutil.disk_usage = lambda path: collections.namedtuple('usage', 'total used free')(1 << 50, 0, 1 << 50)\n")
            result = subprocess.run([PYTHON, '-c', roomy + textwrap.dedent(program)], env=env,
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

    def test_the_first_maintenance_records_tips_before_it_prunes_and_spares_other_sources(self):
        self.run_native('''
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut import native_sessions
            from coconut.native_conversation import NativeConversation, TIP_KEY
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            root = bot['session_id']
            db = SessionDB()
            # An existing install: a compressed chat chain with no recorded tip, and an ended cron session.
            db.create_session(root, source='coconut')
            db.append_message(root, 'user', 'Use the blue label.')
            child = str(uuid4())
            db.create_session(child, source='coconut', parent_session_id=root)
            db.append_message(child, 'user', 'Summary: use the blue label.')
            db.end_session(root, 'compression')
            db.create_session('cron-run', source='cron')
            db.append_message('cron-run', 'user', 'Nightly report.')
            db.end_session('cron-run', 'cron_complete')
            assert db.get_meta(TIP_KEY + root) is None
            result = native_sessions.maintain(dict(bot=bot, conversations=[dict(execution_session_id=None, epoch=None)],
                                                   outcomes=[], retention_days=0, history_days=0, allow_vacuum=True))
            assert result['recorded'] == 1 and result['pruned'] >= 1, result
            assert db.get_session(root) is None
            # Coconut prunes only its own sessions. The profile's Hermes settings govern the rest.
            assert db.get_session('cron-run') is not None
            chat = NativeConversation(dict(operation='start', bot=bot, task=dict(id='chat'), history=[]), SessionDB())
            assert chat.session_id == child, chat.session_id
            chat.close()
        ''')

    def test_a_deferred_vacuum_runs_on_a_later_hour_without_a_new_prune(self):
        self.run_native('''
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            for index in range(40):
                db.create_session(f'old-{index}', source='coconut')
                for _ in range(20):
                    db.append_message(f'old-{index}', 'user', 'x' * 2000)
                db.end_session(f'old-{index}', 'coconut_idle')
            import time
            time.sleep(1.1)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=0, history_days=0, allow_vacuum=True)
            # Another process holds the store, so the first run prunes and defers its VACUUM.
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[(1, 'state.db')]):
                first = native_sessions.maintain(request)
            assert first['pruned'] >= 40 and not first['vacuumed'], first
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]):
                second = native_sessions.maintain(request)
            assert second['pruned'] == 0 and second['vacuumed'], second
        ''')

    def test_a_large_store_with_few_free_pages_vacuums_again_only_after_it_regrows(self):
        self.run_native('''
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            def grow(name, count):
                db.create_session(name, source='coconut')
                for _ in range(count):
                    db.append_message(name, 'user', 'x' * 2000)
            grow('first', 200)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=365, history_days=365, allow_vacuum=True)
            native_sessions.VACUUM_INTERVAL_SECONDS = 0
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \\
                    patch.object(native_sessions, 'VACUUM_MIN_BYTES', 1):
                # Nothing was deleted, so no page is free. The store is over the size floor and was never vacuumed.
                pages, free, _ = native_sessions.store_pages(db.db_path)
                assert free / pages < native_sessions.VACUUM_MIN_FREELIST_RATIO
                assert native_sessions.maintain(request)['vacuumed']
                # Nothing changed since the last VACUUM.
                assert not native_sessions.maintain(request)['vacuumed']
                grow('second', 400)
                assert native_sessions.maintain(request)['vacuumed']
        ''')

    def test_a_vacuum_waits_for_the_disk_to_hold_a_second_copy(self):
        self.run_native('''
            from collections import namedtuple
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            db.create_session('one', source='coconut')
            for _ in range(200):
                db.append_message('one', 'user', 'x' * 2000)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=365, history_days=365, allow_vacuum=True)
            usage = namedtuple('usage', 'total used free')
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \\
                    patch.object(native_sessions, 'VACUUM_MIN_BYTES', 1), \\
                    patch('shutil.disk_usage', return_value=usage(10, 9, 1024)):
                skipped = native_sessions.maintain(request)
                assert not skipped['vacuumed'] and 'disk' in skipped['vacuum_skipped'], skipped
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \\
                    patch.object(native_sessions, 'VACUUM_MIN_BYTES', 1):
                assert native_sessions.maintain(request)['vacuumed']
        ''')

    def test_an_unreadable_size_after_the_vacuum_keeps_the_run_result(self):
        self.run_native('''
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            db.create_session('one', source='coconut')
            for _ in range(200):
                db.append_message('one', 'user', 'x' * 2000)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=365, history_days=365, allow_vacuum=True)
            real = native_sessions.store_pages
            calls = []
            def store_pages(path):
                calls.append(path)
                # The verdict reads the store first. The read after the VACUUM fails.
                if len(calls) > 1:
                    raise RuntimeError('database is locked')
                return real(path)
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \\
                    patch.object(native_sessions, 'VACUUM_MIN_BYTES', 1), \\
                    patch.object(native_sessions, 'store_pages', store_pages):
                result = native_sessions.maintain(request)
            assert result['vacuumed'] and 'locked' in result['vacuum_error'], result
        ''')

    def test_a_due_store_is_reported_and_not_vacuumed_until_the_service_allows_it(self):
        self.run_native('''
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            db.create_session('one', source='coconut')
            for _ in range(200):
                db.append_message('one', 'user', 'x' * 2000)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=365, history_days=365, allow_vacuum=False)
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \\
                    patch.object(native_sessions, 'VACUUM_MIN_BYTES', 1):
                first = native_sessions.maintain(request)
                assert first['vacuum_pending'] and not first['vacuumed'], first
                second = native_sessions.maintain(dict(request, allow_vacuum=True))
                assert second['vacuumed'] and not second['vacuum_pending'], second
        ''')

    def test_a_small_store_is_not_vacuumed_for_its_size(self):
        self.run_native('''
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            db.create_session('one', source='coconut')
            db.append_message('one', 'user', 'x' * 2000)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=365, history_days=365, allow_vacuum=True)
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]):
                assert not native_sessions.maintain(request)['vacuumed']
        ''')

    def test_a_failed_vacuum_keeps_the_rest_of_the_run(self):
        self.run_native('''
            import time
            from uuid import uuid4
            from unittest.mock import patch
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            for index in range(40):
                db.create_session(f'old-{index}', source='coconut')
                for _ in range(20):
                    db.append_message(f'old-{index}', 'user', 'x' * 2000)
                db.end_session(f'old-{index}', 'coconut_idle')
            time.sleep(1.1)
            request = dict(bot=bot, conversations=[], outcomes=[], retention_days=0, history_days=0, allow_vacuum=True)
            with patch('hermes_state_holders.foreign_state_db_holders', return_value=[]), \
                    patch.object(SessionDB, 'vacuum', side_effect=RuntimeError('database is locked')):
                result = native_sessions.maintain(request)
            assert result['pruned'] >= 40 and not result['vacuumed'] and 'locked' in result['vacuum_error'], result
        ''')

    def test_idle_work_goes_after_its_retention_while_compressed_chat_stays_searchable(self):
        self.run_native('''
            import time
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut import native_sessions
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            db = SessionDB()
            db.create_session('idle-work', source='coconut')
            db.append_message('idle-work', 'user', 'Old work.')
            db.end_session('idle-work', 'coconut_idle')
            db.create_session('chat-part', source='coconut')
            db.append_message('chat-part', 'user', 'A decision from last week.')
            db.end_session('chat-part', 'compression')
            time.sleep(1.1)
            result = native_sessions.maintain(dict(bot=bot, conversations=[], outcomes=[], retention_days=0,
                                                   history_days=30, allow_vacuum=False))
            assert result['pruned'] == 1, result
            assert db.get_session('idle-work') is None
            assert db.get_session('chat-part') is not None
        ''')

    def test_a_failed_tip_write_does_not_fail_the_turn(self):
        self.run_native('''
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut.native_conversation import NativeConversation
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            conversation = NativeConversation(dict(operation='start', bot=bot, task=dict(id='chat'), history=[]), SessionDB())
            def refuse(*arguments):
                raise RuntimeError('database is locked')
            conversation.db.set_meta = refuse
            conversation.close()
        ''')

    def test_an_idle_outcome_session_ends_reopens_and_returns_after_a_prune(self):
        self.run_native('''
            from uuid import uuid4
            from hermes_state import SessionDB
            from coconut import native_sessions
            from coconut.native_conversation import NativeConversation, IDLE_END_REASON, session_root
            bot = dict(id=str(uuid4()), session_id=str(uuid4()))
            history = [dict(id='m1', task_id='work', role='user', kind='text', text='Fix the upload row.'),
                       dict(id='m2', task_id='work', role='assistant', kind='text', text='I will fix it.'),
                       dict(id='m3', task_id='follow-up', role='user', kind='text', text='Also keep the retry button.'),
                       dict(id='m4', task_id='other-work', role='user', kind='text', text='Unrelated request.')]
            request = dict(operation='resume', bot=bot, history=history,
                           task=dict(id='work', _execution_session_id='work', _execution_task_ids=['work', 'follow-up']))
            conversation = NativeConversation(request, SessionDB())
            conversation.close()
            db = SessionDB()
            chat = dict(operation='start', bot=bot, task=dict(id='chat'), history=[])
            NativeConversation(chat, SessionDB()).close()
            result = native_sessions.maintain(dict(bot=bot, conversations=[], outcomes=[dict(execution_session_id='work')], retention_days=7))
            assert result['ended'] == 1, result
            work = db.get_session(session_root(bot, execution='work'))
            assert work['end_reason'] == IDLE_END_REASON, work
            # Coconut never ends the chat.
            assert not db.get_session(bot['session_id'])['ended_at']
            resumed = NativeConversation(request, SessionDB())
            assert not db.get_session(resumed.session_id)['ended_at']
            resumed.close()
            # After Hermes prunes the idle session, a resume seeds the visible conversation again.
            result = native_sessions.maintain(dict(bot=bot, conversations=[], outcomes=[dict(execution_session_id='work')], retention_days=0))
            assert result['pruned'] >= 1, result
            assert db.get_session(session_root(bot, execution='work')) is None
            returned = NativeConversation(request, SessionDB())
            texts = [row['content'] for row in returned.history]
            # An earlier task of the same work keeps its messages. Another work's message stays out.
            assert texts == ['Fix the upload row.', 'I will fix it.', 'Also keep the retry button.'], texts
            returned.close()
        ''')


if __name__ == '__main__':
    unittest.main()
