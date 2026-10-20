"""A delegated PR keeps its execution when another requester task's Work registered a wait for that execution.

The saved graph: an intake audit delegates a PR to its owner. A later assignment audit of the same requester owns the
Work of that PR and links its native outcome to a native run of the owner. The PR joined that Work by its URL only.
"""
import copy
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from coconut.service import ServiceError
import test_native_claim_continuation as native_fixtures
import test_native_work_continuation as fixtures
import test_requester_callback_continuation as callbacks

URL = 'https://github.com/example/project/pull/8'
UNRELATED = 'belongs to another native outcome'


def record_tool(service, task, name, arguments, result, turn, *, finish=True):
    """Save a tool call and its result through the provider's receipt events for a turn of the task."""
    saved = service._get('tasks', task['id'])
    service._put('tasks', dict(saved, status='working', _native_turn_id=turn))
    call = service.emit(task['id'], 'native_call', dict(name=name, arguments=arguments))
    if finish:
        service.emit(task['id'], 'native_result', dict(id=call['id'], result=json.dumps(result)))
    service._put('tasks', saved)
    return call['id']


class RegisteredDelegationTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    resume = fixtures.NativeWorkContinuationTests.resume
    through_provider = callbacks.RequesterCallbackContinuationTests.through_provider

    def saved_graph(self, settled=True, link=True):
        owner, audit = self.scheduled_native()
        self.service.kanban.tasks[owner['id']]['status'] = 'done'
        # The intake audit finished. It commented on other cards, never on the Work's card.
        audit = dict(self.service._get('tasks', audit['id']), status='completed', _waiting_on=[],
                     _source_native_ids=['t_issue'], _native_turn_id='audit-turn',
                     _conversation_epoch=self.service._setting('conversation_epoch'),
                     _kanban_claim=dict(native_id='t_audit', external_id=audit['id'], input='audit-turn', run_id=1))
        self.service._put('tasks', audit)
        self.service.kanban.tasks[audit['id']] = dict(external_id=audit['id'], id='t_audit', status='done',
                                                      current_run_id=None, worker_pid=None)
        # The owner commented on the Work's card. An earlier run of the owner completed its own card.
        owner = dict(owner, _source_native_ids=['t_issue', 't_requester'])
        self.service._put('tasks', owner)
        record_tool(self.service, owner, 'kanban_complete', {'summary': 'Ready for review'},
                    {'ok': True, 'task_id': 't_earlier', 'run_id': 11}, 'earlier-turn')
        with self.service.transaction():
            exchange = self.service._new_exchange()
        identity = str(uuid4())
        coordinator = dict(audit, id=identity, thread_id=identity, exchange_id=exchange, exchange_depth=0,
                           _execution_session_id=identity, parent_task_id=None, source_task_id=None,
                           source_bot_id=None, status='completed', _native_turn_id='coordinator-turn',
                           _native_outcome_id='t_requester', _work_context_id='requester-work',
                           _kanban_claim=dict(native_id='t_requester', external_id=identity,
                                              input='coordinator-turn', run_id=21),
                           _source_native_ids=['t_issue', 't_requester', 't_assignment'])
        coordinator.pop('_work_todos', None)
        self.service._put('tasks', coordinator)
        if link:
            record_tool(self.service, coordinator, 'kanban_link', {'parent_id': 't_earlier', 'child_id': 't_requester'},
                        {'ok': True, 'parent_id': 't_earlier', 'child_id': 't_requester', 'gated': False},
                        'coordinator-turn')
        self.service.kanban.tasks[coordinator['id']] = dict(
            external_id=coordinator['id'], id='t_requester', status='done' if settled else 'scheduled',
            block_kind=None if settled else 'dependency', current_run_id=None, worker_pid=None,
            parent_ids=['t_assignment', 't_earlier'])
        work = dict(id='requester-work', task_id=coordinator['id'], bot_id=coordinator['bot_id'], kind='work',
                    title='Payout delays', status='waiting', _native_outcome_id='t_requester',
                    _work_key='retained-pr:' + URL)
        # The requester's own report of the same PR. Its review and activity task is the Work's task.
        requester_pr = dict(id='requester-pr', task_id=coordinator['id'], bot_id=coordinator['bot_id'], kind='pr',
                            title='Payout delays', status='ready_for_review', pr_state='open', url=URL,
                            work_outcome_id=work['id'], _review_task_id=coordinator['id'],
                            _activity_task_id=coordinator['id'])
        # The owner's PR. It joined the Work through the requester's same-URL report.
        pr = dict(id='delegated-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr',
                  title='Payout delays', status='ready_for_review', pr_state='open', url=URL,
                  work_outcome_id=work['id'], _review_task_id=owner['id'])
        for item in (work, requester_pr, pr):
            self.service._put('artifacts', item)
        owner = self.service._get('tasks', owner['id'])
        self.assertNotIn('_work_context_id', owner)
        self.assertNotIn('_native_outcome_id', owner)
        self.assertNotEqual(work['task_id'], owner['source_task_id'])
        self.assertNotIn('t_requester', audit['_source_native_ids'])
        return owner, audit, self.service._get('tasks', coordinator['id']), work, requester_pr, pr

    def owner_caller(self, owner):
        caller = self.write(f'/v1/bots/{owner["bot_id"]}/messages', {'text': 'Apply the approved copy corrections.'})
        caller = dict(self.service._get('tasks', caller['id']), status='working', _native_turn_id='owner-caller-turn')
        self.service._put('tasks', caller)
        return caller

    def callback(self, owner, audit, status='working'):
        """The owner's report returns to the intake audit through the real return route."""
        report = dict(owner, id=str(uuid4()), _execution_session_id=str(uuid4()),
                      _kanban_claim={'native_id': 't_report', 'input': 'report'})
        report.pop('_work_todos', None)
        self.service._put('tasks', report)
        self.service.kanban.tasks[report['id']] = dict(id='t_report', status='done')
        with self.service.transaction():
            delivered = self.service._store_bot_message(report['id'], {
                'target_bot_id': audit['bot_id'], 'text': 'The PR is ready for review.'}, returning=True)
            callback = self.service._get('tasks', delivered['task_id'])
            self.service._ensure_execution_session(callback)
        if status == 'working':
            callback.update(status='working', _native_turn_id='callback-turn',
                            _conversation_epoch=audit['_conversation_epoch'],
                            _kanban_claim=dict(native_id='t_callback', external_id=callback['id'],
                                               input=callback['id'], run_id=71, pid=101, context=''))
            self.service.running[callback['id']] = Mock(is_alive=Mock(return_value=True))
            self.service.kanban.tasks[callback['id']] = dict(id='t_callback', status='running',
                                                             current_run_id=71, worker_pid=101)
        self.service._put('tasks', callback)
        self.assertEqual(callback['_assignment_task_id'], audit['id'])
        self.assertTrue(callback['_is_return'])
        return callback

    def assert_admitted(self, owner, caller, saved):
        before = copy.deepcopy(self.service.kanban.tasks)
        tasks_before = self.service._all('tasks')
        receipt = self.through_provider(caller, twice=True)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        queued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', '_kanban_claim', 'session_id', 'thread_id', 'exchange_id',
                    'source_task_id', 'source_bot_id', '_reply_to_task_id', 'conversation_bot_id'):
            self.assertEqual(queued.get(key), owner.get(key), key)
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(queued['_native_outcome_id'], 't_existing')
        # The owner's PR is the retained item. The requester's Work stays its presentation parent.
        self.assertEqual(queued['_work_context_id'], 'delegated-pr')
        pr, work, requester_pr = saved
        self.assertEqual(self.service._get('artifacts', pr['id']), dict(pr, _native_outcome_id='t_existing'))
        self.assertEqual(self.service._get('artifacts', work['id']), work)
        self.assertEqual(self.service._get('artifacts', requester_pr['id']), requester_pr)
        self.assertEqual(self.service._get('artifacts', pr['id'])['work_outcome_id'], work['id'])
        self.assertEqual(self.service.kanban.tasks, before)
        self.assertEqual(len(self.service._all('tasks')), len(tasks_before))
        for task in tasks_before:
            if task['id'] != owner['id']:
                self.assertEqual(self.service._get('tasks', task['id']), task)
        self.assertEqual(len([d for d in self.service._all('deliveries')
                              if d['receipt'].get('native_task_id') == 't_existing']), 1)
        return queued

    def assert_rejected(self, owner, caller, message):
        artifacts = self.service._all('artifacts')
        tasks = self.service._all('tasks')
        with self.assertRaisesRegex(ServiceError, message):
            self.resume(caller)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._all('artifacts'), artifacts)
        self.assertEqual(self.service._all('tasks'), tasks)
        self.assertFalse([d for d in self.service._all('deliveries') if d['receipt'].get('native_task_id')])

    def test_the_owner_continues_the_saved_pr_once_through_the_provider(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        self.assert_admitted(owner, self.owner_caller(owner), (pr, work, requester_pr))

    def test_the_intake_callback_continues_the_saved_pr_once_through_the_provider(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        caller = self.callback(owner, audit)
        queued = self.assert_admitted(owner, caller, (pr, work, requester_pr))
        self.assertEqual(queued['_bot_messages'][-1]['sender_bot_id'], audit['bot_id'])

    def test_comments_and_the_same_url_alone_keep_the_requester_work(self):
        # The saved graph without the Work's registration: the PR shares only the URL and the comments.
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph(link=False)
        self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_the_registration_must_name_this_owner_and_this_work(self):
        cases = {
            'edge removed from the board': lambda o, a, c: self.service.kanban.tasks[c['id']].update(
                parent_ids=['t_assignment']),
            'work card rebound': lambda o, a, c: self.service.kanban.tasks[c['id']].update(id='t_rebound'),
            'work card missing': lambda o, a, c: self.service.kanban.tasks.pop(c['id']),
            'work task bound elsewhere': lambda o, a, c: self.service._put('tasks', dict(c, _native_outcome_id='t_other')),
            'work task reports another item': lambda o, a, c: self.service._put('tasks', dict(c, _work_context_id='other')),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                change(owner, audit, coordinator)
                self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_only_the_works_task_and_the_owners_run_register(self):
        def other_task(owner, audit, coordinator):
            # Another task of the requester bot links the same cards.
            other = dict(coordinator, id=str(uuid4()), _execution_session_id=None, _work_context_id=None,
                         _native_outcome_id='t_other_requester', _kanban_claim={'native_id': 't_other_requester'})
            other['_execution_session_id'] = other['id']
            self.service._put('tasks', other)
            return other, 't_earlier', 't_requester'

        def other_run(owner, audit, coordinator):
            # The same bot ran another execution. Its card is not the owner's.
            sibling = dict(owner, id=str(uuid4()), _kanban_claim={'native_id': 't_sibling', 'input': 'sibling'})
            sibling['_execution_session_id'] = sibling['id']
            self.service._put('tasks', sibling)
            record_tool(self.service, sibling, 'kanban_complete', {'summary': 'Done'},
                        {'ok': True, 'task_id': 't_sibling', 'run_id': 12}, 'sibling-turn')
            self.service.kanban.tasks[sibling['id']] = dict(id='t_sibling', status='done')
            self.service.kanban.tasks[coordinator['id']]['parent_ids'].append('t_sibling')
            return coordinator, 't_sibling', 't_requester'

        def other_child(owner, audit, coordinator):
            return coordinator, 't_earlier', 't_other_child'

        def context_run(owner, audit, coordinator):
            # A context job of the owner is not one of its runs.
            self.service._put('contexts', dict(id='owner-context', task_id=owner['id'], bot_id=owner['bot_id'],
                                               status='completed', question='Why?'))
            record_tool(self.service, owner, 'kanban_complete', {'summary': 'Context'},
                        {'ok': True, 'task_id': 't_context', 'run_id': 13}, 'owner-context')
            self.service.kanban.tasks[coordinator['id']]['parent_ids'].append('t_context')
            return coordinator, 't_context', 't_requester'

        for name, choose in (('another requester task', other_task), ('another execution of the same bot', other_run),
                             ('another child card', other_child), ('a context job', context_run)):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph(link=False)
                linker, parent, child = choose(owner, audit, coordinator)
                record_tool(self.service, linker, 'kanban_link', {'parent_id': parent, 'child_id': child},
                            {'ok': True, 'parent_id': parent, 'child_id': child}, linker['_native_turn_id'])
                owner = self.service._get('tasks', owner['id'])
                self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_an_explicit_completion_of_another_card_is_not_an_owner_run(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph(link=False)
        # Another execution owns this card on the board.
        other = dict(owner, id=str(uuid4()), _kanban_claim={'native_id': 't_foreign', 'input': 'foreign'})
        other['_execution_session_id'] = other['id']
        self.service._put('tasks', other)
        self.service.kanban.tasks[other['id']] = dict(external_id=other['id'], id='t_foreign', status='done',
                                                      current_run_id=None, worker_pid=None)
        # A turn without a claim has no worker task scope. Hermes then completes any named card and echoes its ID.
        record_tool(self.service, owner, 'kanban_complete', {'task_id': 't_foreign', 'summary': 'Done'},
                    {'ok': True, 'task_id': 't_foreign', 'run_id': 14}, 'unclaimed-turn')
        # The Work's task waits for that card. Everything else qualifies.
        record_tool(self.service, coordinator, 'kanban_link', {'parent_id': 't_foreign', 'child_id': 't_requester'},
                    {'ok': True, 'parent_id': 't_foreign', 'child_id': 't_requester', 'gated': False},
                    'coordinator-turn')
        self.service.kanban.tasks[coordinator['id']]['parent_ids'].append('t_foreign')
        owner = self.service._get('tasks', owner['id'])
        self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def board(self, after_fetch=None):
        """A board behind the scheduler cache. Like KanbanScheduler.call, a snapshot replaces the whole cache.

        after_fetch(cache) can then replace that cache, as a periodic snapshot that lands before the retry does.
        """
        board = copy.deepcopy(self.service.kanban.tasks)
        requests = []

        def call(request):
            self.assertEqual(request['op'], 'snapshot')
            requests.append(sorted(request.get('external_ids') or ()))
            self.service.kanban.tasks = copy.deepcopy(board)
            if after_fetch:
                self.service.kanban.tasks = after_fetch(copy.deepcopy(board))
            return {'tasks': [dict(row, external_id=identity) for identity, row in board.items()]}

        self.service.kanban.call.side_effect = call
        return board, requests

    def test_an_unlink_after_the_cached_snapshot_rejects_the_continuation(self):
        for path in ('emit', 'provider'):
            with self.subTest(path):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                board, requests = self.board()
                # The edge leaves the board. The service cache still holds the snapshot from before the unlink.
                board[coordinator['id']]['parent_ids'] = ['t_assignment']
                self.assertIn('t_earlier', self.service.kanban.tasks[coordinator['id']]['parent_ids'])
                caller = self.owner_caller(owner)
                if path == 'emit':
                    self.assert_rejected(owner, caller, UNRELATED)
                else:
                    receipt = self.through_provider(caller)
                    self.assertEqual(receipt['status'], 'rejected')
                    self.assertIn(UNRELATED, receipt['error'])
                    self.assertEqual(self.service._get('tasks', owner['id']), owner)
                self.assertEqual(len([r for r in requests if coordinator['id'] in r]), 1)
                self.assertNotIn('t_earlier', self.service.kanban.tasks[coordinator['id']]['parent_ids'])
                self.assertEqual(self.service._get('artifacts', pr['id']), pr)
                self.assertFalse([d for d in self.service._all('deliveries') if d['receipt'].get('native_task_id')])

    def test_each_request_reads_the_work_card_once(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        board, requests = self.board()
        board[coordinator['id']]['parent_ids'] = ['t_assignment']
        caller = self.owner_caller(owner)
        for _ in range(2):
            self.assert_rejected(owner, caller, UNRELATED)
        # One read per request, not a loop. The audit's card comes in the same read.
        self.assertEqual(len([r for r in requests if coordinator['id'] in r]), 2)
        self.assertTrue(all(audit['id'] in r for r in requests if coordinator['id'] in r))
        # The edge returns to the board. The next request reads it again and continues once.
        board[coordinator['id']]['parent_ids'] = ['t_assignment', 't_earlier']
        receipt = self.resume(caller)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(len([r for r in requests if coordinator['id'] in r]), 3)
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
        self.assertEqual(self.resume(caller), receipt)
        self.assertEqual(len([r for r in requests if coordinator['id'] in r]), 3)
        self.assertEqual(len([d for d in self.service._all('deliveries')
                              if d['receipt'].get('native_task_id') == 't_existing']), 1)

    def test_an_empty_cache_still_fits_the_retry_budget(self):
        for caller in ('owner', 'callback'):
            with self.subTest(caller):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                source = self.owner_caller(owner) if caller == 'owner' else self.callback(owner, audit)
                board, requests = self.board()
                self.service.kanban.tasks = {}
                self.assertEqual(self.resume(source)['task_id'], owner['id'])
                # The owner's lookup, then the Work's and the audit's cards in one read. Each read repeats earlier IDs.
                self.assertEqual(requests, [[owner['id']], sorted([owner['id'], audit['id'], coordinator['id']])])

    def test_eviction_and_newer_snapshots_keep_the_current_edge(self):
        def evict(identity):
            return lambda cache: {key: value for key, value in cache.items() if key != identity}

        def unlink(identity):
            def newer(cache):
                cache[identity]['parent_ids'] = ['t_assignment']
                return cache
            return newer

        # (card missing from the cache, board keeps the edge, cache after the read, expected result)
        cases = (
            ('evicted before the request', True, True, None, None),
            ('evicted after the read with the edge', False, True, 'evict', None),
            ('evicted after the read without the edge', False, False, 'evict', UNRELATED),
            ('a newer snapshot without the edge', False, True, 'unlink', UNRELATED),
        )
        for name, missing, edge, after, message in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                if missing:
                    self.service.kanban.tasks.pop(coordinator['id'])
                after_fetch = {'evict': evict, 'unlink': unlink}[after](coordinator['id']) if after else None
                board, requests = self.board(after_fetch)
                board[coordinator['id']] = board.get(coordinator['id']) or dict(
                    external_id=coordinator['id'], id='t_requester', status='done', block_kind=None,
                    current_run_id=None, worker_pid=None, parent_ids=['t_assignment', 't_earlier'])
                if not edge:
                    board[coordinator['id']]['parent_ids'] = ['t_assignment']
                caller = self.owner_caller(owner)
                if message:
                    self.assert_rejected(owner, caller, message)
                else:
                    self.assertEqual(self.resume(caller)['task_id'], owner['id'])
                    self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
                self.assertEqual(len([r for r in requests if coordinator['id'] in r]), 1)

    def test_failed_unknown_or_other_board_receipts_do_not_register(self):
        cases = (
            ('failed link', {'parent_id': 't_earlier', 'child_id': 't_requester'},
             {'ok': False, 'error': 'cycle'}, True),
            ('link of another board', {'parent_id': 't_earlier', 'child_id': 't_requester', 'board': 'other'},
             {'ok': True, 'parent_id': 't_earlier', 'child_id': 't_requester'}, True),
            ('mismatched result', {'parent_id': 't_earlier', 'child_id': 't_requester'},
             {'ok': True, 'parent_id': 't_other', 'child_id': 't_requester'}, True),
            ('unknown link effect', {'parent_id': 't_earlier', 'child_id': 't_requester'}, None, False),
        )
        for name, arguments, result, finish in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph(link=False)
                record_tool(self.service, coordinator, 'kanban_link', arguments, result, 'coordinator-turn',
                            finish=finish)
                self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_an_unrelated_work_keeps_its_requester_item(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        other = dict(work, id='unrelated-work', _native_outcome_id='t_unrelated')
        self.service._put('artifacts', other)
        self.service._put('artifacts', dict(pr, work_outcome_id=other['id']))
        self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_two_owner_prs_of_the_registered_work_stay_ambiguous(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        self.service._put('artifacts', dict(pr, id='second-delegated-pr', url=URL[:-1] + '9'))
        self.assert_rejected(owner, self.owner_caller(owner), 'Choose one retained Work item')

    def test_another_tenant_or_requester_cannot_use_the_registration(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        stranger = self.write('/v1/bots', {'name': 'Unrelated', 'role': ''})
        caller = self.write(f'/v1/bots/{stranger["id"]}/messages', {'text': 'Continue that PR.'})
        caller = dict(self.service._get('tasks', caller['id']), status='working')
        self.service._put('tasks', caller)
        self.assert_rejected(owner, caller, 'Only the owner or its requesting bot')
        # A Work of another bot cannot present this owner's PR, even with the same registration.
        for item in (work, requester_pr):
            self.service._put('artifacts', dict(item, bot_id=stranger['id']))
        self.service._put('tasks', dict(coordinator, bot_id=stranger['id']))
        self.assert_rejected(owner, self.owner_caller(owner), UNRELATED)

    def test_a_stale_owner_binding_keeps_its_guard(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        self.service.kanban.tasks[owner['id']].update(id='t_successor')
        with self.assertRaisesRegex(ServiceError, 'native outcome'):
            self.resume(self.owner_caller(owner))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)


    FINISHED_URL = 'https://github.com/example/project/pull/0'

    def finished_report(self, owner, requester, work, **changes):
        """An earlier merged delegated PR of the same Work and the requester's own report of it."""
        earlier = dict(id='earlier-pr', task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', title='Earlier fix',
                       status='merged', pr_state='merged', url=self.FINISHED_URL, work_outcome_id=work['id'],
                       _review_task_id=owner['id'], _activity_task_id=owner['id'])
        report = dict(id='requester-earlier-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
                      title='Earlier fix', status='merged', pr_state='merged', url=self.FINISHED_URL,
                      work_outcome_id=work['id'], _review_task_id=requester['id'], **changes)
        self.service._put('artifacts', earlier)
        self.service._put('artifacts', report)
        return earlier, report

    def requester_branch(self, requester, **changes):
        """Another branch of the requester, delegated through the real bot-message route."""
        docs = self.write('/v1/bots', {'name': 'Docs ' + str(uuid4())[:8]})
        working = dict(self.service._get('tasks', requester['id']))
        self.service._put('tasks', dict(working, status='working'))
        with self.service.transaction():
            receipt = self.service._store_bot_message(requester['id'], {'target_bot_id': docs['id'], 'text': 'Check the docs.'})
        self.service._put('tasks', dict(self.service._get('tasks', requester['id']), status=working['status']))
        sibling = dict(self.service._get('tasks', receipt['task_id']), **changes)
        self.service._put('tasks', sibling)
        return sibling

    def requester_cases(self, requester_id):
        def update(**changes):
            return lambda: self.service._put('tasks', dict(self.service._get('tasks', requester_id), **changes))
        return {
            'an active sibling branch': lambda: self.requester_branch(self.service._get('tasks', requester_id)),
            'a pending requester decision': lambda: self.service._put('decisions', dict(
                id='requester-question', task_id=requester_id, bot_id=self.service._get('tasks', requester_id)['bot_id'],
                status='pending', action_type='question', inbox_kind='decision', revision=1, title='Which list?')),
            'an unknown requester tool result': lambda: self.service._put('native_receipts', dict(
                id='requester-unknown', task_id=requester_id, turn_id='requester-turn', state='executing',
                action={'name': 'terminal', 'arguments': {'command': 'gh pr view'}}, action_hash='x', created_at='2026-10-20')),
            'an undelivered requester return': update(_return_pending={'target_bot_id': 'someone', 'text': 'Result'}),
            'a requester wait on other work': update(_waiting_on=['other-task']),
            'an unfinished sibling return': lambda: self.requester_branch(
                self.service._get('tasks', requester_id), status='completed',
                _return_pending={'target_bot_id': 'someone', 'text': 'Result'}),
            # Only the requester chain finds these: a return callback of the requester's assignment, not in its waits.
            'a queued requester callback': lambda: self.requester_callback(requester_id),
            'a requester callback with an unknown tool result': lambda: self.service._put('native_receipts', dict(
                id='callback-unknown', task_id=self.requester_callback(requester_id, status='completed')['id'],
                turn_id='callback-turn', state='executing', action={'name': 'terminal', 'arguments': {'command': 'make'}},
                action_hash='x', created_at='2026-10-20')),
        }

    def requester_callback(self, requester_id, status='queued'):
        """A return callback of the requester's assignment. The work chain links it through _assignment_task_id."""
        requester = self.service._get('tasks', requester_id)
        callback = dict(requester, id=str(uuid4()), status=status, _is_return=True, _assignment_task_id=requester_id,
                        _waiting_on=[])
        callback['_execution_session_id'] = callback['id']
        for key in ('_native_outcome_id', '_kanban_claim', '_work_context_id'):
            callback.pop(key, None)
        self.service._put('tasks', callback)
        return callback

    def test_a_registered_follow_up_continues_its_open_pr_past_an_earlier_finished_report(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        earlier, report = self.finished_report(owner, coordinator, work)
        before = {a['id']: a for a in self.service._all('artifacts')}
        receipt = self.resume(self.owner_caller(owner))
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
        after = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(set(after), set(before), 'No duplicate Work item')
        for identity in (work['id'], earlier['id'], report['id'], requester_pr['id']):
            self.assertEqual(after[identity], before[identity])

    def test_a_working_registered_work_task_stays_refused_with_or_without_a_finished_report(self):
        # The registered route has never admitted a working Work task as caller. The finished report changes nothing.
        for finished in (False, True):
            with self.subTest(finished=finished):
                try:
                    owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                    if finished:
                        self.finished_report(owner, coordinator, work)
                    coordinator = dict(coordinator, status='working')
                    self.service._put('tasks', coordinator)
                    self.assert_rejected(owner, coordinator, 'another execution')
                finally:
                    self.tearDown()
                    self.setUp()

    def test_a_registered_follow_up_keeps_the_requesters_other_work_and_identity_guards(self):
        for name in self.requester_cases('unused'):
            with self.subTest(case=name):
                try:
                    owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                    self.finished_report(owner, coordinator, work)
                    self.requester_cases(coordinator['id'])[name]()
                    self.assert_rejected(owner, self.owner_caller(owner), 'another execution')
                finally:
                    self.tearDown()
                    self.setUp()
        # The owner calls, so the working Work task is not a proven caller. Its finished report exempts nothing.
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        self.finished_report(owner, coordinator, work)
        self.service._put('tasks', dict(coordinator, status='working'))
        self.assert_rejected(owner, self.owner_caller(owner), 'another execution')
        self.tearDown()
        self.setUp()
        for name, message, change in (
                ('two open delegated PRs', 'Choose one retained Work item', lambda pr: self.service._put('artifacts', dict(
                    pr, id='second-delegated-pr', url=URL[:-1] + '9'))),
                ('a finished report of another Work', 'Choose one retained Work item|' + UNRELATED,
                 lambda pr: self.service._put('artifacts', dict(
                     self.service._get('artifacts', 'earlier-pr'), work_outcome_id='other-work')))):
            with self.subTest(case=name):
                try:
                    owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                    self.service._put('artifacts', dict(work, id='other-work', _native_outcome_id='t_other'))
                    self.finished_report(owner, coordinator, work)
                    change(pr)
                    self.assert_rejected(owner, self.owner_caller(owner), message)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_live_requester_tasks_keep_their_guards(self):
        cases = {
            # The Work's own task is parked on the board, as in the saved graph now.
            'scheduled work': (dict(settled=False), None, 'another execution'),
            'working audit': (dict(), lambda o, a, c: self.service._put('tasks', dict(a, status='working')), UNRELATED),
            'waiting audit': (dict(), lambda o, a, c: self.service._put('tasks', dict(a, _waiting_on=['helper'])),
                              UNRELATED),
            'working work task': (dict(), lambda o, a, c: self.service._put('tasks', dict(c, status='working')),
                                  'another execution'),
            'running work card': (dict(), lambda o, a, c: self.service.kanban.tasks[c['id']].update(
                status='running', current_run_id=22, worker_pid=202), 'another execution'),
        }
        for name, (options, change, message) in cases.items():
            for caller in ('owner', 'callback'):
                with self.subTest(name, caller=caller):
                    self.tearDown()
                    self.setUp()
                    owner, audit, coordinator, work, requester_pr, pr = self.saved_graph(**options)
                    source = self.owner_caller(owner) if caller == 'owner' else self.callback(owner, audit)
                    if change:
                        change(owner, self.service._get('tasks', audit['id']), coordinator)
                    self.assert_rejected(owner, source, message)

    def test_unconsumed_returns_keep_their_guards(self):
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        # A report that the audit has not consumed yet is a live requester branch.
        self.callback(owner, audit, status='queued')
        self.assert_rejected(owner, self.owner_caller(owner), 'another execution')
        self.tearDown()
        self.setUp()
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        self.service._put('tasks', dict(owner, _return_pending={'text': 'Undelivered'}))
        owner = self.service._get('tasks', owner['id'])
        self.assert_rejected(owner, self.owner_caller(owner), 'previous result')

    def test_pending_reviews_decisions_and_contexts_keep_their_guards(self):
        def decision(task):
            self.service._put('decisions', dict(id=str(uuid4()), task_id=task['id'], bot_id=task['bot_id'],
                                                status='pending', title='Publish?'))

        def reviewer(owner, audit, coordinator):
            # Another requester task still reviews another PR of the same Work.
            writer = dict(coordinator, id='other-reviewer', status='working', _execution_session_id='other-reviewer',
                          _native_outcome_id=None, _work_context_id=None, _kanban_claim=None)
            self.service._put('tasks', writer)
            self.service._put('artifacts', dict(id='other-pr', task_id=writer['id'], bot_id=writer['bot_id'],
                kind='pr', title='Another PR', status='open', url=URL[:-1] + '7', work_outcome_id='requester-work',
                _review_task_id=writer['id']))

        cases = (
            ('owner decision', lambda o, a, c: decision(o), 'existing decision'),
            ('audit decision', lambda o, a, c: decision(a), UNRELATED),
            ('work task decision', lambda o, a, c: decision(c), 'another execution'),
            ('work task context', lambda o, a, c: self.service._put('contexts', dict(
                id='open-context', task_id=c['id'], bot_id=c['bot_id'], status='working', question='Why?')),
             'another execution'),
            ('pending review', reviewer, 'another execution'),
            ('requester pause', lambda o, a, c: (
                self.service._put('artifacts', dict(self.service._get('artifacts', 'requester-work'),
                                                    paused_by_decision_id='held')),
                self.service._put('decisions', dict(id='held', task_id=c['id'], status='rejected',
                                                    decided_by='human'))), 'paused'),
        )
        for name, change, message in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                change(owner, audit, coordinator)
                self.assert_rejected(owner, self.owner_caller(owner), message)

    def test_unknown_tool_effects_keep_their_guards(self):
        for name in ('audit', 'coordinator'):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
                task = audit if name == 'audit' else coordinator
                record_tool(self.service, task, 'browser_navigate', {'url': URL}, None, task['_native_turn_id'],
                            finish=False)
                self.assert_rejected(owner, self.owner_caller(owner),
                                     UNRELATED if name == 'audit' else 'another execution')
        # The owner's own unknown effect stays in its recovery for the next turn.
        self.tearDown()
        self.setUp()
        owner, audit, coordinator, work, requester_pr, pr = self.saved_graph()
        unknown = record_tool(self.service, owner, 'terminal', {'command': 'git push'}, None, 'completed-input',
                              finish=False)
        queued = self.assert_admitted(owner, self.owner_caller(owner), (pr, work, requester_pr))
        self.assertIn(unknown, [r['id'] for r in queued['_self_recovery']['unknown_tools']])
        self.assertEqual(self.service._get('native_receipts', unknown)['state'], 'executing')


@unittest.skipUnless(native_fixtures.fixtures.SOURCE and native_fixtures.fixtures.PYTHON,
                     'Set Coconut Hermes paths for native integration checks.')
class NativeRegisteredDelegationTests(unittest.TestCase):
    setUp = native_fixtures.NativeClaimContinuationTests.setUp
    write = native_fixtures.NativeClaimContinuationTests.write
    native_program = native_fixtures.NativeClaimContinuationTests.native_program
    prepare = native_fixtures.NativeClaimContinuationTests.prepare
    resume = native_fixtures.NativeClaimContinuationTests.resume
    check_dispatch = native_fixtures.NativeClaimContinuationTests.check_dispatch
    review_evidence = native_fixtures.NativeClaimContinuationTests.review_evidence

    def registered(self, settled=True, edge=True, unlink_after_snapshot=False):
        prepare = self.prepare
        saved = {}

        def prepare_registration(*args):
            owner, source, identity, workspace = prepare(*args)
            audit = self.service._get('tasks', owner['source_task_id'])
            self.service._put('tasks', dict(audit, _waiting_on=[]))
            # The owner's run completed its own card.
            record_tool(self.service, owner, 'kanban_complete', {'summary': 'Original input complete'},
                        {'ok': True, 'task_id': identity, 'run_id': 1}, owner['_kanban_claim']['input'])
            with self.service.transaction():
                exchange = self.service._new_exchange()
            coordinator = self.write('/v1/bots/' + audit['bot_id'] + '/messages', {'text': 'Reconcile the PR.'})
            coordinator = dict(self.service._get('tasks', coordinator['id']), exchange_id=exchange,
                               _conversation_epoch=self.service._setting('conversation_epoch'))
            coordinator['_execution_session_id'] = coordinator['id']
            row = self.service._kanban_row(coordinator, coordinator, 'ready', 10)
            self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
            self.service.kanban.call({'op': 'snapshot'})
            card = self.service.kanban.tasks[coordinator['id']]['id']
            self.native_program('board.kb.link_tasks(board.conn, parent_id=sys.argv[2], child_id=sys.argv[3])',
                                identity, card)
            # A settled Work finished. A live one is parked on its card, as in the saved graph now.
            operation = "complete_task(board.conn, sys.argv[2], summary='Registered')" if settled else (
                "schedule_task(board.conn, sys.argv[2], reason='Waiting for the delegated PR')")
            self.native_program('assert board.kb.' + operation, card)
            if not edge:
                self.native_program('assert board.kb.unlink_tasks(board.conn, sys.argv[2], sys.argv[3])', identity, card)
            coordinator.update(status='completed', _native_outcome_id=card, _work_context_id='requester-work',
                               _kanban_claim=dict(native_id=card, external_id=coordinator['id'],
                                                  input=row['input'], run_id=2))
            self.service._put('tasks', coordinator)
            record_tool(self.service, coordinator, 'kanban_link', {'parent_id': identity, 'child_id': card},
                        {'ok': True, 'parent_id': identity, 'child_id': card, 'gated': False}, row['input'])
            self.service.kanban.call({'op': 'snapshot'})
            if unlink_after_snapshot:
                # The board loses the edge after the service cached it. Nothing refreshes the cache.
                self.assertIn(identity, self.service.kanban.tasks[coordinator['id']]['parent_ids'])
                self.native_program('assert board.kb.unlink_tasks(board.conn, sys.argv[2], sys.argv[3])', identity, card)
                self.assertIn(identity, self.service.kanban.tasks[coordinator['id']]['parent_ids'])
            work = dict(id='requester-work', task_id=coordinator['id'], bot_id=coordinator['bot_id'], kind='work',
                        title='The request', status='waiting', created_at=coordinator['created_at'],
                        _native_outcome_id=card, _work_key='retained-pr:' + URL)
            requester_pr = dict(id=str(uuid4()), task_id=coordinator['id'], bot_id=coordinator['bot_id'], kind='pr',
                                title='The delegated PR', status='preparing', pr_state='open', url=URL,
                                created_at=coordinator['created_at'], work_outcome_id=work['id'],
                                _review_task_id=coordinator['id'], _activity_task_id=coordinator['id'])
            pr = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='pr', title='Draft correction',
                      status='preparing', pr_state='open', url=URL, created_at=owner['created_at'],
                      work_outcome_id=work['id'], _review_task_id=owner['id'])
            for item in (work, requester_pr, pr):
                self.service._put('artifacts', item)
            self.assertNotEqual(work['task_id'], owner['source_task_id'])
            saved.update(work=work, requester_pr=requester_pr, pr=pr, card=card, coordinator=coordinator['id'])
            return self.service._get('tasks', owner['id']), source, identity, workspace

        self.prepare = prepare_registration
        return saved

    def assert_presentation_kept(self, saved):
        for item in (saved['work'], saved['requester_pr']):
            current = self.service._get('artifacts', item['id'])
            for key in ('task_id', 'bot_id', 'kind', 'url', 'work_outcome_id', '_native_outcome_id'):
                self.assertEqual(current.get(key), item.get(key), key)
        self.assertEqual(self.service._get('artifacts', saved['pr']['id'])['work_outcome_id'], saved['work']['id'])

    def test_a_requester_dispatches_the_registered_pr_once(self):
        saved = self.registered()
        self.check_dispatch('done')
        owner = self.service._get('tasks', saved['pr']['task_id'])
        self.assertEqual(owner['_work_context_id'], saved['pr']['id'])
        self.assert_presentation_kept(saved)

    def test_the_owner_dispatches_the_registered_pr_once(self):
        saved = self.registered()
        self.check_dispatch('done', 'owner')
        self.assertEqual(self.service._get('tasks', saved['pr']['task_id'])['_work_context_id'], saved['pr']['id'])
        self.assert_presentation_kept(saved)

    def check_rejected(self, saved, message):
        owner, source, identity, workspace = self.prepare('done')
        artifacts = self.service._all('artifacts')
        runs = self.native_program('print(board.conn.execute("SELECT count(*) FROM task_runs").fetchone()[0])')
        with self.assertRaisesRegex(ServiceError, message):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._all('artifacts'), artifacts)
        self.assertFalse([d for d in self.service._all('deliveries') if d['receipt'].get('native_task_id')])
        self.assertEqual(self.native_program(
            'print(board.conn.execute("SELECT count(*) FROM task_runs").fetchone()[0])'), runs)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')
        self.assertEqual((workspace / 'pending.txt').read_text(), 'Uncommitted correction')

    def test_an_unlinked_registration_keeps_the_requester_work(self):
        self.check_rejected(self.registered(edge=False), UNRELATED)

    def test_an_unlink_after_the_cached_snapshot_keeps_the_requester_work(self):
        saved = self.registered(unlink_after_snapshot=True)
        self.check_rejected(saved, UNRELATED)
        # The request read the Work's card from the board, so the cache now has no edge either.
        self.assertEqual(self.service.kanban.tasks[saved['coordinator']]['parent_ids'], [])

    def test_a_live_registered_work_keeps_the_writer_guard(self):
        self.check_rejected(self.registered(settled=False), 'another execution')


if __name__ == '__main__':
    unittest.main()
