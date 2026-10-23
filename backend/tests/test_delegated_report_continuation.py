"""A delegate's hidden Work report and its delegated PR retain one execution and one outcome."""
import copy
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from coconut.service import ServiceError
import test_native_claim_continuation as native_fixtures
import test_native_work_continuation as fixtures
import test_requester_callback_continuation as callback_fixtures

URL = 'https://github.com/example/project/pull/1'


def saved_report(owner, work, **changes):
    # The fields of the saved hidden report, as _sync_delegated_work and _register_work wrote them.
    return dict(id='delegate-report', task_id=owner['id'], bot_id=owner['bot_id'], kind='work',
        title='The request', status='working', activity='Checking a delivery failure',
        preview='', url=None, created_at='2026-10-15T21:29:53.601827Z', _automatic_work=False,
        _hidden=True, _same_outcome_as=work['id'], _work_parent_id=work['id'],
        _work_parent_task_id=work['task_id'], _work_report_task_id=owner['id'],
        _work_report_turn=owner.get('_native_turn_id'), _work_key='native-t_requester') | changes


class DelegatedReportContinuationTests(unittest.TestCase):
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    resume = fixtures.NativeWorkContinuationTests.resume
    linked_requester_pr = fixtures.NativeWorkContinuationTests.linked_requester_pr
    audited_requester_pr = fixtures.NativeWorkContinuationTests.audited_requester_pr

    def saved_linkage(self, report_id='delegate-report'):
        owner, source, work, pr = self.linked_requester_pr()
        report = saved_report(owner, work, id=report_id)
        # The requester's own report of the same PR. Its review task is the requester.
        requester_pr = dict(id='requester-pr', task_id=source['id'], bot_id=source['bot_id'],
            kind='pr', title='The delegated PR', status='preparing', pr_state='open', url=URL,
            work_outcome_id=work['id'], _review_task_id=source['id'])
        self.service._put('artifacts', report)
        self.service._put('artifacts', requester_pr)
        return owner, source, work, pr, report, requester_pr

    def assert_rejected(self, owner, source, message):
        artifacts = self.service._all('artifacts')
        with self.assertRaisesRegex(ServiceError, message):
            self.resume(source)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._all('artifacts'), artifacts)
        self.assertFalse([d for d in self.service._all('deliveries') if d.get('target_bot_id') == owner['bot_id']
                          and d.get('receipt', {}).get('native_task_id')])

    def test_the_saved_report_and_pr_continue_one_execution_once(self):
        # The report's ID sorts before and after the PR, so artifact order cannot choose.
        for report_id in ('a-delegate-report', 'z-delegate-report'):
            with self.subTest(report_id=report_id):
                self.tearDown()
                self.setUp()
                owner, source, work, pr, report, requester_pr = self.saved_linkage(report_id)
                receipt = self.resume(source)
                continued = self.service._get('tasks', owner['id'])
                self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
                for key in ('id', '_execution_session_id', 'thread_id', 'session_id', 'exchange_id',
                            'source_task_id', 'source_bot_id', '_reply_to_task_id'):
                    self.assertEqual(continued.get(key), owner.get(key), key)
                self.assertEqual(continued['_work_context_id'], pr['id'])
                self.assertEqual(continued['status'], 'queued')
                self.assertEqual(self.service._get('artifacts', pr['id']), dict(pr, _native_outcome_id='t_existing'))
                for item in (work, report, requester_pr):
                    self.assertEqual(self.service._get('artifacts', item['id']), item)
                self.assertEqual(self.resume(source), receipt)
                self.assertEqual(len([d for d in self.service._all('deliveries')
                                      if d.get('receipt') == receipt]), 1)
                self.assertEqual(self.service._get('tasks', owner['id']), continued)

    def test_the_owner_continues_its_saved_report_and_pr(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        caller = self.write(f'/v1/bots/{owner["bot_id"]}/messages', {'text': 'Continue the PR.'})
        caller = dict(self.service._get('tasks', caller['id']), status='working')
        self.service._put('tasks', caller)
        receipt = self.resume(caller)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
        self.assertEqual(self.service._get('artifacts', report['id']), report)

    def test_an_audited_delegation_folds_the_report_into_its_pr(self):
        owner, coordinator, work, pr = self.audited_requester_pr()
        report = saved_report(owner, work)
        self.service._put('artifacts', report)
        receipt = self.resume(coordinator)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
        self.assertEqual(self.service._get('artifacts', report['id']), report)
        self.assertEqual(self.service._get('artifacts', work['id']), work)


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

    def test_an_audited_follow_up_continues_its_open_pr_past_an_earlier_finished_report(self):
        owner, coordinator, work, pr = self.audited_requester_pr()
        earlier, report = self.finished_report(owner, coordinator, work)
        before = {a['id']: a for a in self.service._all('artifacts')}
        receipt = self.resume(coordinator)
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], pr['id'])
        after = {a['id']: a for a in self.service._all('artifacts')}
        self.assertEqual(set(after), set(before), 'No duplicate Work item')
        self.assertEqual(after[pr['id']], dict(pr, _native_outcome_id='t_existing'))
        for identity in (work['id'], earlier['id'], report['id']):
            self.assertEqual(after[identity], before[identity])
        self.assertEqual(self.service._get('tasks', coordinator['id'])['status'], 'working')

    def test_an_audited_follow_up_keeps_the_requesters_other_work_and_identity_guards(self):
        cases = {name: ('another execution', change) for name, change in self.requester_cases('coordinator').items()}
        cases.update({
            'an unaudited source': ('belongs to another native outcome', None),
            'two open delegated PRs': ('Choose one retained Work item', lambda: self.service._put('artifacts', dict(
                self.service._get('artifacts', 'delegated-pr'), id='second-delegated-pr', url=URL[:-1] + '2'))),
            'a finished report of another Work': ('Choose one retained Work item', lambda: self.service._put('artifacts', dict(
                self.service._get('artifacts', 'earlier-pr'), work_outcome_id='other-work'))),
        })
        for name, (message, change) in cases.items():
            with self.subTest(case=name):
                try:
                    owner, coordinator, work, pr = self.audited_requester_pr(audited=name != 'an unaudited source')
                    self.service._put('artifacts', dict(work, id='other-work', _native_outcome_id='t_other'))
                    self.finished_report(owner, coordinator, work)
                    if change:
                        change()
                    self.assert_rejected(owner, self.service._get('tasks', coordinator['id']), message)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_an_audited_follow_up_needs_the_works_own_task_as_caller(self):
        owner, coordinator, work, pr = self.audited_requester_pr()
        self.finished_report(owner, coordinator, work)
        other = dict(coordinator, id='other-requester-task', _execution_session_id='other-requester-task',
                     _work_context_id=None, _native_outcome_id=None)
        self.service._put('tasks', other)
        self.assert_rejected(owner, other, 'belongs to another native outcome|another execution')

    def test_an_unaudited_requester_work_still_rejects_the_report(self):
        owner, coordinator, work, pr = self.audited_requester_pr(audited=False)
        self.service._put('artifacts', saved_report(owner, work))
        self.assert_rejected(owner, coordinator, 'belongs to another native outcome')

    def test_two_delegated_prs_of_the_same_work_stay_ambiguous(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        self.service._put('artifacts', dict(pr, id='second-delegated-pr', url=URL[:-1] + '2'))
        self.assert_rejected(owner, source, 'Choose one retained Work item')

    def test_a_second_pr_without_the_report_stays_ambiguous(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        self.service.db.execute('DELETE FROM artifacts WHERE id=?', (report['id'],))
        self.service._put('artifacts', dict(pr, id='second-delegated-pr', url=URL[:-1] + '2'))
        self.assert_rejected(owner, source, 'Choose one retained Work item')

    def test_a_report_of_an_unrelated_work_stays_ambiguous(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        other = dict(work, id='other-requester-work', _native_outcome_id='t_other')
        self.service._put('artifacts', other)
        self.service._put('artifacts', saved_report(owner, other))
        self.assert_rejected(owner, source, 'Choose one retained Work item')

    def test_a_report_without_saved_provenance_stays_ambiguous(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        for field, value in (('_work_report_task_id', None), ('_work_report_task_id', source['id']),
                             ('_work_parent_task_id', None), ('_work_parent_task_id', owner['id']),
                             ('_same_outcome_as', 'requester-pr'), ('_native_outcome_id', 't_other'),
                             ('_hidden', False), ('bot_id', source['bot_id'])):
            with self.subTest(field=field, value=value):
                changed = dict(report)
                if value is None:
                    changed.pop(field)
                else:
                    changed[field] = value
                self.service._put('artifacts', changed)
                self.assert_rejected(owner, source, 'Choose one retained Work item|belongs to another native outcome')
        self.service._put('artifacts', report)

    def test_a_report_whose_work_task_is_missing_stays_ambiguous(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        missing = dict(work, id='missing-owner-work', task_id='missing-task')
        self.service._put('artifacts', missing)
        self.service._put('artifacts', dict(pr, work_outcome_id=missing['id']))
        self.service._put('artifacts', dict(report, _same_outcome_as=missing['id'], _work_parent_id=missing['id'],
                                            _work_parent_task_id='missing-task'))
        self.assert_rejected(owner, source, 'Choose one retained Work item|belongs to another native outcome')

    def test_a_report_without_a_delegated_pr_keeps_the_requester_work(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        self.service.db.execute('DELETE FROM artifacts WHERE id=?', (pr['id'],))
        self.assert_rejected(owner, source, 'belongs to another native outcome')

    def test_the_report_keeps_every_continuation_guard(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        cases = (
            ('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'], status='pending',
                               title='Publish?'), 'existing decision'),
            ('tasks', dict(owner, _return_pending={'text': 'Undelivered'}), 'previous result'),
            ('tasks', dict(owner, _waiting_on=['helper']), 'existing dependencies'),
        )
        for table, value, message in cases:
            with self.subTest(message=message):
                saved = self.service._get(table, value['id']) if table == 'tasks' else None
                self.service._put(table, value)
                with self.assertRaisesRegex(ServiceError, message):
                    self.resume(source)
                if saved:
                    self.service._put(table, saved)
                else:
                    self.service.db.execute(f'DELETE FROM {table} WHERE id=?', (value['id'],))
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('artifacts', report['id']), report)

    def test_the_report_keeps_the_requester_pause_and_cancellation(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        self.service._put('artifacts', dict(work, paused_by_decision_id='held'))
        self.service._put('decisions', dict(id='held', task_id=source['id'], status='rejected', decided_by='human'))
        self.assert_rejected(owner, source, 'paused, cancelled')
        self.service._put('artifacts', work)
        self.service.db.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                                ('work.cancelled.work:' + work['id'], json.dumps('cancelled')))
        self.assert_rejected(owner, source, 'paused or cancelled')

    def test_the_report_keeps_the_writer_and_review_guards(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        writer = dict(self.service._get('tasks', source['id']), id='other-requester-writer',
                      status='working', _execution_session_id='other-requester-writer')
        self.service._put('tasks', writer)
        # Another active requester task reports activity on the same PR.
        self.service._put('artifacts', dict(requester_pr, _activity_task_id=writer['id']))
        self.assert_rejected(owner, source, 'another execution')
        self.service._put('artifacts', requester_pr)
        # Another active task reviews another PR of the same Work.
        self.service._put('artifacts', dict(id='other-pr', task_id=writer['id'], bot_id=source['bot_id'],
            kind='pr', title='Another PR', status='open', url=URL[:-1] + '3', work_outcome_id=work['id'],
            _review_task_id=writer['id']))
        self.assert_rejected(owner, source, 'another execution')

    def test_the_report_keeps_the_unknown_tool_guard_of_a_peer(self):
        owner, source, work, pr, report, requester_pr = self.saved_linkage()
        reporter = dict(self.service._get('tasks', source['id']), id='requester-reporter', status='failed',
                        _execution_session_id='requester-reporter')
        self.service._put('tasks', reporter)
        self.service._put('native_receipts', dict(id=str(uuid4()), task_id=reporter['id'], turn_id='turn',
            state='executing', action=dict(name='browser_navigate', arguments={}), created_at=reporter['created_at']))
        self.service._put('artifacts', dict(requester_pr, _activity_task_id=reporter['id']))
        self.assert_rejected(owner, source, 'another execution')


class LinkedRequesterActivityTests(unittest.TestCase):
    """The direct requester reports activity on the delegated PR itself, as in PR 8606.

    Only the requester's own current native turn is exempt as the caller. Every other guard stays.
    """
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    resume = fixtures.NativeWorkContinuationTests.resume
    linked_requester_pr = fixtures.NativeWorkContinuationTests.linked_requester_pr
    assert_rejected = DelegatedReportContinuationTests.assert_rejected
    requester_branch = DelegatedReportContinuationTests.requester_branch
    requester_cases = DelegatedReportContinuationTests.requester_cases
    requester_callback = DelegatedReportContinuationTests.requester_callback
    through_provider = callback_fixtures.RequesterCallbackContinuationTests.through_provider

    def run_turn(self, task, native_id, turn):
        # The task's current turn, running on its own native claim.
        task.update(status='working', _waiting_on=[], _native_turn_id=turn,
                    _conversation_epoch=self.service._setting('conversation_epoch'),
                    _kanban_claim=dict(native_id=native_id, external_id=task['id'], input=turn,
                                       run_id=71, pid=101, context=''))
        self.service._put('tasks', task)
        self.service.running[task['id']] = Mock(is_alive=Mock(return_value=True))
        self.service.kanban.tasks[task['id']] = dict(id=native_id, status='running', current_run_id=71,
                                                     worker_pid=101, input_id=turn)
        return task

    def requester_activity(self):
        owner, requester, work, pr = self.linked_requester_pr()
        # An earlier helper of the requester finished, and the requester consumed its result.
        helper = self.requester_branch(requester, status='completed',
                                       _conversation_epoch=self.service._setting('conversation_epoch'),
                                       _kanban_claim=dict(native_id='t_helper', input='helper'))
        self.service.kanban.tasks[helper['id']] = dict(id='t_helper', status='done')
        requester = self.run_turn(self.service._get('tasks', requester['id']), work['_native_outcome_id'], 'requester-turn')
        report = dict(id='requester-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
                      title='The delegated PR', status='open', pr_state='open', url=pr['url'].upper(),
                      work_outcome_id=work['id'], _review_task_id=requester['id'], _activity_task_id=requester['id'])
        self.service._put('artifacts', report)
        return owner, requester, work, pr, report, helper

    def assert_admitted_once(self, owner, pr, receipt, repeat):
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        continued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', 'thread_id', 'session_id', 'exchange_id',
                    'source_task_id', 'source_bot_id', '_reply_to_task_id', '_kanban_claim'):
            self.assertEqual(continued.get(key), owner.get(key), key)
        self.assertEqual(continued['_work_context_id'], pr['id'])
        self.assertEqual(self.service._get('artifacts', pr['id']), dict(pr, _native_outcome_id='t_existing'))
        self.assertEqual(repeat(), receipt)
        self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == receipt]), 1)

    def each(self, cases, check):
        for name, case in cases.items():
            with self.subTest(case=name):
                try:
                    check(case)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_the_requester_continues_its_delegated_pr_from_its_own_native_turn(self):
        owner, requester, work, pr, report, helper = self.requester_activity()
        tasks = {t['id']: t for t in self.service._all('tasks') if t['id'] != owner['id']}
        artifacts = {a['id']: a for a in self.service._all('artifacts') if a['id'] != pr['id']}
        cards = copy.deepcopy(self.service.kanban.tasks)
        receipt = self.resume(requester)
        self.assert_admitted_once(owner, pr, receipt, lambda: self.resume(requester))
        self.assertEqual({t['id']: t for t in self.service._all('tasks') if t['id'] != owner['id']}, tasks)
        self.assertEqual({a['id']: a for a in self.service._all('artifacts') if a['id'] != pr['id']}, artifacts)
        self.assertEqual(self.service.kanban.tasks, cards)

    def test_the_provider_route_admits_the_requester_once(self):
        owner, requester, work, pr, report, helper = self.requester_activity()
        receipt = self.through_provider(requester, twice=True)
        # The provider repeated its call in the same turn and got the saved receipt.
        self.assert_admitted_once(owner, pr, receipt, lambda: receipt)
        self.assertEqual(self.service._get('artifacts', report['id']), report)

    def test_the_owner_continues_only_after_the_requester_turn_ends(self):
        owner, requester, work, pr, report, helper = self.requester_activity()
        caller = self.write(f'/v1/bots/{owner["bot_id"]}/messages', {'text': 'Continue the PR.'})
        caller = dict(self.service._get('tasks', caller['id']), status='working')
        self.service._put('tasks', caller)
        # The requester's running turn reports on the same PR. The owner's call does not exempt it.
        self.assert_rejected(owner, caller, 'another execution')
        self.service._put('tasks', dict(requester, status='completed'))
        self.service.running.pop(requester['id'])
        self.service.kanban.tasks[requester['id']] = dict(id=work['_native_outcome_id'], status='done')
        self.assert_admitted_once(owner, pr, self.resume(caller), lambda: self.resume(caller))

    def test_a_review_needs_no_turn_but_activity_needs_the_requesters_own_turn(self):
        owner, requester, work, pr, report, helper = self.requester_activity()
        # A local turn holds no native claim, so its activity stays a writer (see PR 45).
        local = dict(requester, _native_turn_id=None, _kanban_claim=None)
        self.service._put('tasks', local)
        self.assert_rejected(owner, local, 'another execution')
        review = dict(report)
        review.pop('_activity_task_id')
        self.service._put('artifacts', review)
        self.assertEqual(self.resume(local)['task_id'], owner['id'])

    def test_another_claim_or_turn_of_the_requester_is_not_its_foreground(self):
        def card(**changes):
            return lambda r: self.service.kanban.tasks[r['id']].update(changes)

        def claim(**changes):
            return lambda r: self.service._put('tasks', dict(r, _kanban_claim=dict(r['_kanban_claim'], **changes)))

        def task(**changes):
            return lambda r: self.service._put('tasks', dict(r, **changes))

        cases = {
            'a scheduled card': card(status='scheduled', current_run_id=None, worker_pid=None),
            'a reviewer run': card(claim_source_status='review'),
            'another run': card(current_run_id=72),
            'another worker': card(worker_pid=202),
            'another card': card(id='t_other'),
            "another task's claim": claim(external_id='other-task'),
            'a claim without a run': claim(run_id=None),
            'an earlier conversation': task(_conversation_epoch='earlier'),
            'another session': task(session_id='other-session'),
        }

        def check(change):
            owner, requester, work, pr, report, helper = self.requester_activity()
            change(requester)
            self.assert_rejected(owner, self.service._get('tasks', requester['id']), 'another execution|cannot continue')
        self.each(cases, check)

    def test_other_reports_and_callers_stay_writers(self):
        def other_turn(owner, requester, work, pr, report):
            # Another requester task in its own execution makes the call. The requester's turn stays a writer.
            other = dict(requester, id=str(uuid4()), _execution_session_id=None, _work_context_id=None)
            other['_execution_session_id'] = other['id']
            other = self.run_turn(other, 't_other_turn', 'other-turn')
            return other

        def other_reporter(bot_name):
            def change(owner, requester, work, pr, report):
                bot = self.write('/v1/bots', {'name': bot_name}) if bot_name else self.service._get('bots', requester['bot_id'])
                writer = self.write(f'/v1/bots/{bot["id"]}/messages', {'text': 'Update the PR.'})
                writer = self.run_turn(self.service._get('tasks', writer['id']), 't_writer', 'writer-turn')
                self.service._put('artifacts', dict(report, _activity_task_id=writer['id']))
                return requester
            return change

        def report(**changes):
            def change(owner, requester, work, pr, report):
                self.service._put('artifacts', dict(report, **changes))
                return requester
            return change

        def aliased_work(owner, requester, work, pr, report):
            alias = dict(work, id='work-alias', _same_outcome_as=work['id'])
            self.service._put('artifacts', alias)
            self.service._put('artifacts', dict(report, work_outcome_id=alias['id']))
            return requester

        def requester_task(owner, requester, work, pr, report):
            # The report names another task of the requester, outside the requester's execution.
            other = other_turn(owner, requester, work, pr, report)
            self.service._put('artifacts', dict(report, task_id=other['id']))
            return requester

        cases = {
            'another PR of the Work': report(url=URL[:-1] + '2'),
            'another Work of the same outcome': aliased_work,
            'another task of the requester': requester_task,
            'another bot as the reporter': report(bot_id='other-bot'),
            'a writer of the requester bot': other_reporter(None),
            'a writer of a foreign bot': other_reporter('Foreign ' + str(uuid4())[:8]),
            'another requester turn as the caller': other_turn,
        }

        def check(change):
            owner, requester, work, pr, report = self.requester_activity()[:5]
            caller = change(owner, requester, work, pr, report)
            self.assert_rejected(owner, self.service._get('tasks', caller['id']), 'another execution')
        self.each(cases, check)

    def test_the_requesters_other_work_and_waits_stay_guarded(self):
        def cases(requester, helper):
            def update(task, **changes):
                return lambda: self.service._put('tasks', dict(task, **changes))

            def helper_running():
                # The current helper is live, and the requester waits for it.
                self.service._put('tasks', dict(helper, status='working'))
                self.service.kanban.tasks[helper['id']] = dict(id='t_helper', status='running', current_run_id=81)
                update(requester, _waiting_on=[helper['id']])()
            found = self.requester_cases(requester['id'])
            found.update({
                'a live helper that the requester waits for': helper_running,
                'an open requester context': lambda: self.service._put('contexts', dict(
                    id='requester-context', task_id=requester['id'], bot_id=requester['bot_id'], status='working')),
                'requester steering': update(requester, _steering=[{'text': 'Wait.'}]),
                'requester live messages': update(requester, _live_messages=[{'text': 'Wait.'}]),
                'a blocked requester follow-up': update(requester, _blocked_followup={'source_ids': [], 'attachment_ids': []}),
                'an undelivered requester reply': update(requester, _native_reply_pending=True),
            })
            return found

        def check(name):
            owner, requester, work, pr, report, helper = self.requester_activity()
            cases(requester, helper)[name]()
            self.assert_rejected(owner, self.service._get('tasks', requester['id']), 'another execution')
        owner, requester, work, pr, report, helper = self.requester_activity()
        names = list(cases(requester, helper))
        self.tearDown()
        self.setUp()
        self.assertEqual(len(names), 14)
        self.each({name: name for name in names}, check)


@unittest.skipUnless(native_fixtures.fixtures.SOURCE and native_fixtures.fixtures.PYTHON,
                     'Set Coconut Hermes paths for native integration checks.')
class NativeDelegatedReportContinuationTests(unittest.TestCase):
    setUp = native_fixtures.NativeClaimContinuationTests.setUp
    write = native_fixtures.NativeClaimContinuationTests.write
    native_program = native_fixtures.NativeClaimContinuationTests.native_program
    prepare = native_fixtures.NativeClaimContinuationTests.prepare
    resume = native_fixtures.NativeClaimContinuationTests.resume
    check_dispatch = native_fixtures.NativeClaimContinuationTests.check_dispatch

    def requester_turn(self, requester):
        """The direct requester's own turn, running on a real native claim."""
        import os
        requester.update(_execution_session_id=requester['id'], _waiting_on=[], _native_turn_id='requester-input',
                         _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', requester)
        row = self.service._kanban_row(requester, requester, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[requester['id']]['id']
        self.native_program("assert board.kb.claim_task(board.conn, sys.argv[2])\n"
                            "board.dispatch._set_worker_pid(board.conn, sys.argv[2], int(sys.argv[3]))",
                            native, str(os.getpid()))
        self.service.kanban.call({'op': 'snapshot'})
        card = self.service.kanban.tasks[requester['id']]
        self.assertEqual(card['status'], 'running')
        requester.update(status='working', _kanban_claim=dict(native_id=native, external_id=requester['id'],
            input=row['input'], run_id=card['current_run_id'], pid=os.getpid(), context=''))
        self.service._put('tasks', requester)
        return requester

    def linked(self, second_pr=False, requester_turn=False):
        prepare = self.prepare
        saved = []

        def prepare_linkage(*args):
            owner, source, identity, workspace = prepare(*args)
            requester = self.service._get('tasks', owner['source_task_id'])
            work = dict(id=str(uuid4()), task_id=requester['id'], bot_id=requester['bot_id'], kind='work',
                title='The request', status='waiting', created_at=requester['created_at'],
                _native_outcome_id='t_requester', _work_key='native-t_requester')
            pr = dict(id=str(uuid4()), task_id=owner['id'], bot_id=owner['bot_id'], kind='pr',
                title='Draft correction', status='preparing', pr_state='open', url=URL,
                created_at=owner['created_at'], work_outcome_id=work['id'],
                _review_task_id=owner['id'], _activity_task_id=owner['id'])
            report = saved_report(owner, work, id=str(uuid4()), created_at=owner['created_at'])
            requester_pr = dict(id=str(uuid4()), task_id=requester['id'], bot_id=requester['bot_id'],
                kind='pr', title='The delegated PR', status='preparing', pr_state='open', url=URL,
                created_at=requester['created_at'], work_outcome_id=work['id'], _review_task_id=requester['id'])
            if requester_turn:
                # The requester reports activity on the delegated PR itself, as in PR 8606.
                requester_pr['_activity_task_id'] = requester['id']
            items = [work, pr, report, requester_pr]
            if second_pr:
                items.append(dict(pr, id=str(uuid4()), url=URL[:-1] + '2'))
            for item in items:
                self.service._put('artifacts', item)
            saved.extend(items)
            if requester_turn:
                self.other_caller = source
                source = self.requester_turn(requester)
            return owner, source, identity, workspace

        self.prepare = prepare_linkage
        return saved

    def test_a_requester_dispatches_the_delegated_pr_once_with_its_report(self):
        saved = self.linked()
        self.check_dispatch('done')
        work, pr, report, requester_pr = saved
        owner = self.service._get('tasks', pr['task_id'])
        self.assertEqual(owner['_work_context_id'], pr['id'])
        for item in (work, report, requester_pr):
            self.assertEqual(self.service._get('artifacts', item['id']), item)

    def test_an_owner_dispatches_the_delegated_pr_once_with_its_report(self):
        saved = self.linked()
        self.check_dispatch('done', 'owner')
        self.assertEqual(self.service._get('tasks', saved[1]['task_id'])['_work_context_id'], saved[1]['id'])

    def test_the_requester_dispatches_its_delegated_pr_from_its_own_native_turn(self):
        saved = self.linked(requester_turn=True)
        self.check_dispatch('done')
        work, pr, report, requester_pr = saved
        owner = self.service._get('tasks', pr['task_id'])
        self.assertEqual(owner['_work_context_id'], pr['id'])
        for item in (work, report, requester_pr):
            self.assertEqual(self.service._get('artifacts', item['id']), item)

    def test_another_requester_turn_cannot_exempt_the_requesters_activity(self):
        self.linked(requester_turn=True)
        owner, requester, identity, workspace = self.prepare('done')
        before = self.service._all('artifacts')

        def cards():
            return {identity: {key: self.service.kanban.tasks[identity][key]
                               for key in ('id', 'status', 'current_run_id', 'worker_pid')}
                    for identity in (owner['id'], requester['id'])}
        held = cards()
        with self.assertRaisesRegex(ServiceError, 'another execution'):
            self.resume(self.other_caller, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._get('tasks', requester['id']), requester)
        self.assertEqual(self.service._all('artifacts'), before)
        self.assertEqual(cards(), held)
        self.assertEqual(held[owner['id']]['status'], 'done')

    def test_two_delegated_prs_stay_ambiguous_through_the_native_board(self):
        saved = self.linked(second_pr=True)
        owner, source, identity, workspace = self.prepare('done')
        before = self.service._all('artifacts')
        with self.assertRaisesRegex(ServiceError, 'Choose one retained Work item'):
            self.resume(source, identity)
        self.assertEqual(self.service._get('tasks', owner['id']), owner)
        self.assertEqual(self.service._all('artifacts'), before)
        self.assertEqual(len(saved), 5)
        self.assertEqual(self.service.kanban.tasks[owner['id']]['status'], 'done')


if __name__ == '__main__':
    unittest.main()
