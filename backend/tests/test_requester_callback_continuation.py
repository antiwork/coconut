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
            # Pending background work of the caller, beyond the input of its own turn, still refuses.
            with self.subTest(method=method), patch.object(self.service, method,
                    side_effect=lambda value, original=original, **options: True if
                    (value.get('id') if isinstance(value, dict) else value) == caller['id'] else original(value, **options)):
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


class ApprovedProposalRequesterContinuationTests(unittest.TestCase):
    """A person approved a follow-up proposal of the requester's callback, as in Desk 2b4380e0 for PR 8549.

    The approved root runs in its own execution. It delegated to a new Dev execution instead of the original owner.
    That delegate's callback filed a dependency block after the owner finished, so Hermes left its card in an input
    block, and Care's repair returned to the root. The root then continues the original owner.
    """
    setUp = fixtures.NativeWorkContinuationTests.setUp
    def tearDown(self):
        self.end_turn()
        fixtures.NativeWorkContinuationTests.tearDown(self)

    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume
    through_provider = RequesterCallbackContinuationTests.through_provider
    reject = RequesterCallbackContinuationTests.reject

    def returned(self, producer, name):
        # The owner's results return to the requester through the real delivery route.
        with self.service.transaction():
            delivered = self.service._store_bot_message(producer['id'], {
                'target_bot_id': self.desk['id'], 'text': name + ' result'}, returning=True)
            callback = self.service._get('tasks', delivered['task_id'])
            self.service._ensure_execution_session(callback)
        callback.update(status='completed', _native_turn_id=name + '-turn', _conversation_epoch=self.epoch,
                        _kanban_claim=dict(native_id='t_' + name, external_id=callback['id'],
                                           input=callback['id'], run_id=0, pid=0, context=''))
        self.service._put('tasks', callback)
        self.service.kanban.tasks[callback['id']] = dict(id='t_' + name, status='done')
        return callback

    def relay(self, source, bot, text):
        # A native message_agent call and its relay delivery, as the worker sends them.
        call = self.service.emit(source['id'], 'native_call', dict(name='message_agent',
                                 arguments=dict(target=bot['id'], message=text)))
        receipt = self.service.emit(source['id'], 'native_bot_message', dict(native_call_id=call['id'],
            native_envelope_id=uuid4().hex, target_bot_id=bot['id'], text=text))
        return self.service._get('tasks', receipt['task_id'])

    def finish(self, task, summary):
        # The task's own turn ends through the service, which prepares its return.
        self.service._put('tasks', dict(self.service._get('tasks', task['id']), status='working'))
        self.service.emit(task['id'], 'completed', {'summary': summary})
        return self.service._get('tasks', task['id'])

    def incident(self):
        from datetime import datetime, timedelta, timezone
        self.epoch = self.service._setting('conversation_epoch')
        self.care = self.write('/v1/bots', {'name': 'Care'})
        owner, requester = self.scheduled_native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        requester.update(status='completed', _waiting_on=[], _conversation_epoch=self.epoch)
        self.service._put('tasks', requester)
        reporter = self.returned(owner, 'reporter')
        proposer = self.returned(owner, 'proposer')
        # The proposer's turn records the proposal, and the person approves it in the app.
        self.service._put('tasks', dict(proposer, status='working'))
        self.service.emit(proposer['id'], 'native_proposal', dict(key='followup-draft', title='Create the draft PR?',
            options=[dict(id='draft', label='Create the test-hardening draft PR')]))
        self.service._put('tasks', proposer)
        decision = next(d for d in self.service._all('decisions') if d['task_id'] == proposer['id'])
        self.service.mutate('POST', f"/v1/decisions/{decision['id']}/answer",
                            dict(option_id='draft', revision=decision['revision']), str(uuid4()), 'test-phone')
        decision = self.service._get('decisions', decision['id'])
        root = self.service._get('tasks', decision['_execution_task_id'])
        self.service._ensure_execution_session(root)
        root.update(status='working', _native_turn_id='root-first-turn', _conversation_epoch=self.epoch)
        self.service._put('tasks', root)
        # The root relayed to a new Dev execution. Its result returned to the root through a callback.
        delegate = self.relay(root, self.dev, 'Publish the approved draft.')
        delegate.update(_conversation_epoch=self.epoch, _kanban_claim=dict(native_id='t_delegate', input=delegate['id']))
        self.service._put('tasks', delegate)
        self.service.kanban.tasks[delegate['id']] = dict(id='t_delegate', status='done')
        self.service._put('tasks', dict(self.service._get('tasks', root['id']), status='waiting'))
        self.finish(delegate, 'Another execution owns the worktree.')
        self.service._drain_returns()
        callback = next(t for t in self.service._all('tasks') if t.get('_is_return') and t.get('source_task_id') == delegate['id'])
        self.service._ensure_execution_session(callback)
        callback.update(status='working', _native_turn_id='callback-turn', _conversation_epoch=self.epoch,
                        _kanban_claim=dict(native_id='t_callback', external_id=callback['id'], input=callback['id'],
                                           run_id=72, pid=0, context=''))
        self.service._put('tasks', callback)
        # The callback asked Care to repair the route, then ended. Care's result goes to the root.
        helper = self.relay(callback, self.care, 'Repair the original owner route.')
        helper.update(_conversation_epoch=self.epoch, _kanban_claim=dict(native_id='t_helper', input=helper['id']))
        self.service._put('tasks', helper)
        callback = self.finish(callback, 'Complete.')
        callback['updated_at'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat().replace('+00:00', 'Z')
        self.service._put('tasks', callback)
        # Hermes re-kinded the callback's dependency block: its owner parent had already finished. The snapshot carries
        # the latest block event of the callback's own run as proof (Board.rekinded_block).
        self.service.kanban.tasks[callback['id']] = dict(id='t_callback', status='blocked', block_kind='needs_input',
                                                         current_run_id=None, worker_pid=None, dependencies=[],
                                                         block_rekind=dict(self.REKIND))
        # The root now owns a native outcome that waits for Care.
        root = self.service._get('tasks', root['id'])
        root.update(status='completed', _waiting_on=[], _native_outcome_id='t_root')
        self.service._put('tasks', root)
        self.service.kanban.tasks[root['id']] = dict(id='t_root', status='todo', block_kind='dependency',
                                                     current_run_id=None, worker_pid=None, input_id='root-first-turn',
                                                     dependencies=[{'id': 't_helper'}])
        self.service.kanban.tasks[helper['id']] = dict(id='t_helper', status='running', current_run_id=81, worker_pid=181)
        url = 'https://github.com/example/project/pull/8549'
        original = dict(id='original-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr',
                        title='Original PR', status='merged', pr_state='merged', url=url,
                        _review_task_id=owner['id'], _activity_task_id=owner['id'])
        peer = dict(id='requester-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
                    title='Requester PR', status='merged', pr_state='merged', url=url.upper(),
                    _review_task_id=requester['id'], _activity_task_id=reporter['id'])
        for item in (original, peer):
            self.service._put('artifacts', item)
        ids = dict(owner=owner['id'], requester=requester['id'], reporter=reporter['id'], proposer=proposer['id'],
                   decision=decision['id'], root=root['id'], delegate=delegate['id'], callback=callback['id'],
                   helper=helper['id'])
        return ids, original, peer

    def get(self, identity):
        return self.service._get('tasks', identity)

    def helper_finished(self, ids):
        # Care finishes normally. Its result resumes the root's native outcome, and the root's worker claims it.
        helper = self.finish(self.get(ids['helper']), 'Route repaired.')
        self.service.kanban.tasks[ids['helper']] = dict(id='t_helper', status='done')
        self.assertTrue(helper['_return_pending'])
        self.service._drain_returns()
        self.assertNotIn('_return_pending', self.get(ids['helper']))
        root = self.get(ids['root'])
        self.assertEqual(root['status'], 'queued')
        self.assertEqual(root['_bot_messages'][-1]['_return_source_task_id'], ids['helper'])
        return self.run_root(root)

    LONG_REPLY = ('Route repaired. New blocker: keep the original owner paused until the migration check on PR 8549 '
                  'passes, then publish only the draft.')

    def readied_root(self, ids, *, parent=True):
        # As in run 30130: Care's card completes while Care's own turn still runs, before its final reply exists.
        # Hermes readies the root's card, and the service queues the root's outcome (_native_retry).
        import time
        self.service._put('tasks', dict(self.get(ids['helper']), status='working'))
        self.service.kanban.tasks[ids['helper']] = dict(id='t_helper', status='done', current_run_id=None, worker_pid=None,
                                                        completed_at=int(time.time()), **self.created(ids['helper'], self.care))
        self.service.kanban.tasks[ids['root']] = dict(id='t_root', status='ready', current_run_id=None, worker_pid=None,
                                                      input_id=self.get(ids['root'])['_native_turn_id'], dependencies=[],
                                                      parent_ids=['t_root_first'] + (['t_helper'] if parent else []))
        self.service._native_retry(self.get(ids['root']), self.service.kanban.tasks[ids['root']])
        self.assertEqual(self.get(ids['root'])['status'], 'queued')

    @staticmethod
    def created(owner, bot, successor_input=None):
        # The creation record that Board writes on every card of a request.
        key = 'coconut-followup:%s:%s' % (owner, successor_input) if successor_input else 'coconut:' + owner
        return dict(created_by='coconut', tenant=bot['id'], idempotency_key=key)

    def root_row(self, ids):
        return next(r for r in self.service._kanban_rows() if r['id'] == ids['root'])['state']

    def start_root(self, ids, during=None):
        # The dispatcher claims the readied card and _run_task starts the turn. The provider records the turn's input,
        # then makes the continuation call from inside the turn.
        root = self.get(ids['root'])
        self.service.kanban.tasks[ids['root']].update(status='running', current_run_id=91, worker_pid=191,
                                                      input_id=root['_native_turn_id'])
        root['_kanban_claim'] = dict(native_id='t_root', external_id=root['id'], input=root['_native_turn_id'],
                                     run_id=91, pid=191, context='# Kanban task t_root')
        self.service._put('tasks', root)
        seen = {}
        def start(task, bot, history, emit):
            from coconut.hermes_worker import conversation_input
            seen['prompt'] = conversation_input(dict(operation='start', task=task, history=history))[0]
            seen['started'] = copy.deepcopy(self.get(ids['root']))
            if during:
                during()
            try:
                seen['receipt'] = emit('work_continue', dict(task_id='t_existing', message='Keep the verified correction local.'))
            except ServiceError as error:
                seen['receipt'] = error
        self.service.provider.start_task = start
        self.service._run_task(root['id'])
        return seen

    def run_root(self, root):
        turn = root['_native_turn_id']
        root.update(status='working', _kanban_claim=dict(native_id='t_root', external_id=root['id'], input=turn,
                    run_id=91, pid=191, context='# Kanban task t_root'))
        self.service._put('tasks', root)
        self.service.running[root['id']] = Mock(is_alive=Mock(return_value=True))
        self.service.kanban.tasks[root['id']] = dict(id='t_root', status='running', current_run_id=91,
                                                     worker_pid=191, input_id=turn, dependencies=[])
        return root

    def settle(self, ids, current=None):
        # Like Board.archive_settled, the adapter closes an input block only while the request's event is its proof.
        settled = []
        def call(request):
            if request['op'] == 'archive_settled':
                settled.append(request)
                proof = current if current is not None else self.service.kanban.tasks[ids['callback']].get('block_rekind')
                if not proof or request.get('rekind_event') != proof['event_id']:
                    return {'ok': False}
                self.service.kanban.tasks[ids['callback']] = dict(id='t_callback', status='done')
                return {'ok': True}
            return self.snapshot_result()
        self.service.kanban.call.side_effect = call
        self.service._settle_native_orphans()
        self.service.kanban.call.side_effect = lambda request: self.snapshot_result()
        return settled

    REKIND = dict(event_id=541371, run_id=72, requested_kind='dependency', rekind_reason='no_open_parent')
    SETTLED = dict(op='archive_settled', id='t_callback', summary='Complete.', rekind_event=541371)

    def ready(self):
        ids, original, peer = self.incident()
        root = self.helper_finished(ids)
        self.assertEqual(self.settle(ids), [self.SETTLED])
        return ids, original, peer, root

    def fresh(self):
        self.tearDown()
        self.setUp()
        return self.ready()

    def chain(self, ids):
        tasks = {t['id']: t for t in self.service._all('tasks')}
        return self.service._work_chain(tasks[ids['requester']], tasks, self.service._pending_completion_receipts(),
                                        include_continuations=True)

    def foreground(self, ids, caller='root'):
        tasks = {t['id']: t for t in self.service._all('tasks')}
        return self.service._requester_callback_foreground(tasks[ids[caller]], tasks[ids['requester']], tasks,
                                                          self.service.kanban.tasks)

    def test_proposal_lookup_uses_the_decision_task_index(self):
        from coconut.native_outcomes import PROPOSAL_DECISIONS
        plan = [row[-1] for row in self.service.db.execute('EXPLAIN QUERY PLAN ' + PROPOSAL_DECISIONS, ('task',))]
        self.assertTrue(any('USING INDEX decisions_task' in step for step in plan), plan)
        self.assertFalse(any(step.startswith('SCAN') for step in plan), plan)
        ids, _, _, root = self.ready()
        executed = []
        self.service.db.set_trace_callback(executed.append)
        try:
            self.assertTrue(self.foreground(ids))
        finally:
            self.service.db.set_trace_callback(None)
        lookups = [statement for statement in executed if 'FROM decisions' in statement]
        self.assertTrue(lookups)
        self.assertFalse([statement for statement in lookups if '_execution_task_id' in statement], lookups)

    def test_incident_graph_matches_the_saved_topology(self):
        ids, original, peer = self.incident()
        owner, root, callback, helper = (self.get(ids[k]) for k in ('owner', 'root', 'callback', 'helper'))
        proposer, delegate = self.get(ids['proposer']), self.get(ids['delegate'])
        from coconut.work_capacity import execution_key
        self.assertEqual(execution_key(proposer), ids['requester'])
        self.assertEqual((proposer['_is_return'], proposer['_assignment_task_id'], proposer['source_task_id']),
                         (True, ids['requester'], ids['owner']))
        self.assertEqual((root['parent_task_id'], execution_key(root), bool(root.get('_is_return'))),
                         (ids['proposer'], ids['root'], False))
        self.assertEqual((delegate['bot_id'], delegate['source_task_id'], delegate['_reply_to_task_id']),
                         (owner['bot_id'], ids['root'], ids['root']))
        self.assertNotEqual(execution_key(delegate), execution_key(owner))
        self.assertEqual((callback['_is_return'], callback['_assignment_task_id'], execution_key(callback)),
                         (True, ids['root'], ids['root']))
        self.assertEqual((helper['bot_id'], helper['source_task_id'], helper['_reply_to_task_id']),
                         (self.care['id'], ids['callback'], ids['root']))
        self.assertEqual((owner['_reply_to_task_id'], owner['source_task_id']), (ids['requester'], ids['requester']))
        self.assertEqual(self.service._get('decisions', ids['decision'])['status'], 'approved')
        # Like the live graph, the approved root and all of its branches belong to the requester's same-PR chain.
        self.assertEqual(set(ids.values()) - {ids['decision'], ids['owner']} - set(self.chain(ids)), set())

    def test_provider_root_continues_original_owner_once_after_helper_and_callback_settle(self):
        ids, original, peer = self.incident()
        # The callback settles first, so only Care's live run holds the gate.
        self.assertEqual(self.settle(ids), [self.SETTLED])
        self.service._put('tasks', dict(self.get(ids['helper']), status='working'))
        root = self.run_root(self.get(ids['root']))
        self.assertTrue(self.foreground(ids))
        owner = self.get(ids['owner'])
        receipt = self.through_provider(root)
        self.assertEqual(receipt['status'], 'rejected', receipt)
        self.assertIn('another execution', receipt['error'])
        self.assertEqual(self.get(ids['owner']), owner)
        # The root's worker ends, Care finishes normally, and its result resumes the root.
        self.service._put('tasks', dict(self.get(ids['root']), status='completed'))
        self.service.running.pop(ids['root'])
        root = self.helper_finished(ids)
        owner['_kanban_claim']['workspace'] = str(Path(self.temp.name) / 'retained-owner')
        self.service._put('tasks', owner)
        before = copy.deepcopy(self.service.kanban.tasks)
        tasks_before = self.service._all('tasks')
        decisions_before = self.service._all('decisions')
        receipt = self.through_provider(root, twice=True)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.get(owner['id'])
        for key in ('id', '_execution_session_id', '_kanban_claim', 'source_task_id', 'source_bot_id',
                    '_reply_to_task_id', 'thread_id', 'exchange_id'):
            self.assertEqual(queued[key], owner[key], key)
        self.assertEqual(queued['_reply_to_task_id'], ids['requester'])
        self.assertEqual(queued['_work_context_id'], original['id'])
        self.assertEqual(queued['_bot_messages'][-1]['sender_bot_id'], self.desk['id'])
        self.assertEqual(self.service.kanban.tasks, before)
        self.assertEqual(len(self.service._all('tasks')), len(tasks_before))
        for task in tasks_before:
            if task['id'] != owner['id']:
                self.assertEqual(self.get(task['id']), task)
        self.assertEqual(self.service._all('decisions'), decisions_before)
        self.assertEqual(self.service._get('artifacts', peer['id']), peer)
        self.assertEqual(len([d for d in self.service._all('deliveries')
                             if d['receipt'].get('native_task_id') == 't_existing']), 1)
        # The same turn's repeated call returns the saved receipt.
        self.assertEqual(self.service.emit(root['id'], 'work_continue', dict(
            task_id='t_existing', message='Keep the verified correction local.')), receipt)
        # The owner's result goes once to the original requester, not to the approved root.
        owner = self.get(owner['id'])
        owner.update(status='working')
        self.service._put('tasks', owner)
        self.service.kanban.tasks[owner['id']] = dict(id='t_existing', status='done', input_id=owner['_native_turn_id'])
        self.service.emit(owner['id'], 'completed', {'summary': 'Draft PR published.'})
        self.service._drain_returns()
        self.service._drain_returns()
        returned = [d for d in self.service._all('deliveries') if d['source_task_id'] == owner['id']
                    and d['text'] == 'Draft PR published.']
        self.assertEqual(len(returned), 1)
        result = self.get(returned[0]['receipt']['task_id'])
        self.assertEqual((result['bot_id'], result['_is_return'], result['_assignment_task_id']),
                         (self.desk['id'], True, ids['requester']))
        # A later turn of the requester continues the same retained owner and execution again.
        self.service._ensure_execution_session(result)
        result.update(status='working', _native_turn_id='later-turn', _conversation_epoch=self.epoch,
                      _kanban_claim=dict(native_id='t_later', external_id=result['id'], input=result['id'],
                                         run_id=95, pid=195, context=''))
        self.service._put('tasks', result)
        self.service.kanban.tasks[result['id']] = dict(id='t_later', status='running', current_run_id=95, worker_pid=195)
        again = self.through_provider(result)
        self.assertEqual(again, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        continued = self.get(owner['id'])
        for key in ('_execution_session_id', '_kanban_claim', '_reply_to_task_id', '_work_context_id'):
            self.assertEqual(continued[key], queued[key], key)

    def test_residual_callback_input_block_waits_for_the_settle_pass(self):
        ids, original, peer = self.incident()
        callback = self.get(ids['callback'])
        recent = dict(callback, updated_at=callback['created_at'])
        root = self.helper_finished(ids)
        self.assertTrue(self.foreground(ids))
        owner = self.get(ids['owner'])
        # Care finished and every result is consumed. Only the callback's input block remains, and it stays a guard.
        self.reject(owner, root)
        # The settle pass waits until the callback has finished for a while.
        self.service._put('tasks', recent)
        self.assertEqual(self.settle(ids), [])
        self.reject(owner, root)
        self.service._put('tasks', callback)
        self.assertEqual(self.settle(ids), [self.SETTLED])
        self.assertEqual(self.get(ids['callback']), callback)
        self.assertEqual(self.resume(root)['task_id'], owner['id'])

    def test_root_waits_for_the_helper_reply_before_it_starts(self):
        ids, original, peer = self.incident()
        self.assertEqual(self.settle(ids), [self.SETTLED])
        self.readied_root(ids)
        # Care's card is done, but Care's turn has not ended, so its reply does not exist yet.
        self.assertEqual(self.root_row(ids), 'scheduled')
        helper = self.finish(self.get(ids['helper']), self.LONG_REPLY)
        self.assertTrue(helper['_return_pending'])
        self.assertEqual(self.root_row(ids), 'scheduled')
        self.service._drain_returns()
        root = self.get(ids['root'])
        self.assertEqual((root['status'], root.get('_steering', [])), ('queued', []))
        self.assertEqual(root['_bot_messages'][-1]['text'], self.LONG_REPLY)
        self.assertEqual(self.root_row(ids), 'ready')
        owner = self.get(ids['owner'])
        seen = self.start_root(ids)
        # The whole reply, with its new blocker, is in the turn's input. Nothing waits as steering.
        self.assertIn(self.LONG_REPLY, seen['prompt'])
        self.assertEqual((seen['started']['status'], seen['started']['_steering']), ('working', []))
        self.assertEqual(seen['receipt'], dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.get(owner['id'])
        for key in ('_execution_session_id', '_kanban_claim', 'source_task_id', '_reply_to_task_id', 'thread_id'):
            self.assertEqual(queued[key], owner[key], key)
        self.assertEqual(queued['_work_context_id'], original['id'])
        self.assertEqual(len([d for d in self.service._all('deliveries')
                             if d['receipt'].get('native_task_id') == 't_existing']), 1)

    def test_a_person_reply_while_the_root_waits_is_kept_for_its_turn(self):
        for attachment in (False, True):
            with self.subTest(attachment=attachment):
                ids, original, peer = self.fresh_incident()
                self.assertEqual(self.settle(ids), [self.SETTLED])
                self.readied_root(ids)
                data = dict(text='Also keep the README change out of the draft.')
                if attachment:
                    data['attachment_ids'] = [self.write('/v1/attachments', dict(bot_id=self.desk['id'], filename='note.txt',
                                                         content_base64='bm90ZQ=='))['id']]
                self.write('/v1/tasks/' + ids['root'] + '/replies', data)
                self.assertEqual(self.root_row(ids), 'scheduled')
                self.finish(self.get(ids['helper']), self.LONG_REPLY)
                self.service._drain_returns()
                self.assertEqual(self.root_row(ids), 'ready')
                seen = self.start_root(ids)
                self.assertIn(self.LONG_REPLY, seen['prompt'])
                self.assertIn(data['text'], seen['prompt'])
                self.assertEqual(seen['started']['_steering'], [])
                self.assertEqual(seen['receipt']['task_id'], ids['owner'])

    def test_only_a_parent_helper_that_owes_this_root_a_result_holds_it(self):
        def helper(**changes):
            return lambda ids: self.service._put('tasks', dict(self.get(ids['helper']), **changes))
        def finished(ids):
            self.finish(self.get(ids['helper']), self.LONG_REPLY)
        def failed(ids):
            self.service.emit(ids['helper'], 'failed', dict(summary='Care could not repair the route.'))
            self.assertTrue(self.get(ids['helper'])['_return_pending'])
        def delivered(ids):
            finished(ids)
            self.service._drain_returns()
        def rebound(times, linked_successor=False, earlier=None):
            # The helper's turn ended with a follow-up, so Board made a successor of its done card and rebound it.
            # The old card stays in the snapshot as an unbound outcome card. As with Care's t_d9d0cb1d, the card that
            # the root linked can itself be an earlier successor of the helper.
            def apply(ids):
                previous = self.service.kanban.tasks.pop(ids['helper'])
                if linked_successor:
                    previous.update(self.created(ids['helper'], self.care, 'earlier-input'))
                if earlier:
                    previous.update(earlier)
                for index in range(times):
                    self.service.kanban.tasks['outcome:' + previous['id']] = previous
                    previous = dict(id='t_helper_next%d' % index, status='done' if index + 1 < times else 'running',
                                    current_run_id=None if index + 1 < times else 83, parent_ids=[previous['id']],
                                    **self.created(ids['helper'], self.care, 'input-%d' % index))
                self.service.kanban.tasks[ids['helper']] = previous
                self.service._put('tasks', dict(self.get(ids['helper']), status='working'))
            return apply
        def foreign(**record):
            def apply(ids):
                self.service.kanban.tasks[ids['helper']].update(record)
            return apply
        def evicted(claimed):
            # A bound card that finished long ago can drop out of the cached snapshot.
            def apply(ids):
                self.service.kanban.tasks.pop(ids['helper'])
                if not claimed:
                    task = self.get(ids['helper'])
                    task['_kanban_claim'] = dict(task['_kanban_claim'], native_id='t_unrelated')
                    self.service._put('tasks', task)
            return apply
        def plain_root(ids):
            root = self.get(ids['root'])
            root.pop('_native_outcome_id')
            self.service._put('tasks', root)
        cases = {
            'helper turn still running': (True, None, 'scheduled'),
            'helper reply not yet delivered': (True, finished, 'scheduled'),
            'helper failed with its result pending': (True, failed, 'scheduled'),
            'helper waits for a person': (True, helper(status='needs_you'), 'scheduled'),
            'helper native reply pending': (True, helper(status='completed', _native_reply_pending={'text': 'reply'}), 'scheduled'),
            'helper result pending without a relay reply': (True, helper(status='completed', _native_reply_pending=None,
                                                                         _return_pending={'text': 'result'}), 'scheduled'),
            'helper reply delivered': (True, delivered, 'ready'),
            'helper replies to another task': (True, helper(_reply_to_task_id='another-task'), 'ready'),
            'helper card is not a parent': (False, None, 'ready'),
            'helper cancelled': (True, helper(status='cancelled'), 'ready'),
            'helper rebound to a successor card': (True, rebound(1), 'scheduled'),
            'helper rebound through two successor cycles': (True, rebound(2), 'scheduled'),
            'linked successor card and the helper rebinds again': (True, rebound(1, linked_successor=True), 'scheduled'),
            'bound parent card made by a worker is still its task': (True, foreign(created_by='default'), 'scheduled'),
            'earlier card made by a worker, not by Board': (True, rebound(1, earlier=dict(created_by='default')), 'ready'),
            'earlier card of another bot': (True, rebound(1, earlier=dict(tenant='another-bot')), 'ready'),
            'earlier card naming another request': (True, rebound(1, earlier=dict(idempotency_key='coconut:another-task')), 'ready'),
            'earlier card without an owner key': (True, rebound(1, earlier=dict(idempotency_key='coconut-outcome:work')), 'ready'),
            'evicted parent card still claimed by the helper': (True, evicted(True), 'scheduled'),
            'evicted parent card that no task claims': (True, evicted(False), 'ready'),
            'root has no native outcome': (True, plain_root, 'ready'),
        }
        for name, (parent, apply, state) in cases.items():
            with self.subTest(case=name):
                ids, original, peer = self.fresh_incident()
                self.assertEqual(self.settle(ids), [self.SETTLED])
                self.readied_root(ids, parent=parent)
                if apply:
                    apply(ids)
                self.assertEqual(self.root_row(ids), state)

    def test_one_card_index_per_dispatch_pass(self):
        # Every parented outcome reads the same card index of the pass. The number of whole-snapshot scans in one
        # _kanban_rows pass must not grow with the number of such outcomes, and every hold stays as it was.
        ids, original, peer = self.incident()
        self.assertEqual(self.settle(ids), [self.SETTLED])
        self.readied_root(ids)
        class Snapshot(dict):
            scans = 0
            def items(self):
                Snapshot.scans += 1
                return super().items()
            def values(self):
                Snapshot.scans += 1
                return super().values()
            def keys(self):
                Snapshot.scans += 1
                return super().keys()
            def __iter__(self):
                Snapshot.scans += 1
                return super().__iter__()
        def task(bot, name):
            created = self.write('/v1/bots/' + bot['id'] + '/messages', {'text': name})
            row = self.get(created['id'])
            row.update(_execution_session_id=row['id'], _conversation_epoch=self.epoch)
            self.service._put('tasks', row)
            return row
        def outcome(name, parents):
            row = task(self.desk, name)
            row.update(status='queued', _native_outcome_id='t_' + name, _native_turn_id=name + '-input')
            self.service._put('tasks', row)
            self.service.kanban.tasks[row['id']] = dict(id='t_' + name, status='ready', current_run_id=None, worker_pid=None,
                                                        input_id=name + '-input', dependencies=[], parent_ids=parents,
                                                        **self.created(row['id'], self.desk))
            return row['id']
        def helper(name, root, claim=None):
            row = task(self.care, name)
            row.update(status='working', _reply_to_task_id=root, _kanban_claim=dict(native_id=claim or 't_' + name, input=row['id']))
            self.service._put('tasks', row)
            return row['id']
        held = {ids['root']: 'scheduled'}
        # An adopted worker card bound to its helper.
        adopted = outcome('adopted-root', ['t_adopted'])
        self.service.kanban.tasks[helper('adopted-helper', adopted)] = dict(id='t_adopted', status='done', created_by='default',
                                                                            tenant=self.care['id'], idempotency_key='coconut:x:y')
        # An earlier card of a helper that has moved to a successor.
        earlier = outcome('earlier-root', ['t_earlier'])
        moved = helper('moved-helper', earlier, claim='t_moved_next')
        self.service.kanban.tasks['outcome:t_earlier'] = dict(id='t_earlier', status='done', **self.created(moved, self.care))
        self.service.kanban.tasks[moved] = dict(id='t_moved_next', status='running', parent_ids=['t_earlier'],
                                                **self.created(moved, self.care, 'next'))
        # A bound card that dropped out of the cached snapshot. Its helper still claims it.
        missing = outcome('missing-root', ['t_gone'])
        helper('gone-helper', missing, claim='t_gone')
        held.update({adopted: 'scheduled', earlier: 'scheduled', missing: 'scheduled'})
        def run_pass(extra):
            plain = [outcome('plain-%d-%d' % (extra, index), ['t_foreign_%d' % index]) for index in range(extra)]
            self.service.kanban.tasks = Snapshot(self.service.kanban.tasks)
            Snapshot.scans = 0
            states = {row['id']: row['state'] for row in self.service._kanban_rows()}
            for identity in plain:
                self.assertEqual(states[identity], 'ready')
            return {identity: states[identity] for identity in held}, Snapshot.scans, plain
        first, few, plain = run_pass(1)
        for identity in plain:
            self.service._put('tasks', dict(self.get(identity), status='completed'))
        second, many, _ = run_pass(4)
        self.assertEqual(first, held)
        self.assertEqual(second, held)
        self.assertEqual(many, few)

    def test_a_reply_that_arrives_during_the_turn_still_blocks_the_owner(self):
        # When nothing holds the root, for example a helper that is not its parent, the reply arrives as steering.
        # The owner gate keeps treating it as new input.
        ids, original, peer = self.incident()
        self.assertEqual(self.settle(ids), [self.SETTLED])
        self.readied_root(ids, parent=False)
        self.assertEqual(self.root_row(ids), 'ready')
        owner = self.get(ids['owner'])
        def deliver():
            self.finish(self.get(ids['helper']), self.LONG_REPLY)
            self.service._drain_returns()
            self.assertEqual(self.get(ids['root'])['_steering'], ['Care: ' + self.LONG_REPLY])
        seen = self.start_root(ids, during=deliver)
        self.assertIsInstance(seen['receipt'], ServiceError)
        self.assertIn('another execution', str(seen['receipt']))
        self.assertEqual(self.get(ids['owner']), owner)

    def fresh_incident(self):
        self.end_turn()
        self.tearDown()
        self.setUp()
        return self.incident()

    def end_turn(self):
        # Let a blocked fixture turn return, so the service closes without waiting for it.
        turn = getattr(self, 'turn', None)
        if turn:
            turn['go'].set()
            turn['done'].set()
            for worker in list(self.service.running.values()):
                if isinstance(worker, threading.Thread):
                    worker.join(10)
            self.turn = None

    def test_deliberate_callback_holds_are_never_settled(self):
        # Only the re-kind proof of the callback's own claimed run settles its block. Any other hold stays, and so does
        # the gate on the original owner.
        cases = {
            'explicit input block': dict(card=dict(block_rekind=None)),
            'capability triage': dict(card=dict(status='triage', block_kind='capability')),
            'capability block with proof': dict(card=dict(block_kind='capability')),
            'proof of another run': dict(card=dict(block_rekind=dict(self.REKIND, run_id=73))),
            'proof of another kind': dict(card=dict(block_rekind=dict(self.REKIND, requested_kind='needs_input'))),
            'proof without reason': dict(card=dict(block_rekind=dict(self.REKIND, rekind_reason=None))),
            'proof without event': dict(card=dict(block_rekind=dict(self.REKIND, event_id=None))),
            'claim of another card': dict(task=dict(native_id='t_other')),
            'unclaimed run': dict(task=dict(run_id=None)),
            'failed callback': dict(status='failed'),
            'callback with outcome': dict(outcome='t_callback'),
            'unknown tool effect': dict(receipt=True),
            'pending decision': dict(decision=True),
            'newer hold before the archive': dict(current=dict(self.REKIND, event_id=541999)),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                self.tearDown()
                self.setUp()
                ids, original, peer = self.incident()
                root = self.helper_finished(ids)
                owner = self.get(ids['owner'])
                card = self.service.kanban.tasks[ids['callback']]
                card.update(case.get('card', {}))
                callback = self.get(ids['callback'])
                if 'task' in case:
                    callback['_kanban_claim'] = dict(callback['_kanban_claim'], **case['task'])
                if 'status' in case:
                    callback['status'] = case['status']
                if 'outcome' in case:
                    callback['_native_outcome_id'] = case['outcome']
                self.service._put('tasks', callback)
                if case.get('receipt'):
                    self.service._put('native_receipts', dict(id='callback-unknown', task_id=ids['callback'], turn_id='callback-turn',
                        state='executing', action={'name': 'unknown', 'arguments': {}}))
                if case.get('decision'):
                    self.service._put('decisions', dict(id='callback-hold', task_id=ids['callback'], status='pending'))
                held = copy.deepcopy(self.service.kanban.tasks[ids['callback']])
                settled = self.settle(ids, case.get('current'))
                self.assertEqual(self.service.kanban.tasks[ids['callback']], held)
                if 'current' in case:
                    self.assertEqual(settled, [self.SETTLED])
                else:
                    self.assertEqual(settled, [])
                self.reject(owner, root)
                self.assertEqual(self.get(ids['callback']), callback)

    def test_old_and_wrong_callers_are_not_the_foreground(self):
        for name in ('delegate', 'helper', 'callback', 'proposer', 'reporter'):
            with self.subTest(caller=name):
                ids, _, _, root = self.fresh()
                owner = self.get(ids['owner'])
                caller = self.get(ids[name])
                caller.update(status='working', _native_turn_id=name + '-new-turn')
                self.service._put('tasks', caller)
                self.assertFalse(self.foreground(ids, name) and name != 'proposer')
                with self.assertRaises(ServiceError):
                    self.resume(caller)
                self.assertEqual(self.get(ids['owner']), owner)
        # The earlier proposer is a verified requester callback, so only the running root holds it back.
        ids, _, _, root = self.fresh()
        self.service._put('tasks', dict(root, status='completed'))
        self.service.running.pop(ids['root'])
        self.service.kanban.tasks[ids['root']] = dict(id='t_root', status='done', input_id=root['_native_turn_id'])
        with self.assertRaises(ServiceError):
            self.resume(self.get(ids['root']))

    def test_answer_provenance_is_required(self):
        # A forged or missing answer neither links the root into the requester's graph nor makes it the foreground.
        cases = [('decisions', {'decided_by': None}), ('decisions', {'status': 'pending'}),
                 ('decisions', {'status': 'rejected'}), ('decisions', {'inbox_kind': 'question'}),
                 ('decisions', {'bot_id': 'other-bot'}), ('decisions', {'task_id': 'reporter'}),
                 ('decisions', {'_execution_task_id': 'callback'}),
                 ('tasks', {'parent_task_id': 'reporter'}), ('tasks', {'thread_id': 'other'}),
                 ('tasks', {'conversation_bot_id': 'other-bot'})]
        for table, changes in cases:
            with self.subTest(table=table, changes=changes):
                ids, _, _, root = self.fresh()
                changes = {k: ids.get(v, v) for k, v in changes.items()}
                row = self.service._get('decisions', ids['decision']) if table == 'decisions' else root
                self.service._put(table, dict(row, **changes))
                self.assertFalse(self.foreground(ids))
                self.assertNotIn(ids['root'], self.chain(ids))
        # A linked root that is a callback or that runs in the requester's execution is not the answer's execution.
        for changes in ({'_is_return': True}, {'_execution_session_id': 'requester'}):
            with self.subTest(root=changes):
                ids, _, _, root = self.fresh()
                owner = self.get(ids['owner'])
                self.service._put('tasks', dict(root, **{k: ids.get(v, v) for k, v in changes.items()}))
                self.assertIn(ids['root'], self.chain(ids))
                self.assertFalse(self.foreground(ids))
                self.reject(owner, self.get(ids['root']))
        ids, _, _, root = self.fresh()
        self.assertTrue(self.foreground(ids))
        self.assertEqual(self.resume(root)['task_id'], ids['owner'])

    def test_proposing_callback_needs_verified_return_provenance(self):
        cases = (('tasks', {'_is_return': False}), ('tasks', {'_assignment_task_id': 'root'}),
                 ('tasks', {'source_bot_id': 'unrelated'}), ('tasks', {'exchange_id': 'other'}),
                 ('deliveries', {'target_bot_id': 'other'}), ('deliveries', {'receipt': 'reporter'}),
                 ('deliveries', {'receipt': 'rejected'}))
        for table, changes in cases:
            with self.subTest(table=table, changes=changes):
                ids, _, _, root = self.fresh()
                owner, proposer = self.get(ids['owner']), self.get(ids['proposer'])
                row = proposer if table == 'tasks' else next(
                    d for d in self.service._all('deliveries') if d['receipt']['task_id'] == proposer['id'])
                if 'receipt' in changes:
                    value = changes['receipt']
                    changes = {'receipt': dict(row['receipt'], **({'status': value} if value == 'rejected' else {'task_id': ids[value]}))}
                self.service._put(table, dict(row, **{k: ids.get(v, v) if isinstance(v, str) else v for k, v in changes.items()}))
                self.assertIn(ids['root'], self.chain(ids))
                self.assertFalse(self.foreground(ids))
                self.reject(owner, root)

    def test_root_claim_must_be_its_own_current_outcome_run(self):
        card_cases = ({'input_id': 'older-input'}, {'id': 't_other'}, {'current_run_id': 92}, {'worker_pid': 192},
                      {'status': 'review'}, {'claim_source_status': 'review'})
        root_cases = ({'_kanban_claim': dict(input='older-input')}, {'_native_turn_id': 'newer-turn'},
                      {'_native_outcome_id': 't_other'}, {'_native_outcome_id': None},
                      {'_kanban_claim': dict(native_id='t_other')}, {'_kanban_claim': dict(external_id='other')},
                      {'status': 'completed'})
        for kind, changes in [('card', c) for c in card_cases] + [('root', c) for c in root_cases]:
            with self.subTest(kind=kind, changes=changes):
                ids, _, _, root = self.fresh()
                owner = self.get(ids['owner'])
                if kind == 'card':
                    self.service.kanban.tasks[ids['root']] = dict(self.service.kanban.tasks[ids['root']], **changes)
                else:
                    if '_kanban_claim' in changes:
                        changes = {'_kanban_claim': dict(root['_kanban_claim'], **changes['_kanban_claim'])}
                    self.service._put('tasks', dict(root, **changes))
                self.assertFalse(self.foreground(ids))
                self.reject(owner, self.get(ids['root']))
        ids, _, _, root = self.fresh()
        self.assertEqual(self.resume(root)['task_id'], ids['owner'])

    def test_pending_choices_inputs_returns_tools_and_cancellation_still_block(self):
        def task(name, **changes):
            return lambda ids, original, peer: self.service._put('tasks', dict(self.get(ids[name]), **changes))
        def row(table, name, **values):
            return lambda ids, original, peer: self.service._put(table, dict(values, task_id=ids[name]))
        def card(name, **changes):
            return lambda ids, original, peer: self.service.kanban.tasks.__setitem__(
                ids[name], dict(self.service.kanban.tasks[ids[name]], **changes))
        cases = {}
        for name in ('root', 'requester', 'proposer', 'callback', 'helper', 'delegate', 'reporter'):
            cases['decision ' + name] = row('decisions', name, id='pending-' + name, status='pending')
            cases['context ' + name] = row('contexts', name, id='context-' + name, status='queued')
            cases['unknown tool ' + name] = row('native_receipts', name, id='unknown-' + name, turn_id='turn',
                                               state='executing', action={'name': 'unknown', 'arguments': {}})
        for key, value in (('_waiting_on', ['unfinished']), ('_return_pending', {'text': 'result'}),
                           ('_steering', ['new input']), ('_live_messages', {'message': 'new input'}),
                           ('_blocked_followup', {'text': 'new input'}), ('_native_reply_pending', {'text': 'result'})):
            cases['root ' + key] = task('root', **{key: value})
        for name in ('helper', 'delegate', 'requester'):
            cases['return ' + name] = task(name, _return_pending={'text': 'result'})
        cases['waiting requester'] = task('requester', status='waiting', _waiting_on=['unfinished'])
        for name in ('helper', 'callback', 'proposer', 'delegate', 'reporter'):
            cases['working ' + name] = task(name, status='working')
        cases['helper native claim'] = card('helper', status='running', current_run_id=82, worker_pid=182)
        cases['callback input block'] = card('callback', status='blocked', block_kind='needs_input', block_rekind=None)
        cases['proposer native claim'] = card('proposer', status='running', current_run_id=83)
        cases['delegate native retry'] = card('delegate', status='ready', block_kind='transient')
        cases['live helper worker'] = lambda ids, original, peer: self.service.running.__setitem__(
            ids['helper'], Mock(is_alive=Mock(return_value=True)))
        cases['cancelled exchange'] = lambda ids, original, peer: self.service._put('exchanges', dict(
            self.service._get('exchanges', self.get(ids['root'])['exchange_id']), cancelled=True))
        cases['cancelled work'] = lambda ids, original, peer: self.service.db.execute(
            'INSERT INTO settings VALUES (?,?)', ('work.cancelled.' + self.service._work_identity(original), 'true'))
        cases['independent same-PR reviewer'] = lambda ids, original, peer: self.service._put(
            'artifacts', dict(peer, _review_task_id=ids['root']))
        for name, apply in cases.items():
            with self.subTest(case=name):
                ids, original, peer, root = self.fresh()
                owner = self.get(ids['owner'])
                apply(ids, original, peer)
                receipt = self.through_provider(self.get(ids['root']))
                self.assertEqual(receipt['status'], 'rejected', receipt)
                self.assertEqual(self.get(ids['owner']), owner)
                for decision in self.service._all('decisions'):
                    if decision['id'].startswith('pending-'):
                        self.assertEqual(decision['status'], 'pending')
        # Without any of them the same graph admits once.
        ids, original, peer, root = self.fresh()
        self.assertEqual(self.through_provider(root)['task_id'], ids['owner'])

if __name__ == '__main__':
    unittest.main()
