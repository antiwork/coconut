import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from coconut.service import Service
from test_service_pr import Tracker, observation, URL
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class Provider:
    name = 'pr-review-fixture'
    ready = True
    model = 'fixture'
    def __init__(self): self.starts = []
    def start_task(self, task, bot, history, emit):
        self.starts.append(task)
        if task.get('_pr_review'):
            emit('text_delta', {'text': 'Current checks and reviews pass. Offer a merge decision for ' + URL})
        elif bot['name'] == 'Dev':
            emit('artifact', {'kind': 'pr', 'url': URL})
            emit('text_delta', {'text': 'Checks are running.'})
        elif task.get('_is_return') and 'Offer a merge decision' in str(task.get('_bot_messages')):
            emit('native_question', {'title': 'Merge PR #42?', 'recommended_option_id': 'merge',
                'options': [{'id':'merge','label':'Merge','explanation':'Merge the reviewed changes into main after checking the head.'},
                            {'id':'leave','label':'Leave open','explanation':'Keep the pull request open without merging.'}]})
        emit('completed', {'summary': 'Reported the current result.'})
    def cancel_task(self, task_id): pass


class PRReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.desk = self.write('/v1/bots', {'name':'Desk','role':'Coordinator'})
        self.dev = self.write('/v1/bots', {'name':'Dev','role':'Development'})
        parent = self.write(f'/v1/bots/{self.desk["id"]}/messages', {'text':'Ask Dev to finish PR #42'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status']='working';self.service._put('tasks',task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id':self.dev['id'],'text':'Finish PR #42'})
            task = self.service._get('tasks', parent['id']);task['status']='waiting';self.service._put('tasks',task)
        self.child_id = child['task_id']
        self.service._run_task(self.child_id)
        self.service._drain_returns()
        for task in self.service._all('tasks'):
            if task.get('_is_return'): self.service._run_task(task['id'])
        self.artifact_id = self.service.snapshot()['artifacts'][0]['id']

    def tearDown(self):
        self.service.close();self.temp.cleanup()

    def write(self, path, data): return self.service.mutate('POST', path, data, str(uuid4()), 'test-phone')

    def green(self, **changes):
        result = replace(observation('ready_for_review'), pr_state='open', draft=False, checks_state='passed')
        self.tracker.observations.append(replace(result, **changes))
        self.service.refresh_pull_requests()

    def test_a_review_pass_reads_the_artifacts_once_for_every_pr_of_a_bot(self):
        self.green()
        original = self.service._all
        def count(extra):
            with self.service.transaction():
                artifact = self.service._get('artifacts', self.artifact_id)
                # Finish the review turn that green() queued, so each PR reaches the peer check.
                task = self.service._get('tasks', artifact['_review_task_id'])
                task['status'] = 'completed'
                self.service._put('tasks', task)
                for index in range(extra):
                    self.service._put('artifacts', dict(artifact, id=f'copy-{extra}-{index}', url=f'{URL}-{extra}-{index}',
                                                        _reviewed_states={}, _reviewed_heads=[]))
            reads = []
            def read(table):
                reads.append(table)
                return original(table)
            with patch.object(self.service, '_all', side_effect=read):
                self.service._queue_pr_reviews()
            return reads.count('artifacts')
        # The cost does not grow with the number of pull requests.
        self.assertEqual(count(1), count(6))

    def test_green_wakes_dev_then_desk_offers_one_pending_merge_card(self):
        before = len(self.service._all('messages'))
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
        self.assertEqual(len(self.service._all('messages')),before)
        self.service._run_task(self.child_id)
        self.assertEqual(self.provider.starts[-1]['_pr_review']['url'],URL)
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return') and t['status']=='queued']
        self.assertEqual(len(returns),1)
        self.assertEqual(returns[0]['bot_id'],self.desk['id'])
        self.service._run_task(returns[0]['id'])
        card = self.service.snapshot()['decisions'][0]
        self.assertEqual(card['bot_id'],self.desk['id'])
        self.assertEqual(card['status'],'pending')
        self.assertEqual(card['recommended_option_id'],'merge')
        count = len(self.provider.starts)
        self.green()
        self.assertEqual(len(self.provider.starts),count)
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')

    def test_retry_tracker_keeps_native_owner_and_wakes_it_before_coordinator(self):
        owner = self.service._get('tasks', self.child_id)
        owner.update(_execution_session_id=owner['id'], _native_outcome_id='native-original')
        self.service._put('tasks', owner)
        retry = dict(owner, id=str(uuid4()), parent_task_id=owner['id'],
                     source_bot_id=None, source_task_id=None, exchange_depth=0,
                     exchange_id=self.service._new_exchange())
        retry.pop('_native_outcome_id')
        self.service._put('tasks', retry)
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact['_review_task_id'] = retry['id']
        self.service._put('artifacts', artifact)
        coordinator = self.service._get('tasks', owner['source_task_id'])
        peer = dict(artifact, id=str(uuid4()), bot_id=self.desk['id'],
                    task_id=coordinator['id'], _review_task_id=coordinator['id'],
                    created_at='2000-01-01T00:00:00Z')
        self.service._put('artifacts', peer)
        self.green()
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
        self.assertNotEqual(self.service._get('tasks', coordinator['id'])['status'], 'queued')
        self.assertEqual(self.service._pr_review_source(self.dev['id'], retry['id'])['id'], owner['id'])
        from unittest.mock import patch
        tasks = {t['id']: t for t in self.service._all('tasks')}
        with patch.object(self.service, '_all', side_effect=AssertionError('Repeated task scan')):
            self.assertEqual(self.service._pr_review_source(self.dev['id'], retry['id'], tasks=tasks)['id'], owner['id'])

    def test_verified_owner_offers_card_without_coordinator_turn(self):
        from coconut.pull_request_reviews import PR_MERGE_INSTRUCTIONS
        self.assertIn('Offer one coconut_ask merge decision yourself', PR_MERGE_INSTRUCTIONS)
        # The bot's skills say how to verify the PR, for example local checks while CI runs.
        self.assertIn('as your skills for pull request review and merge say', PR_MERGE_INSTRUCTIONS)
        revision = '2026-09-23T11:46:31Z'
        self.tracker.observe_issue = lambda url: dict(updated_at=revision)
        original_start = self.provider.start_task
        def start(task, bot, history, emit):
            if not task.get('_pr_review'):
                return original_start(task, bot, history, emit)
            emit('native_question', {'title': 'Merge PR #42?', 'merge_pr_url': URL, 'risk_level': 'low', 'risk': 'A bad merge reaches main.', 'source_revisions': {URL: revision},
                'recommended_option_id': 'merge',
                'options': [{'id': 'merge', 'label': 'Merge',
                             'explanation': 'Merge the verified changes into main.'},
                            {'id': 'leave', 'label': 'Leave open',
                             'explanation': 'Keep the pull request open.'}]})
            emit('completed', {'summary': 'Waiting for the merge decision.'})
        self.provider.start_task = start
        self.green()
        before = {task['id'] for task in self.service._all('tasks')}
        self.service._run_task(self.child_id)
        cards = self.service.snapshot()['decisions']
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['task_id'], self.child_id)
        self.assertEqual(cards[0]['status'], 'pending')
        self.assertEqual(cards[0]['merge_pr_url'], URL)
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'needs_you')
        self.assertEqual({task['id'] for task in self.service._all('tasks')}, before)
        self.green()
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)

    def test_external_merge_resumes_original_owner_once(self):
        from coconut.hermes_worker import conversation_input
        self.green(pr_state='merged', checks_state='unknown')
        task = self.service._get('tasks', self.child_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_pr_review']['pr_state'], 'merged')
        prompt, _ = conversation_input({'operation': 'start', 'task': task, 'history': []})
        self.assertIn('Verify whether the accepted outcome is complete', prompt)
        self.assertIn('Do not merge, reopen, publish, or deploy', prompt)
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_external_close_waits_for_active_owner(self):
        task = self.service._get('tasks', self.child_id)
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.green(pr_state='closed', checks_state='unknown')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'working')
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service._queue_pr_reviews()
        task = self.service._get('tasks', self.child_id)
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(task['_pr_review']['pr_state'], 'closed')

    def test_external_close_preserves_cancelled_work(self):
        self.write('/v1/work/items/' + self.artifact_id + '/cancel', {})
        self.green(pr_state='closed', checks_state='unknown')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'cancelled')

    def test_terminal_observation_keeps_completed_native_outcome_closed(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        native = SimpleNamespace(tasks={self.child_id: {'id': 't_finished', 'status': 'done'}})
        self.green(pr_state='merged', checks_state='unknown')
        task = self.service._get('tasks', self.child_id)
        task['status'] = 'completed'
        self.service._put('tasks', task)
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact['_terminal_review_pending'] = True
        artifact.pop('_reviewed_states', None)
        self.service._put('artifacts', artifact)
        with patch.object(self.service, 'kanban', native):
            self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_terminal_observation_waits_for_its_native_snapshot(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        task = self.service._get('tasks', self.child_id)
        task['_native_outcome_id'] = 't_original'
        self.service._put('tasks', task)
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(pr_state='merged', _terminal_review_pending=True)
        self.service._put('artifacts', artifact)
        for rows in ({}, {self.child_id: {'id': 't_other', 'status': 'scheduled'}}):
            with self.subTest(rows=rows), patch.object(self.service, 'kanban', SimpleNamespace(tasks=rows)):
                self.service._queue_pr_reviews()
                self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')
        native = SimpleNamespace(tasks={self.child_id: {'id': 't_original', 'status': 'scheduled'}})
        with patch.object(self.service, 'kanban', native):
            self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')

    def test_terminal_observation_preserves_human_pause(self):
        from unittest.mock import patch
        with patch.object(self.service, '_work_pause', return_value={'id': 'human-stop'}):
            self.green(pr_state='merged', checks_state='unknown')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')
        self.assertTrue(self.service._get('artifacts', self.artifact_id)['_terminal_review_pending'])

    def test_retained_closed_pr_does_not_reopen_history(self):
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(pr_state='merged', checks_state='passed')
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_restart_does_not_duplicate_queued_review(self):
        self.green()
        turn=self.service._get('tasks',self.child_id)['_native_turn_id']
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['_native_turn_id'],turn)

    def test_failed_observation_never_wakes(self):
        self.green(observed=False,detail='No access')
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')

    def test_verified_draft_readiness_wakes_owner_and_produces_a_merge_card(self):
        self.green(draft=True)
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(_activity_state='ready', _activity_head=artifact['head_sha'])
        self.service._put('artifacts', artifact)
        self.service._queue_pr_reviews()
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'queued')
        self.service._run_task(self.child_id)
        self.assertTrue(self.provider.starts[-1]['_pr_review']['draft'])
        self.service._drain_returns()
        for task in self.service._all('tasks'):
            if task.get('_is_return') and task['status'] == 'queued':
                self.service._run_task(task['id'])
        self.assertEqual(self.service.snapshot()['decisions'][0]['status'], 'pending')
        self.service._queue_pr_reviews()
        self.assertEqual(len(self.service.snapshot()['decisions']), 1)

    def test_pending_decision_defers_review(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='needs_you';self.service._put('tasks',task)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'needs_you')
        self.assertNotIn('_reviewed_head',self.service._get('artifacts',self.artifact_id))

    def test_cancelled_or_reset_conversation_never_wakes(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='cancelled';self.service._put('tasks',task)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'cancelled')

    def test_review_binding_rejects_another_bots_task(self):
        from coconut.service import ServiceError
        desk_task=next(t for t in self.service._all('tasks') if t['bot_id']==self.desk['id'])
        with self.assertRaises(ServiceError):
            self.write('/v1/work/pull-requests', {'bot_id':self.dev['id'],'url':URL,'review_task_id':desk_task['id']})


    def test_waiting_dev_cannot_lose_review_to_desk(self):
        parent=next(t for t in self.service._all('tasks') if t['bot_id']==self.desk['id'] and not t.get('_is_return'))
        self.tracker.observations.append(replace(observation(),pr_state='open',draft=False,checks_state='running'))
        self.write('/v1/work/pull-requests',{'bot_id':self.desk['id'],'url':URL,'review_task_id':parent['id']})
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='needs_you';self.service._put('tasks',task)
        result=replace(observation('ready_for_review'),pr_state='open',draft=False,checks_state='passed')
        self.tracker.observations.extend([result,result]);self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks',parent['id'])['status'],'completed')
        self.assertFalse(any(a.get('_reviewed_head') for a in self.service._all('artifacts')))

    def test_failed_assignment_is_restored_before_continuation(self):
        with self.service.transaction():
            child=self.service._get('tasks',self.child_id)
            parent=dict(child,id=str(uuid4()),status='failed',_result_task_id=child['id'])
            self.service._put('tasks',parent)
            child['_assignment_task_id']=parent['id'];self.service._put('tasks',child)
        self.green()
        self.assertEqual(self.service._get('tasks',parent['id'])['status'],'waiting')
        self.service._run_task(self.child_id)
        self.assertEqual(self.provider.starts[-1]['_pr_review']['url'],URL)

    def test_native_prompt_keeps_verification_and_approval_boundaries(self):
        from coconut.hermes_worker import conversation_input
        prompt,_=conversation_input({'operation':'start','task':{'title':'Check PR','_pr_review':{'url':URL,'head_sha':'a'*40}},'history':[]})
        self.assertIn('not permission to merge',prompt)
        self.assertIn('required checks',prompt)
        from coconut.pull_request_reviews import PR_MERGE_INSTRUCTIONS
        self.assertIn('requesting bot', PR_MERGE_INSTRUCTIONS)
        self.assertIn(URL,prompt)


    def test_upgrade_binds_legacy_artifact_to_original_task(self):
        with self.service.transaction():
            artifact=self.service._get('artifacts',self.artifact_id)
            artifact.pop('_review_task_id');self.service._put('artifacts',artifact)
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')

    def test_routine_registration_keeps_original_return_address(self):
        later=self.write(f'/v1/bots/{self.dev["id"]}/messages',{'text':'Check the same PR in this conversation'})
        self.service._run_task(later['id'])
        self.assertEqual(self.service._get('artifacts',self.artifact_id)['_review_task_id'],self.child_id)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
        self.assertEqual(self.service._get('tasks',later['id'])['status'],'completed')

    def start_correction(self):
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages', {'text': 'Continue the approved PR corrections'})
        task = self.service._get('tasks', sent['id'])
        task.update(status='working', _execution_session_id=task['id'],
                    _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        return task

    def report_correction(self, task):
        return self.service._register_pr_artifact(task['id'], dict(url=URL,
            activity='Testing the corrections', activity_state='working'))

    def test_correction_owns_work_and_check_updates_after_completed_review(self):
        task = self.start_correction()
        self.report_correction(task)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], task['id'])
        import threading
        self.service.running[task['id']] = threading.current_thread()
        try:
            row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
            self.assertEqual(row['work_state'], 'working')
            self.assertEqual(row['activity'], 'Testing the corrections')
        finally:
            self.service.running.clear()
        task.update(status='completed')
        self.service._put('tasks', task)
        self.green()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', self.child_id)['status'], 'completed')

    def test_progress_uses_the_native_execution_instead_of_a_short_continuation(self):
        task = self.start_correction()
        continuation = dict(task, id=str(uuid4()), parent_task_id=task['id'])
        self.service._put('tasks', continuation)
        self.report_correction(continuation)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], task['id'])

    def test_work_reply_reuses_the_current_pr_execution(self):
        task = self.start_correction()
        self.report_correction(task)
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages',
            dict(text='Continue the existing fix', mode='queue', work_artifact_id=self.artifact_id))
        continuation = self.service._get('tasks', sent['id'])
        self.service._ensure_execution_session(continuation)
        self.assertEqual(continuation['_execution_session_id'], task['_execution_session_id'])

    def test_work_reply_to_delegated_pr_keeps_execution_and_return_address(self):
        owner = self.service._get('tasks', self.child_id)
        self.service._ensure_execution_session(owner)
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages',
            dict(text='Continue the existing fix', mode='queue', work_artifact_id=self.artifact_id))
        task = self.service._get('tasks', sent['id'])
        task.update(status='working', _conversation_epoch=owner['_conversation_epoch'])
        self.service._put('tasks', task)
        self.assertNotEqual(task['conversation_bot_id'], owner['conversation_bot_id'])
        self.report_correction(task)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])

    def test_queued_work_reply_prevents_takeover_before_its_first_turn(self):
        owner = self.service._get('tasks', self.child_id)
        self.service._ensure_execution_session(owner)
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages',
            dict(text='Continue the existing fix', mode='queue', work_artifact_id=self.artifact_id))
        self.report_correction(self.start_correction())
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertIn(sent['id'], [t['id'] for t in row['work_attempts']])

    def test_finished_continuation_does_not_restore_an_older_failure(self):
        owner = self.service._get('tasks', self.child_id)
        self.service._ensure_execution_session(owner)
        failed = dict(owner, id=str(uuid4()), status='failed', parent_task_id=owner['id'])
        self.service._put('tasks', failed)
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages',
            dict(text='Continue the existing fix', mode='queue', work_artifact_id=self.artifact_id))
        task = self.service._get('tasks', sent['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service._register_pr_artifact(task['id'], dict(url=URL, activity='Waiting for checks', activity_state='waiting'))
        task['status'] = 'completed'
        self.service._put('tasks', task)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertEqual(row['work_state'], 'waiting')
        self.assertNotIn(failed['id'], [t['id'] for t in row['work_attempts']])

    def test_completed_work_reply_keeps_its_proposal_and_approved_work(self):
        owner = self.service._get('tasks', self.child_id)
        self.service._ensure_execution_session(owner)
        sent = self.write(f'/v1/bots/{self.dev["id"]}/messages',
            dict(text='Review the remaining tradeoff', mode='queue', work_artifact_id=self.artifact_id))
        task = self.service._get('tasks', sent['id'])
        task['status'] = 'working'
        self.service._put('tasks', task)
        self.service.emit(task['id'], 'native_proposal', dict(key='review-tradeoff',
            title='Change the retry policy?', explanation='The current policy retries three times.',
            options=[dict(id='change', label='Change the policy'), dict(id='keep', label='Keep the policy')]))
        task['status'] = 'completed'
        self.service._put('tasks', task)
        decision = self.service.snapshot()['decisions'][-1]
        other = self.start_correction()
        self.report_correction(other)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertEqual(row['work_decision_id'], decision['id'])
        self.write(f'/v1/decisions/{decision["id"]}/answer', dict(option_id='change', revision=1))
        execution = self.service._get('decisions', decision['id'])['_execution_task_id']
        self.report_correction(other)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertIn(execution, [t['id'] for t in row['work_attempts']])

    def test_completed_resumed_pr_does_not_restore_its_cancelled_predecessor(self):
        owner = self.service._get('tasks', self.child_id)
        self.service._ensure_execution_session(owner)
        self.write('/v1/work/items/' + self.artifact_id + '/cancel', {})
        self.write('/v1/work/items/' + self.artifact_id + '/resume', {})
        artifact = self.service._get('artifacts', self.artifact_id)
        task = self.service._get('tasks', artifact['_review_task_id'])
        task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        self.service._register_pr_artifact(task['id'], dict(url=URL, activity='Waiting for checks', activity_state='waiting'))
        task['status'] = 'completed'
        self.service._put('tasks', task)
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertEqual(row['work_state'], 'waiting')
        self.assertEqual(row['activity'], 'Review complete')
        self.assertNotIn(owner['id'], [t['id'] for t in row['work_attempts']])

    def test_correction_keeps_ownership_when_coordinator_also_tracks_the_pr(self):
        parent = next(t for t in self.service._all('tasks') if t['bot_id'] == self.desk['id'] and not t.get('_is_return'))
        self.tracker.observations.append(replace(observation(), pr_state='open', checks_state='running'))
        peer = self.write('/v1/work/pull-requests', dict(bot_id=self.desk['id'], url=URL, review_task_id=parent['id']))
        saved = self.service._get('artifacts', peer['id'])
        saved['created_at'] = '2000-01-01T00:00:00Z'
        self.service._put('artifacts', saved)
        task = self.start_correction()
        self.report_correction(task)
        self.assertEqual(self.service._pr_review_owner(self.service._all('artifacts'))['_review_task_id'], task['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        result = replace(observation('ready_for_review'), pr_state='open', draft=False, checks_state='passed')
        self.tracker.observations.extend([result, result])
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', parent['id'])['status'], 'completed')

    def test_new_progress_cannot_replace_an_existing_wait_or_decision(self):
        task = self.start_correction()
        owner = self.service._get('tasks', self.child_id)
        for status in ('waiting', 'needs_you', 'working', 'queued'):
            with self.subTest(status=status):
                owner['status'] = status
                self.service._put('tasks', owner)
                self.report_correction(task)
                self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])

    def test_status_callback_does_not_take_ownership_of_completed_work(self):
        task = self.start_correction()
        task['_is_return'] = True
        self.service._put('tasks', task)
        self.report_correction(task)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], self.child_id)

    def test_readiness_report_keeps_completed_native_owner_for_check_updates(self):
        task = self.start_correction()
        owner = self.service._get('tasks', self.child_id)
        owner['_native_outcome_id'] = 'native-original'
        self.service._put('tasks', owner)
        self.report_correction(task)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.green()
        self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
        self.assertEqual(self.service._get('tasks', task['id'])['status'], 'completed')

    def test_coordinator_receives_current_checks_when_a_result_waits_in_queue(self):
        self.green()
        self.service._run_task(self.child_id)
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_is_return') and t['status'] == 'queued')
        artifact = self.service._get('artifacts', self.artifact_id)
        artifact.update(checks_state='failed', required_checks_state='failed', checked_at='2099-01-01T00:00:00Z')
        self.service._put('artifacts', artifact)
        self.service._run_task(callback['id'])
        current = self.provider.starts[-1]['_tracked_pr_observations']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['url'], URL)
        self.assertEqual(current[0]['head_sha'], artifact['head_sha'])
        self.assertEqual(current[0]['checks_state'], 'failed')
        self.assertEqual(current[0]['checked_at'], '2099-01-01T00:00:00Z')

    def test_separate_reporter_callback_retains_pr_context_after_newer_reports(self):
        owner = self.service._get('tasks', self.child_id)
        owner['_native_outcome_id'] = 'native-original'
        self.service._put('tasks', owner)
        parent = self.service._get('tasks', owner['source_task_id'])
        parent['status'] = 'working'
        self.service._put('tasks', parent)
        sent = self.service._store_bot_message(parent['id'], dict(target_bot_id=self.dev['id'], text='Verify readiness'))
        reporter = self.service._get('tasks', sent['task_id'])
        reporter.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', reporter)
        self.report_correction(reporter)
        reporter = self.service._get('tasks', reporter['id'])
        self.service._prepare_return(reporter, 'The reviewed head is ready.', 'completed')
        reporter['status'] = 'completed'
        self.service._put('tasks', reporter)
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_is_return') and t['status'] == 'queued')
        newer = self.start_correction()
        self.report_correction(newer)
        artifact = self.service._get('artifacts', self.artifact_id)
        self.assertEqual(artifact['_review_task_id'], owner['id'])
        self.assertEqual(artifact['_activity_task_id'], newer['id'])
        artifact.update(checks_state='failed', required_checks_state='failed', checked_at='2099-01-01T00:00:00Z')
        self.service._put('artifacts', artifact)
        self.service._run_task(callback['id'])
        self.assertEqual(self.provider.starts[-1]['id'], callback['id'])
        current = self.provider.starts[-1]['_tracked_pr_observations']
        self.assertEqual([item['url'] for item in current], [URL])
        self.assertEqual(current[0]['checks_state'], 'failed')

    def test_latest_refresh_failure_reaches_callback_without_unrelated_prs(self):
        self.green()
        self.service._run_task(self.child_id)
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_is_return') and t['status'] == 'queued')
        artifact = self.service._get('artifacts', self.artifact_id)
        failed = dict(artifact, id=str(uuid4()), checked_at='2099-01-01T00:00:00Z',
                      refresh_error='GitHub is unreachable.')
        self.service._put('artifacts', failed)
        other = self.write(f'/v1/bots/{self.dev["id"]}/messages', {'text': 'A separate PR'})
        unrelated = dict(failed, id=str(uuid4()), url=URL + '0', task_id=other['id'], _review_task_id=other['id'])
        self.service._put('artifacts', unrelated)
        self.service._run_task(callback['id'])
        current = self.provider.starts[-1]['_tracked_pr_observations']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['url'], URL)
        self.assertEqual(current[0]['refresh_error'], 'GitHub is unreachable.')

    def test_native_continuation_keeps_work_active_and_prevents_takeover(self):
        task = self.start_correction()
        owner = self.service._get('tasks', self.child_id)
        owner['_execution_session_id'] = owner['id']
        self.service._put('tasks', owner)
        continuation = dict(owner, id=str(uuid4()), status='queued',
                            exchange_id=self.service._new_exchange(), parent_task_id=owner['id'])
        self.service._put('tasks', continuation)
        self.report_correction(task)
        self.assertEqual(self.service._get('artifacts', self.artifact_id)['_review_task_id'], owner['id'])
        row = next(a for a in self.service.snapshot()['artifacts'] if a['id'] == self.artifact_id)
        self.assertIn(continuation['id'], [t['id'] for t in row['work_attempts']])
        self.assertEqual(row['work_state'], 'queued')

    def test_return_to_earlier_head_does_not_repeat_review(self):
        for head in ('a' * 40, 'b' * 40):
            self.green(head_sha=head)
            self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
            self.service._run_task(self.child_id)
            self.service._drain_returns()
            for task in self.service._all('tasks'):
                if task.get('_is_return') and task['status']=='queued':
                    self.service._run_task(task['id'])
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green(head_sha='a' * 40)
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')
        artifact=self.service._get('artifacts',self.artifact_id)
        self.assertEqual(set(artifact['_reviewed_heads']),{'a' * 40,'b' * 40})

    def test_cancelled_owner_does_not_block_new_assignment(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id)
            task['status']='cancelled';self.service._put('tasks',task)
        self.tracker.observations.append(replace(observation(),pr_state='open',draft=False,checks_state='running'))
        worker=self.write('/v1/bots',{'name':'Dev','role':'Development'})
        request=self.write(f'/v1/bots/{worker["id"]}/messages',{'text':'Finish PR #42'})
        self.service._run_task(request['id'])
        result=replace(observation('ready_for_review'),pr_state='open',draft=False,checks_state='passed')
        self.tracker.observations.extend([result,result]);self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks',request['id'])['status'],'queued')
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'cancelled')

    def test_registration_replaces_cancelled_source_for_same_bot(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id)
            task['status']='cancelled';self.service._put('tasks',task)
        request=self.write(f'/v1/bots/{self.dev["id"]}/messages',{'text':'Finish PR #42'})
        self.service._run_task(request['id'])
        self.assertEqual(self.service._get('artifacts',self.artifact_id)['_review_task_id'],request['id'])
        self.green()
        self.assertEqual(self.service._get('tasks',request['id'])['status'],'queued')

    def test_recorded_merge_answer_does_not_repeat_initial_review_request(self):
        from coconut.hermes_worker import conversation_input
        prompt,_=conversation_input({'operation':'resume','task':{'title':'Check PR','_pr_review':{'url':URL,'head_sha':'a'*40}},
            'decision':{'action_type':'question','status':'answered','answer':'merge'},'history':[]})
        self.assertIn('using this recorded answer',prompt)
        self.assertNotIn('Do not merge now',prompt)
        self.assertNotIn('offer that decision yourself',prompt)
