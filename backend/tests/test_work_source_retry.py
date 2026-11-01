import time
import json
import unittest
from unittest.mock import patch

import test_work_source_coverage as fixtures


class WorkSourceRetryTests(unittest.TestCase):
    setUp = fixtures.WorkSourceTests.setUp
    audit = fixtures.WorkSourceTests.audit

    def finish(self, identity, status='completed'):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task['status'] = status
            self.service._put('tasks', task)

    def removed_source(self):
        binding = dict(updated_at=self.item['updated_at'], decision_ids=['pending-card'])
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: binding}))
        self.service._put('decisions', dict(id='pending-card', task_id='old-owner', status='pending'))
        self.items = []
        return binding

    def test_unresolved_assignment_retries_after_backoff_without_a_lifetime_limit(self):
        first = self.audit()['task_id']
        self.finish(first)
        second = self.audit()['task_id']
        self.finish(second, 'failed')
        self.assertEqual(self.audit()['task_id'], second)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            third = self.audit()['task_id']
        self.assertNotIn(third, [first, second])
        self.assertEqual(len(self.service._all('messages')), 0)

    def test_completed_removed_source_with_valid_card_stays_reconciled(self):
        binding = self.removed_source()
        first = self.audit()['task_id']
        self.finish(first)

        self.assertEqual(self.audit()['task_id'], first)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            for _ in range(3):
                self.assertEqual(self.audit()['task_id'], first)
        self.assertEqual(len(self.service._all('tasks')), 1)

        binding['updated_at'] = '2026-09-14T13:00:00Z'
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: binding}))
        second = self.audit()['task_id']
        self.assertNotEqual(second, first)
        self.finish(second)

        binding['decision_ids'].append('new-pending-card')
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: binding}))
        self.service._put('decisions', dict(id='new-pending-card', task_id='new-owner', status='pending'))
        self.assertNotIn(self.audit()['task_id'], (first, second))

    def test_completed_removed_source_does_not_loop_after_audit_appends_itself(self):
        old_owner = self.service._send(
            self.bot, {'text': 'Handle the source assignment', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            owner = self.service._get('tasks', old_owner['id'])
            owner['status'] = 'needs_you'
            self.service._put('tasks', owner)
            self.service._put('decisions', dict(
                id='pending-card', task_id=owner['id'], bot_id=self.bot['id'], status='pending', revision=1,
                action_type='question', title='Choose the follow-up', context='The source needs a human choice.'))
        binding = dict(updated_at=self.item['updated_at'], decision_ids=['pending-card'])
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: binding}))
        self.items = []
        first = self.audit()['task_id']
        prompts = []

        def start(task, bot, history, emit):
            prompt = fixtures.turn_input(dict(operation='start', task=task, history=history))
            prompts.append(prompt)
            saved = json.loads((self.root / 'coverage.json').read_text())
            saved[self.item['id']]['task_ids'] = [task['id']]
            (self.root / 'coverage.json').write_text(json.dumps(saved))
            emit('completed', {'summary': 'The removed source and pending card were reconciled.'})

        self.provider.start_task = start
        self.service._run_task(first)
        self.assertIn('task_ids or decision_ids', str(prompts[0]))
        self.assertEqual(self.service._get('tasks', first)['status'], 'completed')
        self.assertEqual(json.loads((self.root / 'coverage.json').read_text())
                         [self.item['id']]['task_ids'], [first])

        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            for _ in range(3):
                self.assertEqual(self.audit()['task_id'], first)
        self.assertEqual(len(self.service._all('tasks')), 2)

        binding['task_ids'] = [first]
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: binding}))
        card = self.service._get('decisions', 'pending-card')
        card['revision'] = 2
        self.service._put('decisions', card)
        self.assertNotEqual(self.audit()['task_id'], first)

    def test_failed_removed_source_retries_with_backoff(self):
        self.removed_source()
        first = self.audit()['task_id']
        self.finish(first, 'failed')
        second = self.audit()['task_id']
        self.assertNotEqual(second, first)
        self.finish(second, 'failed')
        self.assertEqual(self.audit()['task_id'], second)

        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            third = self.audit()['task_id']
        self.assertNotIn(third, (first, second))

    def test_dead_wait_is_revisited_but_active_child_is_preserved(self):
        first = self.audit()['task_id']
        self.finish(first, 'waiting')
        child = self.service._send(self.bot, {'text': 'Read evidence', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            task = self.service._get('tasks', first)
            task['_waiting_on'] = [child['id']]
            self.service._put('tasks', task)
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertEqual(self.audit()['task_id'], first)
            self.finish(child['id'])
            self.assertNotEqual(self.audit()['task_id'], first)

    def test_receipt_wait_and_its_pending_decision_prevent_duplicate_recovery(self):
        first = self.audit()['task_id']
        self.finish(first, 'waiting')
        child = self.service._send(self.bot, {'text': 'Check evidence', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            self.service._put('native_receipts', dict(id='wait-receipt', task_id=first, state='completed',
                action={'name': 'coconut_wait'}, _wait_task_id=child['id']))
        with patch('coconut.work_sources.time.time', return_value=time.time() + 4000):
            self.assertEqual(self.audit()['task_id'], first)
            self.finish(child['id'], 'needs_you')
            with self.service.transaction():
                self.service._put('decisions', dict(id='card', task_id=child['id'], status='pending'))
            self.assertEqual(self.audit()['task_id'], first)
            with self.service.transaction():
                self.service._put('decisions', dict(id='card', task_id=child['id'], status='executed'))
            self.assertNotEqual(self.audit()['task_id'], first)


if __name__ == '__main__':
    unittest.main()
