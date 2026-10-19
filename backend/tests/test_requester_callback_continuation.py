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


class RequesterRootCallbackContinuationTests(unittest.TestCase):
    """The owner's source is a return callback of the requester's root, as in Desk #3748.

    Its same-PR activity reporter and the calling callback both execute as that root.
    """
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume
    through_provider = RequesterCallbackContinuationTests.through_provider
    reject = RequesterCallbackContinuationTests.reject

    def returned(self, owner, root, name, *, callback_owner=None, status='completed'):
        # A helper that the root delegated returns through the real delivery route.
        producer = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()), status=status,
                        source_task_id=root['id'], parent_task_id=root['id'], _reply_to_task_id=root['id'],
                        _native_relay=dict(envelope_id=uuid4().hex, sender_bot_id=root['bot_id']),
                        _kanban_claim={'native_id': 't_' + name + '_helper', 'input': name})
        for key in ('_work_todos', '_requester_reply_task_id'):
            producer.pop(key, None)
        if callback_owner:
            producer['_requester_reply_task_id'] = callback_owner
        self.service._put('tasks', producer)
        self.service.kanban.tasks[producer['id']] = dict(id='t_' + name + '_helper', status='done')
        with self.service.transaction():
            delivered = self.service._store_bot_message(producer['id'], {
                'target_bot_id': root['bot_id'], 'text': name + ' result'}, returning=True)
            callback = self.service._get('tasks', delivered['task_id'])
            self.service._ensure_execution_session(callback)
        callback.update(status='completed', _native_turn_id=name + '-turn',
                        _conversation_epoch=root['_conversation_epoch'],
                        _kanban_claim=dict(native_id='t_' + name, external_id=callback['id'],
                                           input=callback['id'], run_id=0, pid=0, context=''))
        self.service._put('tasks', callback)
        self.service.kanban.tasks[callback['id']] = dict(id='t_' + name, status='done')
        return producer, callback

    def run_as_caller(self, callback):
        callback.update(status='working', _kanban_claim=dict(callback['_kanban_claim'], run_id=71, pid=101))
        self.service._put('tasks', callback)
        self.service.running[callback['id']] = Mock(is_alive=Mock(return_value=True))
        self.service.kanban.tasks[callback['id']] = dict(id=callback['_kanban_claim']['native_id'], status='running',
                                                         current_run_id=71, worker_pid=101)
        return callback

    def incident(self):
        owner, root = self.scheduled_native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        root.update(status='completed', _waiting_on=[], _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', root)
        _, source = self.returned(owner, root, 'source')
        # The source callback relayed this owner. The owner's result goes to the root.
        owner.update(source_task_id=source['id'], parent_task_id=source['id'], _reply_to_task_id=root['id'],
                     _requester_reply_task_id=source['id'])
        self.service._put('tasks', owner)
        _, reporter = self.returned(owner, root, 'reporter', callback_owner=source['id'])
        helper, caller = self.returned(owner, root, 'caller', callback_owner=source['id'])
        self.run_as_caller(caller)
        url = 'https://github.com/example/project/pull/1'
        original = dict(id='original-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr',
                        title='Original PR', status='merged', pr_state='merged', url=url,
                        _review_task_id=owner['id'], _activity_task_id=owner['id'])
        peer = dict(id='requester-pr', task_id=source['id'], bot_id=root['bot_id'], kind='pr',
                    title='Requester PR', status='merged', pr_state='merged', url=url.upper(),
                    _review_task_id=source['id'], _activity_task_id=reporter['id'])
        for item in (original, peer):
            self.service._put('artifacts', item)
        for task in (source, reporter, caller):
            self.assertTrue(task['_is_return'])
            self.assertEqual(task['_execution_session_id'], root['id'])
            self.assertEqual(task['_assignment_task_id'], root['id'])
        for task in (reporter, caller):
            self.assertEqual(task['_relay_callback_owner'], source['id'])
        self.assertNotIn('_native_outcome_id', owner)
        return owner, root, source, reporter, helper, caller, original, peer

    def test_provider_root_callback_admits_once_preserving_owner_route_and_workspace(self):
        owner, root, source, reporter, helper, caller, original, peer = self.incident()
        owner['_kanban_claim']['workspace'] = str(Path(self.temp.name) / 'retained-owner')
        self.service._put('tasks', owner)
        self.service._put('decisions', dict(id='answered', task_id=owner['id'], status='rejected'))
        before = copy.deepcopy(self.service.kanban.tasks)
        tasks_before = self.service._all('tasks')
        receipt = self.through_provider(caller, twice=True)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', '_kanban_claim', 'source_task_id', 'source_bot_id',
                    '_reply_to_task_id', '_requester_reply_task_id', 'thread_id'):
            self.assertEqual(queued[key], owner[key], key)
        self.assertEqual(queued['_reply_to_task_id'], root['id'])
        self.assertEqual(queued['_work_context_id'], original['id'])
        self.assertEqual(queued['_self_recovery']['decisions'][0]['status'], 'rejected')
        self.assertEqual(self.service.kanban.tasks, before)
        self.assertEqual(len(self.service._all('tasks')), len(tasks_before))
        for task in tasks_before:
            if task['id'] != owner['id']:
                self.assertEqual(self.service._get('tasks', task['id']), task)
        self.assertEqual(self.service._get('artifacts', peer['id']), peer)
        self.assertEqual(len([d for d in self.service._all('deliveries')
                             if d['receipt'].get('native_task_id') == 't_existing']), 1)

    def test_live_helper_and_other_live_root_branches_remain_guarded(self):
        owner, root, source, reporter, helper, caller, _, _ = self.incident()
        # The helper whose result the caller consumes is still running, like Care's repair.
        self.service._put('tasks', dict(helper, status='working'))
        self.service.kanban.tasks[helper['id']] = dict(id='t_caller_helper', status='running', current_run_id=81)
        self.reject(owner, caller)
        self.service._put('tasks', helper)
        self.service.kanban.tasks[helper['id']] = dict(id='t_caller_helper', status='done')
        # An independent writer that the root delegated outside the source callback's branch.
        independent, _ = self.returned(owner, root, 'independent')
        self.service._put('tasks', dict(independent, status='working'))
        self.service.kanban.tasks[independent['id']] = dict(id='t_independent_helper', status='running', current_run_id=82)
        self.reject(owner, caller)
        self.service._put('tasks', independent)
        self.service.kanban.tasks[independent['id']] = dict(id='t_independent_helper', status='done')
        # A sibling callback of the root that runs beside the caller.
        _, sibling = self.returned(owner, root, 'sibling')
        self.run_as_caller(sibling)
        self.reject(owner, caller)
        self.service._put('tasks', dict(sibling, status='completed'))
        self.service.running.pop(sibling['id'])
        self.service.kanban.tasks[sibling['id']] = dict(id='t_sibling', status='done')
        for changes in ({'_waiting_on': [helper['id']]}, {'_return_pending': {'text': 'result'}}):
            with self.subTest(root=changes):
                self.service._put('tasks', dict(self.service._get('tasks', root['id']), **changes))
                self.reject(owner, caller)
        self.service._put('tasks', root)
        for identity in (root['id'], reporter['id'], caller['id']):
            with self.subTest(decision=identity):
                self.service._put('decisions', dict(id='pending', task_id=identity, status='pending'))
                self.reject(owner, caller)
                self.assertEqual(self.service._get('decisions', 'pending')['status'], 'pending')
        self.service._put('decisions', dict(id='pending', task_id=caller['id'], status='executed'))
        self.service._put('native_receipts', dict(id='unknown', task_id=reporter['id'], turn_id='reporter-turn',
            state='executing', action={'name': 'unknown', 'arguments': {}}))
        self.reject(owner, caller)
        self.service._put('native_receipts', dict(id='unknown', task_id=reporter['id'], turn_id='reporter-turn',
            state='completed', action={'name': 'unknown', 'arguments': {}}))
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_provider_source_callback_live_signals_remain_guarded(self):
        # The source callback is an earlier turn of the root, not the caller. Only the caller is exempt.
        def task(**changes):
            return lambda s, source, helper: s.service._put('tasks', dict(source, **{
                k: ([helper['id']] if v == 'helper' else v) for k, v in changes.items()}))
        def card(**changes):
            return lambda s, source, helper: s.service.kanban.tasks.__setitem__(
                source['id'], dict(s.service.kanban.tasks[source['id']], **changes))
        def row(table, **values):
            return lambda s, source, helper: s.service._put(table, dict(values, task_id=source['id']))
        cases = {
            'wait': task(_waiting_on='helper'), 'return': task(_return_pending={'text': 'result'}),
            'working': task(status='working'), 'queued': task(status='queued'),
            'decision': row('decisions', id='source-decision', status='pending'),
            'context': row('contexts', id='source-context', status='queued'),
            'unknown receipt': row('native_receipts', id='source-unknown', turn_id='source-turn', state='executing',
                                   action={'name': 'unknown', 'arguments': {}}),
            'running command': row('native_receipts', id='source-command', turn_id='source-turn', state='executing',
                                   action={'name': 'terminal', 'arguments': {}}),
            'background wait': row('native_receipts', id='source-wait', turn_id='source-turn', state='completed',
                                   action={'name': 'process', 'arguments': {}}, _wait_process_receipt='source-process'),
            'native ready': card(status='ready', block_kind='dependency', current_run_id=None, worker_pid=None),
            'native claim': card(status='running', current_run_id=91, worker_pid=191),
            'native input block': card(status='blocked', block_kind='needs_input'),
            'rebound native': card(id='t_other'),
            'live worker': lambda s, source, helper: s.service.running.__setitem__(
                source['id'], Mock(is_alive=Mock(return_value=True))),
        }
        for name, apply in cases.items():
            with self.subTest(source=name):
                self.tearDown()
                self.setUp()
                owner, root, source, reporter, helper, caller, _, _ = self.incident()
                apply(self, source, helper)
                receipt = self.through_provider(caller)
                self.assertEqual(receipt['status'], 'rejected', receipt)
                self.assertIn('another execution', receipt['error'])
                self.assertEqual(self.service._get('tasks', owner['id']), owner)
                for decision in self.service._all('decisions'):
                    self.assertEqual(decision['status'], 'pending')
        # The same source admits once it is quiet: answered decision, ended context and delivered wait.
        self.tearDown()
        self.setUp()
        owner, root, source, reporter, helper, caller, _, _ = self.incident()
        self.service._put('decisions', dict(id='source-decision', task_id=source['id'], status='executed'))
        self.service._put('contexts', dict(id='source-context', task_id=source['id'], status='completed'))
        self.service._put('native_receipts', dict(id='source-wait', task_id=source['id'], turn_id='source-turn',
            state='completed', action={'name': 'process', 'arguments': {}}, _wait_process_receipt='source-process',
            _process_delivery='delivered'))
        self.service._put('native_receipts', dict(id='source-unknown', task_id=source['id'], turn_id='source-turn',
            state='completed', action={'name': 'unknown', 'arguments': {}}))
        receipt = self.through_provider(caller, twice=True)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(self.service._get('tasks', source['id']), source)

    def test_ready_native_callback_with_completed_input_waits_for_the_settle_pass(self):
        from datetime import datetime, timedelta, timezone
        owner, root, source, reporter, helper, caller, _, _ = self.incident()
        # A completed callback blocked on a native dependency. Hermes promoted it to READY
        # after the dependency finished, but Coconut has no input left to dispatch on it.
        _, stale = self.returned(owner, root, 'stale', callback_owner=source['id'])
        finished = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat().replace('+00:00', 'Z')
        stale['updated_at'] = finished
        self.service._put('tasks', stale)
        self.service.kanban.tasks[stale['id']] = dict(id='t_stale', status='ready', block_kind='dependency',
                                                      current_run_id=None, worker_pid=None, dependencies=[])
        self.reject(owner, caller)
        settled = []
        def call(request):
            if request['op'] == 'archive_settled':
                settled.append(request)
                self.service.kanban.tasks[stale['id']] = dict(id='t_stale', status='done')
                return {'ok': True}
            return self.snapshot_result()
        self.service.kanban.call.side_effect = call
        self.service._settle_native_orphans()
        self.assertEqual(settled, [dict(op='archive_settled', id='t_stale', summary=stale['summary'])])
        self.assertEqual(self.service._get('tasks', stale['id']), stale)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_root_needs_verified_callback_provenance(self):
        owner, root, source, reporter, helper, caller, _, _ = self.incident()
        delivery = next(d for d in self.service._all('deliveries') if d['receipt']['task_id'] == source['id'])
        for table, row, changes in (
                ('tasks', source, {'_is_return': False}),
                ('tasks', source, {'_assignment_task_id': reporter['id']}),
                ('tasks', source, {'source_bot_id': 'unrelated'}),
                ('tasks', source, {'session_id': 'other'}),
                ('tasks', owner, {'_reply_to_task_id': source['id']}),
                ('deliveries', delivery, {'target_bot_id': 'other'}),
                ('deliveries', delivery, {'receipt': dict(delivery['receipt'], task_id=reporter['id'])}),
                ('deliveries', delivery, {'receipt': dict(delivery['receipt'], status='rejected')})):
            with self.subTest(table=table, changes=changes):
                self.service._put(table, dict(row, **changes))
                self.reject(dict(owner, **changes) if row is owner else owner, caller)
                self.service._put(table, row)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])

    def test_root_callback_does_not_grant_another_bot_or_execution(self):
        owner, root, source, reporter, helper, caller, _, _ = self.incident()
        stranger = self.write('/v1/bots', {'name': 'Unrelated'})
        for changes in ({'bot_id': stranger['id']}, {'_execution_session_id': str(uuid4())},
                        {'_assignment_task_id': source['id']}):
            with self.subTest(changes=changes):
                self.service._put('tasks', dict(caller, **changes))
                self.reject(owner, caller)
        self.service._put('tasks', caller)
        self.assertEqual(self.resume(caller)['task_id'], owner['id'])


if __name__ == '__main__':
    unittest.main()
