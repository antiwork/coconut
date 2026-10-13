"""A block check records a declined card through the worker, so the next check waits."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider, HermesProvider
from coconut.service import Service, now
from coconut.work_recovery import block_check_delay
import test_provider as fixtures

AUDIT_PROMPT = ('Reconcile these open assignments against the original sources and existing work. '
                'Read prior decisions before asking anything.')


class BlockCheckPauseProviderTests(unittest.TestCase):
    setUp = fixtures.ProviderTests.setUp
    tearDown = fixtures.ProviderTests.tearDown
    fake_hermes = fixtures.ProviderTests.fake_hermes

    def check(self, native, clock):
        with patch('coconut.native_outcomes.time.time', return_value=clock), self.service.transaction():
            self.service._native_block_check(self.service._get('tasks', self.task_id), native)
        return self.service._get('tasks', self.task_id)

    def test_assignment_reconcile_check_records_the_declined_card(self):
        # The incident shape: a native outcome whose Work has no key field, checked in an assignment-reconcile turn.
        fixture = self.fake_hermes('''
            import json
            names = set(registry.handlers)
            assert 'coconut_track_work' not in names, names
            assert 'Reconcile these open assignments' in prompt
            assert 'Coconut queued this check because the native task stayed blocked' in prompt
            assert DECISION in prompt
            if 'coconut_record_pause' in names:
                receipt = json.loads(registry.dispatch('coconut_record_pause', {'decision_id': DECISION}))
                assert receipt == {'status': 'recorded', 'decision_id': DECISION}, receipt
            return {'completed': True, 'final_response': 'The declined card still controls this block.'}
        '''.replace('DECISION', repr('9b3a046e-5f42-4b97-b614-94b7de7c66f3')))
        # A native claim runs in its workspace. The shared stub has no workspace override.
        with (self.root / 'source' / 'tools' / 'terminal_tool.py').open('a') as stub:
            stub.write('def register_task_env_overrides(task_id, overrides): pass\n')
        self.service = Service(str(self.root / 'service'), FixtureProvider(str(self.root / 'service')), start_workers=False)
        self.addCleanup(self.service.close)
        self.service.provider = HermesProvider(fixture.data_dir, python=fixture.python,
                                               hermes_root=fixture.hermes_root, catalog=fixture.catalog)
        self.addCleanup(self.service.provider.close)
        bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        task = self.service.mutate('POST', f"/v1/bots/{bot['id']}/messages",
                                   {'text': 'Suspend the seller and release the payout', 'mode': 'queue'}, str(uuid4()))
        self.task_id = task['id']
        with self.service.transaction():
            self.service._put('artifacts', dict(id='work-3298', bot_id=bot['id'], task_id=task['id'], kind='work',
                title='Suspend the seller and release the payout', status='waiting', activity='Payout blocked',
                preview='', url=None, created_at='2026-10-06T15:27:19Z', _work_key='gp3298-kinkyevents-flag',
                _native_outcome_id='t_14ed0a56', _work_previous_task_ids=[task['id']], _hidden=False))
            self.service._put('decisions', dict(id='9b3a046e-5f42-4b97-b614-94b7de7c66f3', task_id=task['id'],
                bot_id=bot['id'], status='rejected', decided_by='person', decided_at=now(), created_at=now(), title='Clear the payout pause?', options=[], revision=1))
            saved = self.service._get('tasks', task['id'])
            saved.update(status='completed', _native_outcome_id='t_14ed0a56', _execution_session_id=task['id'],
                         _work_context_id='work-3298', _source_audit_prompt=AUDIT_PROMPT,
                         _kanban_claim=dict(native_id='t_14ed0a56', run_id=1, claim_lock='claim-3298', external_id=task['id'],
                                            context='# Kanban task t_14ed0a56: Suspend the seller and release the payout'))
            self.service._put('tasks', saved)
        blocked = dict(id='t_14ed0a56', status='triage', block_kind='needs_input')
        self.check(blocked, 0)
        queued = self.check(blocked, 10 ** 9)
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(queued['_self_recovery'].get('work', []), [])
        # The check turn runs as the native claim.
        self.check(dict(blocked, status='running', current_run_id=1), 10 ** 9 + 1)
        workspace = self.root / 'workspace'
        workspace.mkdir()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            # The native dispatcher starts the claim's worker before the turn.
            self.service.provider.prepare_kanban_worker(self.task_id, self.service._public(self.service._get('bots', bot['id'])),
                                                        str(workspace), self.task_id)
            self.service._run_task(self.task_id)
        finished = self.service._get('tasks', self.task_id)
        self.assertNotEqual(finished.get('error_code'), 'native_dispatch_failed')
        self.assertNotIn(finished['status'], ('failed', 'queued', 'working'), (finished.get('summary'), finished.get('error_code'), finished.get('_error_detail')))
        # The worker blocked the task again. The next check must wait for the person.
        armed = self.check(blocked, 2 * 10 ** 9)['_block_check']
        due = max(armed['next_at'], 2 * 10 ** 9 + block_check_delay(armed['attempt']))
        self.assertEqual(self.check(blocked, due)['status'], finished['status'])
        self.assertEqual(armed.get('paused_by_decision_id'), '9b3a046e-5f42-4b97-b614-94b7de7c66f3')
        self.assertEqual(self.service._get('decisions', '9b3a046e-5f42-4b97-b614-94b7de7c66f3')['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
