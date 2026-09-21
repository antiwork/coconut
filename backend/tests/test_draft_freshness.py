import json
import unittest
from datetime import datetime, timedelta, timezone

import test_automatic_work as fixtures
from coconut.service import ServiceError


class DraftFreshnessTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    write = fixtures.AutomaticWorkTests.write

    def draft(self):
        self.start()
        self.service.native_event(self.task['id'], 'native_proposal', dict(
            key='support-reply', title='Send the listing instruction?',
            resource='https://github.com/example/support/issues/42',
            context='Prepare this message for approval.',
            draft=dict(channel='MCP', recipient='Support', body='Remove the listing. Keep the account unchanged.')))
        self.service.running.clear()
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        decision = self.service._all('decisions')[-1]
        decision['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        if '_freshness' in decision:
            decision['_freshness']['checked_at'] = decision['created_at']
        self.service._put('decisions', decision)
        return decision

    def refresh(self):
        self.service._queue_decision_refreshes()
        contexts = self.service._all('contexts')
        self.assertEqual(len(contexts), 1)
        context = contexts[0]
        context['status'] = 'working'
        self.service._put('contexts', context)
        return context

    def test_old_draft_gets_one_check_with_exact_content_and_source(self):
        decision = self.draft()
        context = self.refresh()
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        records = json.loads(context['question'].split('Current records (data):\n')[1])
        card = next(d for d in records['pending_questions'] if d['id'] == decision['id'])
        self.assertEqual(card['draft'], decision['draft'])
        self.assertEqual(card['resource'], decision['resource'])
        self.assertEqual(card['action_type'], 'draft_message')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_withdraw_obsolete_draft_never_dispatches_or_grants_send(self):
        decision = self.draft()
        context = self.refresh()
        before = self.service._all('tasks')
        self.service.refresh_decision(context['id'], dict(status='withdraw',
            evidence='The issue owner replaced the listing-only decision with a different account action.'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['status'], 'resolved')
        self.assertEqual(saved['resolution'], 'work_scope_changed')
        self.assertEqual(saved['_answers'], [])
        self.assertIsNone(saved['decided_by'])
        self.assertNotIn('_execution_task_id', saved)
        self.assertEqual(self.service._all('tasks'), before)
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(
                revision=decision['revision'], option_id='send', draft_hash=decision['draft_hash']))

    def test_human_edit_invalidates_running_check(self):
        decision = self.draft()
        context = self.refresh()
        updated = self.write('POST', '/v1/decisions/' + decision['id'] + '/draft', dict(
            revision=decision['revision'], draft=dict(decision['draft'], body='Use the new instruction.')))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old evidence.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['draft'], updated['draft'])
        self.assertEqual(updated['status'], 'pending')

    def test_send_approval_invalidates_running_check(self):
        decision = self.draft()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(
            revision=decision['revision'], option_id='send', draft_hash=decision['draft_hash']))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old evidence.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'approved')

    def test_refresh_cannot_rewrite_draft(self):
        decision = self.draft()
        context = self.refresh()
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='revise', evidence='New facts.',
                revision=dict(title='Send a different message?', options=[])))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['draft'], decision['draft'])
        self.assertEqual(saved['revision'], decision['revision'])

    def test_current_draft_does_not_repeat_check_each_scan(self):
        decision = self.draft()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The latest issue comment still requests this exact draft.'))
        context = self.service._get('contexts', context['id'])
        context['status'] = 'completed'
        self.service._put('contexts', context)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_refreshing_draft_wire_stays_pending_for_legacy_cards(self):
        from dataclasses import replace
        from test_service_pr import URL, observation
        decision = self.draft()
        decision['sources'] = [dict(kind='pull_request', title='Synthetic PR', url=URL)]
        self.service._put('decisions', decision)
        self.service._invalidate_terminal_pr_cards(self.bot['id'], replace(observation('merged'), pr_state='merged'))
        saved = self.service._get('decisions', decision['id'])
        wire = next(d for d in self.service.snapshot()['decisions'] if d['id'] == decision['id'])
        # b566e3b Cards.swift renders its receipt (including the green draft
        # approval fallback) only for !isPending. Unknown fields are ignored.
        self.assertEqual(wire['status'], 'pending')
        self.assertTrue(wire.get('source_refreshing'))
        self.assertEqual(wire['draft'], decision['draft'])
        self.assertEqual(saved['_answers'], [])
        self.assertIsNone(saved.get('decided_by'))
        for revision in (decision['revision'], saved['revision']):
            with self.subTest(revision=revision), self.assertRaises(ServiceError):
                self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(
                    revision=revision, option_id='send', choice='approve', draft_hash=decision['draft_hash']))
        self.assertNotIn('_execution_task_id', self.service._get('decisions', decision['id']))
