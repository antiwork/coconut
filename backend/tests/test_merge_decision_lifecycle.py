import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, YieldForDecision
from coconut.pr_tracking import PRObservation

URL = 'https://github.com/example/project/pull/42'


class MergeDecisionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.tmp.name), FixtureProvider(self.tmp.name), start_workers=False)
        self.bot = self.write('/v1/bots', {'name': 'Desk', 'role': ''})
        self.task = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Review this PR'})
        self.observation = self.observed('open')
        self.service.pr_tracker = type('Tracker', (), {'observe': lambda _, url: self.observation})()
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.artifact = self.service._register_pr_artifact(self.task['id'], {'url': URL})

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def write(self, path, data):
        return self.service.mutate('POST', path, data, str(uuid4()))

    def observed(self, state):
        return PRObservation('merged' if state == 'merged' else 'pending', 'a' * 40, 'Change', URL,
                             state, '2026-09-13T10:00:00Z', True, state, False, 'pending')

    def question(self, **extra):
        payload = dict(title='Merge this PR?', options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')], **extra)
        receipt = self.service.emit(self.task['id'], 'native_call', {'name': 'coconut_ask', 'arguments': payload})
        with self.assertRaises(YieldForDecision):
            self.service.emit(self.task['id'], 'native_question', dict(payload, receipt_id=receipt['id']))
        return self.service._all('decisions')[-1], receipt['id']

    def test_verified_merge_resolves_and_resumes_once_without_approval(self):
        decision, receipt_id = self.question(merge_pr_url=URL)
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        resolved = self.service._get('decisions', decision['id'])
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['revision'], 2)
        self.assertEqual(resolved['_answers'], [])
        self.assertIsNone(resolved['decided_by'])
        self.assertNotIn('selected_option_id', resolved)
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_resume_decision'], decision['id'])
        receipt = self.service._get('native_receipts', receipt_id)
        self.assertEqual(receipt['state'], 'completed')
        self.assertEqual(json.loads(receipt['result'])['resolution'], 'merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id']), resolved)
        with self.assertRaises(ServiceError) as error:
            self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        self.assertEqual(error.exception.status, 409)

    def test_restart_reconciles_saved_observation(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.observation = self.observed('merged')
        self.service._refresh_pull_requests()
        self.service.close()
        self.service = Service(Path(self.tmp.name), FixtureProvider(self.tmp.name), start_workers=False)
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'resolved')

    def test_text_alone_does_not_link_or_resolve_a_card(self):
        decision, _ = self.question()
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_failed_observation_and_closed_pr_do_not_resolve(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.observation = PRObservation(None, None, None, URL, 'Unavailable', '2026-09-13T10:00:00Z', False)
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.observation = self.observed('closed')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_untracked_url_is_rejected(self):
        with self.assertRaises(ServiceError):
            self.question(merge_pr_url=URL.replace('/42', '/43'))
        self.assertEqual(self.service._all('decisions'), [])

    def test_merge_card_waits_for_the_delegated_review_result(self):
        dev = self.write('/v1/bots', {'name': 'Dev', 'role': ''})
        review = self.service._store_bot_message(self.task['id'],
            dict(target_bot_id=dev['id'], text='Review the current PR head.'))
        with self.assertRaisesRegex(ServiceError, 'pending worker or delegated result') as error:
            self.question(merge_pr_url=URL)
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.service._all('decisions'), [])
        task = self.service._get('tasks', self.task['id'])
        self.assertIn(review['task_id'], task['_waiting_on'])
        self.service._run_task(review['task_id'])
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_resolved_dependency_id') == review['task_id'])
        self.assertEqual(callback['_assignment_task_id'], task['id'])
        callback['status'] = 'working'
        self.service._put('tasks', callback)
        self.task = callback
        decision, _ = self.question(merge_pr_url=URL)
        self.assertEqual(decision['status'], 'pending')
        self.assertIn(review['task_id'], self.service._get('tasks', task['id'])['_waiting_on'])

    def test_merge_card_can_follow_the_returned_process_review(self):
        receipt = self.service.emit(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'background': True}))
        self.service.emit(self.task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps({'session_id': 'proc_review', 'notify_on_complete': True, 'status': 'running'})))
        with self.assertRaisesRegex(ServiceError, 'pending worker'):
            self.question(merge_pr_url=URL)
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        record = self.service._get('native_receipts', receipt['id'])
        with self.service.transaction():
            self.service._resume_process_task(task['id'], [(record, 'The review passed.')])
        task = self.service._get('tasks', task['id'])
        self.assertTrue(task['_background_completion'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        decision, _ = self.question(merge_pr_url=URL)
        self.assertEqual(decision['status'], 'pending')

    def test_merge_request_does_not_wait_for_its_own_card(self):
        coordinator = self.write('/v1/bots', {'name': 'Coordinator', 'role': ''})
        owner = self.task
        request = self.service._store_bot_message(owner['id'],
            dict(target_bot_id=coordinator['id'], text='The review passed. Offer the merge decision.',
                 require_response=True, native_envelope_id=uuid4().hex))
        receipt = self.service.emit(owner['id'], 'native_call', dict(name='send_message', arguments={}))
        with self.service.transaction():
            record = self.service._get('native_receipts', receipt['id'])
            record.update(state='completed', _relay_task_id=request['task_id'])
            self.service._put('native_receipts', record)
            task = self.service._get('tasks', request['task_id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        self.service._register_pr_artifact(task['id'], {'url': URL})
        self.task = task
        reviewer = self.write('/v1/bots', {'name': 'Reviewer', 'role': ''})
        other = self.service._store_bot_message(owner['id'],
            dict(target_bot_id=reviewer['id'], text='Verify another review finding.'))
        with self.assertRaisesRegex(ServiceError, 'pending worker'):
            self.question(merge_pr_url=URL)
        with self.service.transaction():
            parent = self.service._get('tasks', owner['id'])
            parent['_waiting_on'].remove(other['task_id'])
            self.service._put('tasks', parent)
        process = self.service.emit(owner['id'], 'native_call', dict(name='terminal', arguments={}))
        self.service.emit(owner['id'], 'native_result', dict(id=process['id'],
            result=json.dumps(dict(session_id='independent-review', notify_on_complete=True))))
        with self.assertRaisesRegex(ServiceError, 'pending worker'):
            self.question(merge_pr_url=URL)
        with self.service.transaction():
            record = self.service._get('native_receipts', process['id'])
            record['_process_delivery'] = 'consumed'
            self.service._put('native_receipts', record)
        decision, _ = self.question(merge_pr_url=URL)
        self.assertEqual(decision['status'], 'pending')
        self.assertIn(task['id'], self.service._get('tasks', owner['id'])['_waiting_on'])
        self.assertNotIn('_process_delivery', self.service._get('native_receipts', receipt['id']))

    def test_cancelled_review_dependencies_do_not_block_the_current_review(self):
        old = self.service._get('tasks', self.task['id'])
        old['_waiting_on'] = ['cancelled-review']
        self.service._put('tasks', old)
        with self.service.transaction():
            self.service._cancel_one(old, [])
        self.assertEqual(self.service._get('tasks', old['id'])['_waiting_on'], ['cancelled-review'])
        self.task = self.other_task(self.write('/v1/bots', {'name': 'Current reviewer'}))
        decision, _ = self.question(merge_pr_url=URL)
        self.assertEqual(decision['status'], 'pending')

    def test_optional_card_resolution_does_not_restart_finished_work(self):
        self.service.emit(self.task['id'], 'native_proposal', dict(key='merge', title='Merge?', merge_pr_url=URL,
                            options=[dict(id='merge', label='Merge', explanation='Merge it.')]))
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
        self.observation = self.observed('merged')
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._all('decisions')[0]['status'], 'resolved')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'completed')

    def other_task(self, bot=None):
        bot = bot or self.bot
        task = self.service._send(self.service._get('bots', bot['id']),
            {'text': 'Assess this PR', 'mode': 'queue'}, human_authored=False)
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        self.service._register_pr_artifact(task['id'], {'url': URL})
        return stored

    def test_separate_tasks_share_one_pending_merge_decision(self):
        decision, _ = self.question(merge_pr_url=URL)
        task = self.other_task()
        payload = dict(title='Merge the correction?', merge_pr_url=URL, options=decision['options'])
        receipt = self.service.emit(task['id'], 'native_call', {'name': 'coconut_ask', 'arguments': payload})
        with self.assertRaises(ServiceError) as error:
            self.service.emit(task['id'], 'native_question', dict(payload, receipt_id=receipt['id']))
        self.assertEqual(error.exception.status, 400)
        self.assertIn(decision['id'], str(error.exception))
        self.assertIn(decision['task_id'], str(error.exception))
        self.assertEqual(self.service._all('decisions'), [decision])
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'working')
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')

    def test_bots_and_optional_proposals_share_the_pending_merge_decision(self):
        decision, _ = self.question(merge_pr_url=URL)
        bot = self.write('/v1/bots', {'name': 'Dev', 'role': ''})
        task = self.other_task(bot)
        with self.assertRaises(ServiceError):
            self.service.emit(task['id'], 'native_proposal', dict(key='merge-again', title='Merge?',
                merge_pr_url=URL.upper().replace('HTTPS://GITHUB.COM', 'https://github.com').replace('/PULL/', '/pull/'),
                options=decision['options']))
        self.assertEqual(len(self.service._all('decisions')), 1)

    def test_recorded_choice_prevents_automated_reasks_for_the_same_head(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        task = self.other_task()
        with self.assertRaises(ServiceError):
            self.service.emit(task['id'], 'native_proposal', dict(key='merge-again', title='Merge?',
                merge_pr_url=URL, options=decision['options']))
        self.assertEqual(len(self.service._all('decisions')), 1)

    def test_new_human_request_can_reconsider_a_recorded_choice(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        task = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Reconsider this merge before proceeding', 'mode': 'queue'})
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        self.service.emit(task['id'], 'native_proposal', dict(key='reconsider', title='Leave this PR open?',
            merge_pr_url=URL, options=decision['options']))
        self.assertEqual(len(self.service._all('decisions')), 2)

    def test_human_reply_on_the_existing_task_can_reconsider_a_choice(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        replied = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Reconsider this merge first.', 'mode': 'auto'})
        self.assertEqual(replied['id'], self.task['id'])
        task = self.service._get('tasks', replied['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service.emit(task['id'], 'native_proposal', dict(key='reconsider', title='Leave this PR open?',
            merge_pr_url=URL, options=decision['options']))
        self.assertEqual(len(self.service._all('decisions')), 2)

        self.service.emit(task['id'], 'completed', {'summary': 'The revised merge choice is in Inbox.'})
        task = self.service._get('tasks', task['id'])
        self.assertEqual(task['_preference_source_ids'], [])
        reconsidered = self.service._all('decisions')[-1]
        with self.service.transaction():
            self.service._revise_question(reconsidered, task,
                dict(title='Leave the reviewed PR open?', options=decision['options']), 'Refresh the saved question.')
        self.assertEqual(self.service._get('decisions', reconsidered['id'])['revision'], 2)
        self.write(f"/v1/decisions/{reconsidered['id']}/answer", {'choice': 'reply', 'revision': 2,
            'correction': 'Clarify the remaining risk before asking again.'})
        answered = self.service._get('decisions', reconsidered['id'])
        continuation = self.service._get('tasks', answered['_execution_task_id'])
        continuation['status'] = 'working'
        self.service._put('tasks', continuation)
        self.service.emit(continuation['id'], 'native_proposal', dict(key='clarified', title='Leave the PR open?',
            merge_pr_url=URL, options=decision['options']))
        followup = self.service._all('decisions')[-1]
        self.assertEqual(followup['previous_decision_id'], reconsidered['id'])
        self.assertEqual(followup['status'], 'pending')

    def test_deleted_bot_cannot_hold_a_pending_merge_proposal(self):
        self.service.emit(self.task['id'], 'native_proposal', dict(key='merge', title='Merge?', merge_pr_url=URL,
            options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')]))
        decision = self.service._all('decisions')[0]
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service.mutate('DELETE', f"/v1/bots/{self.bot['id']}", {}, str(uuid4()))
        bot = self.write('/v1/bots', {'name': 'New Desk', 'role': ''})
        task = self.other_task(bot)
        self.service.emit(task['id'], 'native_proposal', dict(key='merge', title='Merge?',
            merge_pr_url=URL, options=decision['options']))
        self.assertEqual(self.service._all('decisions')[-1]['bot_id'], bot['id'])

    def check_dismissal_order(self, legacy=False):
        payload = dict(key='merge', title='Merge?', merge_pr_url=URL,
            options=[dict(id='merge', label='Merge', explanation='Merge the reviewed change.')])
        self.service.emit(self.task['id'], 'native_proposal', payload)
        decision = self.service._all('decisions')[0]
        other = self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Assess this merge again.', 'mode': 'queue'})
        self.write(f"/v1/decisions/{decision['id']}/inbox", {'action': 'dismiss', 'revision': 1})
        if legacy:
            dismissed = self.service._get('decisions', decision['id'])
            dismissed.pop('_inbox_dismissed_at')
            self.service._put('decisions', dismissed)
        task = self.service._get('tasks', other['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        with self.assertRaises(ServiceError):
            self.service.emit(task['id'], 'native_proposal', payload)
        self.write(f"/v1/bots/{self.bot['id']}/messages", {'text': 'Reconsider the dismissed merge now.', 'mode': 'auto'})
        self.service.emit(task['id'], 'native_proposal', payload)
        self.assertEqual(self.service._all('decisions')[-1]['status'], 'pending')

    def test_only_a_request_after_dismissal_can_reconsider_a_proposal(self):
        self.check_dismissal_order()

    def test_legacy_dismissal_uses_its_saved_event(self):
        self.check_dismissal_order(legacy=True)

    def test_changed_head_requires_its_own_decision_after_an_answer(self):
        from dataclasses import replace
        decision, _ = self.question(merge_pr_url=URL)
        self.write(f"/v1/decisions/{decision['id']}/answer", {'option_id': 'merge', 'revision': 1})
        self.observation = replace(self.observation, head_sha='b' * 40)
        self.service.refresh_pull_requests()
        task = self.other_task()
        self.service.emit(task['id'], 'native_proposal', dict(key='new-head', title='Merge the new changes?',
            merge_pr_url=URL, options=decision['options']))
        decisions = self.service._all('decisions')
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[-1]['_merge_pr_head'], 'b' * 40)
        self.assertEqual(decisions[-1]['status'], 'pending')

    def test_text_reply_can_lead_to_a_revised_merge_question(self):
        decision, _ = self.question(merge_pr_url=URL)
        self.write(f"/v1/decisions/{decision['id']}/answer", {'choice': 'reply', 'revision': 1,
            'correction': 'Explain the risk before asking again.'})
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        revised, _ = self.question(merge_pr_url=URL, risk='The change can reject valid requests.')
        self.assertEqual(revised['previous_decision_id'], decision['id'])
        self.assertEqual(revised['status'], 'pending')

    def test_revising_another_question_cannot_create_a_second_merge_decision(self):
        decision, _ = self.question(merge_pr_url=URL)
        task = self.other_task()
        self.service.emit(task['id'], 'native_proposal', dict(key='other', title='Review the changes?', options=decision['options']))
        other = self.service._all('decisions')[-1]
        with self.service.transaction():
            with self.assertRaises(ServiceError):
                self.service._revise_question(other, task,
                    dict(title='Merge the changes?', options=decision['options'], merge_pr_url=URL), 'Use this card.')
        self.assertIsNone(self.service._get('decisions', other['id']).get('merge_pr_url'))
        with self.service.transaction():
            self.service._revise_question(decision, self.service._get('tasks', decision['task_id']),
                dict(title='Merge the reviewed changes?', options=decision['options']), 'Clarify the title.')
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], 2)
