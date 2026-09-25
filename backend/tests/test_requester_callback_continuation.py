"""A requester return may admit its delegate, but only its own foreground is exempt."""
import copy
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut.service import ServiceError
import test_native_work_continuation as fixtures
import test_provider


class RequesterCallbackContinuationTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume

    def callback(self):
        owner, requester = self.scheduled_native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        requester.update(status='completed', _waiting_on=[],
                         _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', requester)
        report = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()),
                      source_task_id=requester['id'], _reply_to_task_id=requester['id'],
                      _kanban_claim={'native_id': 't_report', 'input': 'report'})
        report.pop('_work_todos', None)
        self.service._put('tasks', report)
        self.service.kanban.tasks[report['id']] = dict(id='t_report', status='done')
        # Use the real return route, not a same-execution-ID-only synthetic callback.
        with self.service.transaction():
            delivered = self.service._store_bot_message(report['id'], {
                'target_bot_id': requester['bot_id'], 'text': 'The previous result is saved.'}, returning=True)
            caller = self.service._get('tasks', delivered['task_id'])
            self.service._ensure_execution_session(caller)
        caller.update(status='working', _native_turn_id='callback-turn',
                      _conversation_epoch=requester['_conversation_epoch'],
                      _kanban_claim=dict(native_id='t_callback', external_id=caller['id'],
                                         input=caller['id'], run_id=71, pid=101, context=''))
        self.service._put('tasks', caller)
        self.service.running[caller['id']] = Mock(is_alive=Mock(return_value=True))
        self.service.kanban.tasks[caller['id']] = dict(id='t_callback', status='running',
                                                     current_run_id=71, worker_pid=101)
        update = dict(requester, id=str(uuid4()), _kanban_claim={'native_id': 't_update', 'input': 'report'})
        self.service._put('tasks', update)
        self.service.kanban.tasks[update['id']] = dict(id='t_update', status='done')
        url = 'https://github.com/example/project/pull/1'
        original = dict(id='original-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr',
                        title='Original PR', status='merged', pr_state='merged', url=url,
                        _review_task_id=owner['id'], _activity_task_id=report['id'])
        peer = dict(id='requester-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
                    title='Requester PR', status='merged', pr_state='merged', url=url.upper(),
                    _review_task_id=requester['id'], _activity_task_id=update['id'])
        for item in (original, peer):
            self.service._put('artifacts', item)
        for task in (owner, caller):
            self.assertNotIn('_work_context_id', task)
            self.assertNotIn('_native_outcome_id', task)
        return owner, requester, report, caller, original, peer

    def through_provider(self, caller, *, twice=False):
        self.root = Path(self.temp.name) / str(uuid4())
        payload = dict(task_id='t_existing', message='Keep the verified correction local.')
        behavior = "import json\nresult = json.loads(registry.dispatch('coconut_continue_work', " + repr(payload) + "))\n"
        if twice:
            behavior += "assert json.loads(registry.dispatch('coconut_continue_work', " + repr(payload) + ")) == result\n"
        behavior += "return {'completed': True, 'final_response': json.dumps(result)}\n"
        provider = test_provider.ProviderTests.fake_hermes(self, behavior)
        self.assertTrue(provider.catalog.root.is_relative_to(Path(self.temp.name)))
        events = []
        def emit(kind, data):
            if kind == 'work_continue':
                return self.service.emit(caller['id'], kind, data)
            events.append((kind, data))
        with patch.dict(os.environ, ANTHROPIC_API_KEY='test-only'):
            provider.start_task({k: caller[k] for k in ('id', 'bot_id', 'title', '_native_turn_id')},
                                self.service._get('bots', caller['bot_id']), [], emit)
        text = ''.join(data['text'] for kind, data in events if kind == 'text_delta')
        self.assertTrue(text.startswith('{'), events)
        return json.loads(text)

    def reject(self, owner, caller):
        with self.assertRaises(ServiceError):
            self.resume(caller)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)

    def test_provider_callback_preserves_owner_workspace_results_and_admits_once(self):
        owner, requester, report, caller, original, peer = self.callback()
        owner['_kanban_claim']['workspace'] = str(Path(self.temp.name) / 'retained-owner')
        owner['_saved_result'] = 'Original verified result'
        self.service._put('tasks', owner)
        self.service._put('decisions', dict(id='answered', task_id=owner['id'], status='rejected'))
        before = copy.deepcopy(self.service.kanban.tasks)
        tasks_before = self.service._all('tasks')
        receipt = self.through_provider(caller, twice=True)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', '_kanban_claim', 'source_task_id', 'source_bot_id',
                    '_reply_to_task_id', 'thread_id', '_saved_result'):
            self.assertEqual(queued[key], owner[key])
        self.assertEqual(queued['_work_context_id'], original['id'])
        self.assertEqual(queued['_bot_messages'][:-1], owner['_bot_messages'])
        self.assertEqual(queued['_bot_messages'][-1]['text'], 'Keep the verified correction local.')
        self.assertEqual(queued['_self_recovery']['decisions'][0]['status'], 'rejected')
        self.assertEqual(self.service.kanban.tasks, before)
        self.assertEqual(len(self.service._all('tasks')), len(tasks_before))
        for task in tasks_before:
            if task['id'] != owner['id']:
                self.assertEqual(self.service._get('tasks', task['id']), task)
        self.assertEqual(self.service._get('artifacts', peer['id']), peer)
        self.assertEqual(len([d for d in self.service._all('deliveries')
                             if d['receipt'].get('native_task_id') == 't_existing']), 1)

    def test_valid_reciprocal_continuation_also_qualifies(self):
        owner, requester, report, caller, _, _ = self.callback()
        caller.update(parent_task_id=requester['id'], _is_return=False)
        requester['_continued_task_id'] = caller['id']
        self.service._put('tasks', caller)
        self.service._put('tasks', requester)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_execution_identity_without_return_or_continuation_is_not_authority(self):
        owner, _, _, caller, _, _ = self.callback()
        for changes in ({'_is_return': False}, {'_assignment_task_id': 'unrelated'},
                        {'source_task_id': owner['id']}, {'source_bot_id': 'unrelated'},
                        {'thread_id': 'other'}, {'_native_turn_id': None}):
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(caller, **changes))
                self.reject(owner, caller)
        self.service._put('tasks', caller)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_delivery_and_producer_provenance_are_required(self):
        owner, _, report, caller, _, _ = self.callback()
        delivery = next(d for d in self.service._all('deliveries') if d['receipt']['task_id'] == caller['id'])
        for changes in ({'source_task_id': owner['id']}, {'target_bot_id': 'other'},
                        {'receipt': dict(delivery['receipt'], task_id=report['id'])},
                        {'receipt': dict(delivery['receipt'], thread_id='other')},
                        {'receipt': dict(delivery['receipt'], status='rejected')}):
            with self.subTest(changes=changes):
                self.service._put('deliveries', dict(delivery, **changes))
                self.reject(owner, caller)
        self.service._put('deliveries', delivery)
        self.service._put('tasks', dict(report, _reply_to_task_id='other'))
        self.reject(owner, caller)

    def test_caller_pending_inputs_waits_and_results_still_block(self):
        owner, _, _, caller, _, _ = self.callback()
        for changes in ({'_waiting_on': ['unfinished']}, {'_return_pending': {'text': 'result'}},
                        {'_steering': ['new input']}, {'_live_messages': {'message': 'new input'}},
                        {'_blocked_followup': {'text': 'new input'}},
                        {'_native_reply_pending': {'text': 'result'}}):
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(caller, **changes))
                self.reject(owner, caller)
        self.service._put('tasks', caller)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_provider_returns_actionable_caller_decision_and_context_rejections(self):
        owner, _, _, caller, _, _ = self.callback()
        self.service._put('decisions', dict(id='pending', task_id=caller['id'], status='pending'))
        receipt = self.through_provider(caller)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIn('another execution', receipt['error'])
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.service._put('decisions', dict(id='pending', task_id=caller['id'], status='executed'))
        self.service._put('contexts', dict(id='pending-context', task_id=caller['id'], status='queued'))
        receipt = self.through_provider(caller)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertIn('another execution', receipt['error'])
        self.assertEqual(self.service._get('contexts', 'pending-context')['status'], 'queued')

    def test_current_or_older_unknown_tool_receipts_are_not_foreground_exemptions(self):
        owner, _, _, caller, _, _ = self.callback()
        for turn in ('callback-turn', 'old-turn'):
            for name in ('terminal', 'execute_code', 'coconut_continue_work', 'unknown'):
                with self.subTest(turn=turn, name=name):
                    self.service._put('native_receipts', dict(id='unknown', task_id=caller['id'], turn_id=turn,
                        state='executing', action={'name': name, 'arguments': {}}, _process_ended_at='earlier'))
                    self.reject(owner, caller)

    def test_background_and_work_pause_are_not_foreground_exemptions(self):
        owner, _, _, caller, _, _ = self.callback()
        for method in ('_background_pending', '_task_work_is_paused'):
            original = getattr(self.service, method)
            with self.subTest(method=method), patch.object(self.service, method,
                    side_effect=lambda value, original=original: True if
                    (value.get('id') if isinstance(value, dict) else value) == caller['id'] else original(value)):
                self.reject(owner, caller)

    def test_native_foreground_must_match_the_exact_saved_claim(self):
        owner, _, _, caller, _, _ = self.callback()
        card = self.service.kanban.tasks[caller['id']]
        for changes in ({'id': 'different'}, {'current_run_id': 72}, {'worker_pid': 102},
                        {'status': 'review'}, {'status': 'done'}, {'current_run_id': None},
                        {'claim_source_status': 'review'}):
            with self.subTest(changes=changes):
                self.service.kanban.tasks[caller['id']] = dict(card, **changes)
                self.reject(owner, caller)
        self.service.kanban.tasks[caller['id']] = card
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_active_sibling_and_independent_reviewer_remain_guarded(self):
        owner, requester, report, caller, original, peer = self.callback()
        sibling = dict(caller, id=str(uuid4()), _kanban_claim=dict(caller['_kanban_claim']))
        sibling['_kanban_claim'].update(external_id=sibling['id'], native_id='t_sibling')
        self.service._put('tasks', sibling)
        self.service.kanban.tasks[sibling['id']] = dict(id='t_sibling', status='running', current_run_id=71)
        self.reject(owner, caller)
        sibling.update(status='completed')
        self.service._put('tasks', sibling)
        self.service.kanban.tasks[sibling['id']] = dict(id='t_sibling', status='done')
        self.service._put('artifacts', dict(peer, _review_task_id=caller['id']))
        self.reject(owner, caller)
        self.service._put('artifacts', peer)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_snapshot_retry_revalidates_caller_provenance_and_claim(self):
        for change in ('delivery', 'turn', 'claim', 'decision', 'native'):
            with self.subTest(change=change):
                # Each case uses a fresh, isolated service and the actual receipt path.
                if change != 'delivery':
                    self.tearDown()
                    self.setUp()
                owner, _, _, caller, _, _ = self.callback()
                rows = copy.deepcopy(self.service.kanban.tasks)
                self.service.kanban.tasks = {}
                requested = threading.Event()
                changed = threading.Event()
                errors = []
                def mutate():
                    try:
                        if not requested.wait(5):
                            raise AssertionError('No targeted snapshot')
                        with self.service.transaction():
                            if change == 'delivery':
                                delivery = next(d for d in self.service._all('deliveries')
                                                if d['receipt']['task_id'] == caller['id'])
                                self.service._put('deliveries', dict(delivery, target_bot_id='other'))
                            elif change == 'turn':
                                self.service._put('tasks', dict(caller, _native_turn_id='replacement-turn'))
                            elif change == 'claim':
                                self.service._put('tasks', dict(caller,
                                    _kanban_claim=dict(caller['_kanban_claim'], run_id=72)))
                            elif change == 'decision':
                                self.service._put('decisions', dict(id='race-decision', task_id=caller['id'], status='pending'))
                            else:
                                self.service.kanban.tasks = {caller['id']: dict(rows[caller['id']], current_run_id=72)}
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        changed.set()
                worker = threading.Thread(target=mutate)
                worker.start()
                def snapshot(request):
                    self.assertFalse(self.service.lock._is_owned())
                    requested.set()
                    self.assertTrue(changed.wait(5))
                    return {'tasks': [dict(rows[i], external_id=i) for i in request['external_ids'] if i in rows]}
                self.service.kanban.call.side_effect = snapshot
                try:
                    result = self.through_provider(caller)
                    self.assertEqual(result['status'], 'rejected', result)
                    self.assertEqual(self.service._get('tasks', owner['id']), owner)
                finally:
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
