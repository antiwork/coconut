"""A delegate's hidden Work report and its delegated PR retain one execution and one outcome."""
import copy
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from coconut import native_outcomes
from coconut.service import ServiceError
import test_native_claim_continuation as native_fixtures
import test_native_work_continuation as fixtures
import test_requester_callback_continuation as callback_fixtures
from hermes_env import needs_hermes

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

    def run_turn(self, task, native_id, turn, context=''):
        # The task's current turn, running on its own native claim. Hermes gives context only to a retained outcome.
        task.update(status='working', _waiting_on=[], _native_turn_id=turn,
                    _conversation_epoch=self.service._setting('conversation_epoch'),
                    _kanban_claim=dict(native_id=native_id, external_id=task['id'], input=turn,
                                       run_id=71, pid=101, context=context))
        if context:
            task['_native_outcome_id'] = native_id
        self.service._put('tasks', task)
        self.service.running[task['id']] = Mock(is_alive=Mock(return_value=True))
        self.service.kanban.tasks[task['id']] = dict(id=native_id, status='running', current_run_id=71,
                                                     worker_pid=101, input_id=turn)
        return task

    def requester_activity(self, retained=False):
        owner, requester, work, pr = self.linked_requester_pr()
        # An earlier helper of the requester finished, and the requester consumed its result.
        helper = self.requester_branch(requester, status='completed',
                                       _conversation_epoch=self.service._setting('conversation_epoch'),
                                       _kanban_claim=dict(native_id='t_helper', input='helper'))
        self.service.kanban.tasks[helper['id']] = dict(id='t_helper', status='done')
        requester = self.service._get('tasks', requester['id'])
        if retained:
            # Like Desk in PR 8606: the requester's Work is its retained native outcome.
            requester['_work_context_id'] = work['id']
        requester = self.run_turn(requester, work['_native_outcome_id'], 'requester-turn',
                                  'Saved worker context' if retained else '')
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

    def test_the_requesters_retained_outcome_continues_with_its_current_input(self):
        owner, requester, work, pr, report, helper = self.requester_activity(retained=True)
        claim = requester['_kanban_claim']
        card = self.service.kanban.tasks[requester['id']]
        self.assertTrue(claim['context'])
        self.assertEqual((requester['_native_outcome_id'], requester['_work_context_id']), (work['_native_outcome_id'], work['id']))
        self.assertEqual((card['id'], card['input_id']), (claim['native_id'], claim['input']))
        self.assertEqual(claim['input'], requester['_native_turn_id'])
        receipt = self.resume(requester)
        self.assert_admitted_once(owner, pr, receipt, lambda: self.resume(requester))
        self.assertEqual(self.service._get('tasks', requester['id']), requester)
        self.assertEqual(self.service._get('artifacts', work['id']), work)

    def test_a_retained_requester_needs_its_current_input_and_native_identity(self):
        def card(**changes):
            return lambda r: self.service.kanban.tasks[r['id']].update(changes)

        def claim(**changes):
            return lambda r: self.service._put('tasks', dict(r, _kanban_claim=dict(r['_kanban_claim'], **changes)))

        def task(**changes):
            return lambda r: self.service._put('tasks', dict(r, **changes))

        cases = {
            'a card with an earlier input': card(input_id='earlier-turn'),
            'a card without an input': card(input_id=None),
            'a claim of an earlier input': claim(input='earlier-turn'),
            'a claim without an input': claim(input=None),
            'a later turn than the claim': task(_native_turn_id='later-turn'),
            'another retained outcome': task(_native_outcome_id='t_other'),
            'no retained outcome': task(_native_outcome_id=None),
        }

        def check(change):
            owner, requester, work, pr, report, helper = self.requester_activity(retained=True)
            change(requester)
            self.assert_rejected(owner, self.service._get('tasks', requester['id']), 'another execution')
        self.each(cases, check)

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


class SelectedFinishedPrTests(unittest.TestCase):
    """Every delegated PR of the requester's Work finished, as PR 8606 and Coconut 218 of one Care recovery.

    Coconut chooses none of them. The plain call refuses and names retained_pr_url. A caller that names one finished PR
    continues the same execution with that existing PR as its Work item. The choice approves nothing, and every other
    shape, guard, and caller check stays. No Work row or other artifact is created.
    """
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    resume = fixtures.NativeWorkContinuationTests.resume
    linked_requester_pr = fixtures.NativeWorkContinuationTests.linked_requester_pr
    audited_requester_pr = fixtures.NativeWorkContinuationTests.audited_requester_pr
    requester_branch = DelegatedReportContinuationTests.requester_branch
    requester_cases = DelegatedReportContinuationTests.requester_cases
    requester_callback = DelegatedReportContinuationTests.requester_callback
    run_turn = LinkedRequesterActivityTests.run_turn
    requester_activity = LinkedRequesterActivityTests.requester_activity
    each = LinkedRequesterActivityTests.each
    through_provider = callback_fixtures.RequesterCallbackContinuationTests.through_provider

    SECOND_URL = 'https://github.com/example/coconut/pull/218'
    MESSAGE = 'The corrected commit passed review. Finish its acceptance check.'

    def finished(self, first='merged', second='merged', ids=('delegated-pr', 'second-delegated-pr'), retained=False):
        """The saved shape: two finished delegated PRs of one Work, and the requester's own finished report of the first.

        The requester's own native turn makes the call. It reported activity on its PR itself.
        """
        owner, requester, work, pr, report, helper = self.requester_activity(retained=retained)
        self.service.db.execute('DELETE FROM artifacts WHERE id=?', (pr['id'],))
        prs = [dict(pr, id=ids[0], status=first, pr_state=first),
               dict(pr, id=ids[1], url=self.SECOND_URL, title='Let the requester continue', status=second, pr_state=second)]
        report = dict(report, status='merged', pr_state='merged')
        for item in (*prs, report):
            self.service._put('artifacts', item)
        return owner, requester, work, prs, report

    def choose(self, caller, url, **changes):
        return self.service.emit(caller['id'], 'work_continue',
                                 dict(task_id='t_existing', message=self.MESSAGE, retained_pr_url=url, **changes))

    def state(self):
        tables = ('tasks', 'artifacts', 'deliveries', 'decisions', 'contexts', 'settings', 'native_receipts')
        saved = {table: self.service.db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in tables}
        saved['events'] = self.service.db.execute('SELECT count(*), max(seq) FROM events').fetchone()
        return saved

    def assert_no_writes(self, call, message):
        before = self.state()
        with self.assertRaisesRegex(ServiceError, message):
            call()
        self.assertEqual(self.state(), before)

    def assert_chosen(self, owner, requester, work, prs, report, chosen, receipt):
        others = {a['id']: a for a in self.service._all('artifacts') if a['id'] != chosen['id']}
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        continued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', 'thread_id', 'session_id', 'exchange_id', 'source_task_id',
                    'source_bot_id', '_reply_to_task_id', '_kanban_claim', 'parent_task_id'):
            self.assertEqual(continued.get(key), owner.get(key), key)
        self.assertEqual((continued['status'], continued['_native_outcome_id']), ('queued', 't_existing'))
        # The existing PR is the Work item, through the same guarded path as a single delegated PR.
        self.assertEqual(continued['_work_context_id'], chosen['id'])
        self.assertEqual(self.service._get('artifacts', chosen['id']), dict(chosen, _native_outcome_id='t_existing'))
        return continued, others

    def test_the_plain_call_still_refuses_and_names_the_choice(self):
        # The PR IDs sort both ways, so artifact order cannot choose.
        for ids in (('a-delegated-pr', 'z-delegated-pr'), ('z-delegated-pr', 'a-delegated-pr')):
            with self.subTest(ids=ids):
                try:
                    owner, requester, work, prs, report = self.finished(ids=ids)
                    self.assert_no_writes(lambda: self.resume(requester),
                                          '^' + re.escape(native_outcomes.FINISHED_PRS_AMBIGUOUS) + '$')
                finally:
                    self.tearDown()
                    self.setUp()

    def test_a_named_finished_pr_continues_the_same_execution(self):
        for index, url in ((0, URL), (1, self.SECOND_URL), (1, self.SECOND_URL.upper())):
            with self.subTest(url=url):
                try:
                    owner, requester, work, prs, report = self.finished()
                    before = {a['id']: a for a in self.service._all('artifacts')}
                    events = self.service.db.execute('SELECT max(seq) FROM events').fetchone()[0]
                    receipt = self.choose(requester, url)
                    continued, others = self.assert_chosen(owner, requester, work, prs, report, prs[index], receipt)
                    # No Work row or other artifact appears. The other PR, the Work, and the report stay as they were.
                    self.assertEqual(set(a['id'] for a in self.service._all('artifacts')), set(before))
                    self.assertEqual(others, {k: v for k, v in before.items() if k != prs[index]['id']})
                    self.assertEqual(self.service.db.execute(
                        "SELECT count(*) FROM events WHERE seq>? AND type='artifact.created'", (events,)).fetchone()[0], 0)
                    self.assertEqual(self.choose(requester, url), receipt)
                    self.assertEqual(self.service._get('tasks', owner['id']), continued)
                    self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == receipt]), 1)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_the_requesters_retained_outcome_chooses_from_its_current_input(self):
        owner, requester, work, prs, report = self.finished(retained=True)
        self.assert_chosen(owner, requester, work, prs, report, prs[1], self.choose(requester, self.SECOND_URL))
        self.assertEqual(self.service._get('tasks', requester['id']), requester)
        self.assertEqual(self.service._get('artifacts', work['id']), work)

    def test_an_audited_delegation_can_name_its_finished_pr(self):
        owner, coordinator, work, pr = self.audited_requester_pr()
        # The owner reported activity on its second PR itself, as Care did on Coconut 218.
        finished = [dict(pr, status='merged', pr_state='merged'),
                    dict(pr, id='second-delegated-pr', url=self.SECOND_URL, status='merged', pr_state='merged',
                         _activity_task_id=owner['id'])]
        for item in finished:
            self.service._put('artifacts', item)
        self.assert_no_writes(lambda: self.resume(coordinator), re.escape(native_outcomes.FINISHED_PRS_AMBIGUOUS))
        receipt = self.choose(coordinator, URL)
        self.assertEqual(receipt['task_id'], owner['id'])
        self.assertEqual(self.service._get('tasks', owner['id'])['_work_context_id'], finished[0]['id'])
        self.assertEqual(self.service._get('artifacts', finished[1]['id']), finished[1])
        self.assertEqual(self.service._get('artifacts', work['id']), work)

    def provider_call(self, caller, payload, twice=False):
        """The real Hermes worker calls coconut_continue_work with the payload, as through_provider does."""
        self.root = Path(self.temp.name) / str(uuid4())
        behavior = "import json\nresult = registry.dispatch('coconut_continue_work', " + repr(payload) + ")\n"
        if twice:
            behavior += ("assert json.loads(registry.dispatch('coconut_continue_work', " + repr(payload) + ")) "
                         "== json.loads(result)\n")
        behavior += "return {'completed': True, 'final_response': result}\n"
        provider = callback_fixtures.test_provider.ProviderTests.fake_hermes(self, behavior)
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

    def test_the_provider_passes_the_choice_and_admits_once(self):
        owner, requester, work, prs, report = self.finished()
        # Without the choice, the worker's call gets the refusal and nothing changes.
        before = self.state()
        refused = self.provider_call(requester, dict(task_id='t_existing', message=self.MESSAGE))
        self.assertEqual(refused, dict(status='rejected', error=native_outcomes.FINISHED_PRS_AMBIGUOUS))
        self.assertEqual({k: v for k, v in self.state().items() if k not in ('events',)},
                         {k: v for k, v in before.items() if k not in ('events',)})
        receipt = self.provider_call(requester, dict(task_id='t_existing', message=self.MESSAGE,
                                                     retained_pr_url=self.SECOND_URL), twice=True)
        self.assert_chosen(owner, requester, work, prs, report, prs[1], receipt)
        self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == receipt]), 1)

    def test_the_schema_offers_an_optional_choice(self):
        schema = native_outcomes.CONTINUE_SCHEMA['parameters']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['required'], ['task_id', 'message'])
        self.assertEqual({k: schema['properties']['retained_pr_url'][k] for k in ('type', 'maxLength')},
                         dict(type='string', maxLength=native_outcomes.RETAINED_PR_URL_LIMIT))

    def test_the_original_requester_receives_the_result_once(self):
        owner, requester, work, prs, report = self.finished()
        self.choose(requester, URL)
        earlier = {d['id'] for d in self.service._all('deliveries')}
        # The owner's turn completes the native outcome, as test_native_work_continuation does.
        continued = dict(self.service._get('tasks', owner['id']), status='completed')
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=continued['_native_turn_id'])
        self.service._prepare_return(continued, 'The merged change is verified.', 'completed')
        self.service._put('tasks', continued)
        self.service.running.pop(requester['id'])
        self.service._put('tasks', dict(self.service._get('tasks', requester['id']), status='completed'))
        self.service._drain_returns()
        self.service._drain_returns()
        returned = [d for d in self.service._all('deliveries') if d['id'] not in earlier and d['source_task_id'] == owner['id']]
        self.assertEqual([(d['target_bot_id'], d['text']) for d in returned],
                         [(requester['bot_id'], 'The merged change is verified.')])
        callbacks = [t for t in self.service._all('tasks') if t.get('_is_return') and t.get('source_task_id') == owner['id']]
        self.assertEqual([(t['bot_id'], t['_assignment_task_id']) for t in callbacks], [(requester['bot_id'], requester['id'])])
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['_reply_to_task_id'], finished['_work_context_id'], finished['_execution_session_id']),
                         (requester['id'], prs[0]['id'], owner['_execution_session_id']))
        self.assertFalse(finished.get('_return_pending'))
        self.assertEqual(self.service._get('artifacts', prs[1]['id']), prs[1])
        self.assertEqual(self.service._get('artifacts', work['id']), work)

    def test_a_choice_is_refused_in_every_other_shape(self):
        refused = native_outcomes.SELECTION_REFUSED

        def put(index, **changes):
            return lambda owner, requester, work, prs: self.service._put('artifacts', dict(prs[index], **changes))

        def other_work(owner, requester, work, prs):
            self.service._put('artifacts', dict(work, id='other-work', _native_outcome_id='t_other'))
            put(1, work_outcome_id='other-work')(owner, requester, work, prs)

        def other_report(owner, requester, work, prs):
            other = dict(work, id='other-work', _native_outcome_id='t_other')
            self.service._put('artifacts', other)
            self.service._put('artifacts', saved_report(owner, other, _work_parent_task_id=requester['id']))

        def foreign_report(owner, requester, work, prs):
            self.service._put('artifacts', saved_report(owner, work, _work_parent_task_id=requester['id'],
                                                        _native_outcome_id='t_other'))

        def owner_work(owner, requester, work, prs):
            self.service._put('artifacts', dict(id='owner-work', task_id=owner['id'], bot_id=owner['bot_id'], kind='work',
                                                title='Other work', status='waiting'))

        def unproven(owner, requester, work, prs):
            other = dict(requester, id='other-requester-task', _execution_session_id='other-requester-task',
                         _work_context_id=None, _native_outcome_id=None, _kanban_claim=None)
            self.service._put('tasks', other)
            self.service._put('artifacts', dict(work, task_id=other['id']))

        def single(owner, requester, work, prs):
            self.service.db.execute('DELETE FROM artifacts WHERE id=?', (prs[1]['id'],))

        def requester_only(owner, requester, work, prs):
            self.service._put('artifacts', dict(id='requester-only-pr', task_id=requester['id'], bot_id=requester['bot_id'],
                kind='pr', title='Requester PR', status='merged', pr_state='merged', url=URL[:-1] + '7',
                work_outcome_id=work['id'], _review_task_id=requester['id']))

        def retained_item(owner, requester, work, prs):
            # A stale owner that already retains its Work item cannot have it replaced.
            self.service._put('tasks', dict(owner, _work_context_id=prs[0]['id']))

        cases = {
            'an open PR left': (URL, put(1, status='open', pr_state='open')),
            'the open PR itself': (self.SECOND_URL, put(1, status='open', pr_state='open')),
            'two open PRs and a finished one': (URL, lambda owner, requester, work, prs: [
                self.service._put('artifacts', dict(prs[1], id=identity, url=self.SECOND_URL[:-1] + str(n),
                                                    status='open', pr_state='open'))
                for n, identity in enumerate(('open-1', 'open-2'))]),
            'a single finished PR': (URL, single),
            'a URL that no owner PR has': (URL[:-1] + '9', None),
            'the requesters own PR': (URL[:-1] + '7', requester_only),
            'a finished PR of another Work': (URL, other_work),
            'the PR of another Work': (self.SECOND_URL, other_work),
            'a report of another Work': (URL, other_report),
            'a report of another native outcome': (URL, foreign_report),
            'another Work of the owner': (URL, owner_work),
            'an unproven delegate link': (URL, unproven),
            'a PR of another bot': (self.SECOND_URL, put(1, bot_id='someone-else')),
            'an owner that retains its Work item': (URL, retained_item),
        }

        def check(case):
            url, change = case
            owner, requester, work, prs, report = self.finished()
            if change:
                change(owner, requester, work, prs)
            self.assert_no_writes(lambda: self.choose(self.service._get('tasks', requester['id']), url),
                                  '^' + re.escape(refused) + '$')
        self.each(cases, check)

    def test_a_choice_of_an_owner_with_a_retained_outcome_is_refused(self):
        owner, requester, work, prs, report = self.finished()
        self.choose(requester, URL)
        self.service._put('tasks', dict(self.service._get('tasks', owner['id']), status='completed'))
        self.assert_no_writes(lambda: self.service.emit(requester['id'], 'work_continue', dict(
            task_id='t_existing', message='Again.', retained_pr_url=self.SECOND_URL)), re.escape(native_outcomes.SELECTION_REFUSED))

    def test_a_malformed_choice_is_refused(self):
        owner, requester, work, prs, report = self.finished()
        for value in ('', '   ', 7, None, ['x'], 'x' * (native_outcomes.RETAINED_PR_URL_LIMIT + 1)):
            with self.subTest(value=value):
                self.assert_no_writes(lambda: self.choose(requester, value), 'retained_pr_url must contain')

    def test_a_choice_keeps_every_guard(self):
        def pause(owner, requester, work, prs):
            self.service._put('artifacts', dict(work, paused_by_decision_id='held'))
            self.service._put('decisions', dict(id='held', task_id=requester['id'], status='rejected', decided_by='human'))

        def cancel(owner, requester, work, prs):
            self.service.db.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                                    ('work.cancelled.work:' + work['id'], json.dumps('cancelled')))

        def owner_change(**changes):
            return lambda owner, requester, work, prs: self.service._put('tasks', dict(owner, **changes))

        def owner_decision(owner, requester, work, prs):
            self.service._put('decisions', dict(id='pending', task_id=owner['id'], bot_id=owner['bot_id'],
                                                status='pending', title='Publish?'))

        def writer_activity(owner, requester, work, prs):
            writer = dict(requester, id='other-requester-writer', status='working',
                          _execution_session_id='other-requester-writer')
            self.service._put('tasks', writer)
            self.service._put('artifacts', dict(id='writer-pr', task_id=requester['id'], bot_id=requester['bot_id'],
                kind='pr', title='Writer PR', status='merged', pr_state='merged', url=self.SECOND_URL,
                work_outcome_id=work['id'], _review_task_id=requester['id'], _activity_task_id=writer['id']))

        def open_activity(owner, requester, work, prs):
            self.service._put('artifacts', dict(id='requester-open-pr', task_id=requester['id'], bot_id=requester['bot_id'],
                kind='pr', title='Open PR', status='open', pr_state='open', url=self.SECOND_URL[:-1] + '9',
                work_outcome_id=work['id'], _review_task_id=requester['id'], _activity_task_id=requester['id']))

        def unknown_peer_tool(owner, requester, work, prs):
            reporter = dict(requester, id='requester-reporter', status='failed', _execution_session_id='requester-reporter')
            self.service._put('tasks', reporter)
            self.service._put('native_receipts', dict(id=str(uuid4()), task_id=reporter['id'], turn_id='turn',
                state='executing', action=dict(name='browser_navigate', arguments={}), created_at=reporter['created_at']))
            self.service._put('artifacts', dict(id='reporter-pr', task_id=requester['id'], bot_id=requester['bot_id'],
                kind='pr', title='Reporter PR', status='merged', pr_state='merged', url=self.SECOND_URL,
                work_outcome_id=work['id'], _review_task_id=requester['id'], _activity_task_id=reporter['id']))

        def activity_without_review(owner, requester, work, prs):
            # The requester's activity on the other PR is a writer unless its own finished review covers it.
            report = self.service._get('artifacts', 'requester-pr')
            report.pop('_review_task_id')
            self.service._put('artifacts', report)

        def local_turn(owner, requester, work, prs):
            # Naming the requester's own PR still needs the requester's own native turn for its activity.
            report = self.service._get('artifacts', 'requester-pr')
            report.pop('_review_task_id')
            self.service._put('artifacts', report)
            self.service._put('tasks', dict(requester, _native_turn_id=None, _kanban_claim=None))

        cases = {
            'a paused Work': ('paused', self.SECOND_URL, pause),
            'a cancelled Work': ('paused or cancelled', self.SECOND_URL, cancel),
            'a pending owner decision': ('existing decision', self.SECOND_URL, owner_decision),
            'an undelivered owner result': ('previous result', self.SECOND_URL, owner_change(_return_pending={'text': 'x'})),
            'an owner wait': ('existing dependencies', self.SECOND_URL, owner_change(_waiting_on=['helper'])),
            'an active owner branch': ('active work', self.SECOND_URL, lambda owner, requester, work, prs: self.service._put(
                'tasks', dict(owner, id='owner-branch', status='working', _native_outcome_id=None, _kanban_claim=None))),
            'another requester writer': ('another execution', self.SECOND_URL, writer_activity),
            'the requesters activity on an open PR': ('another execution', URL, open_activity),
            'an unknown tool of a peer': ('another execution', URL, unknown_peer_tool),
            'the requesters activity without its finished review': ('another execution', self.SECOND_URL, activity_without_review),
            'a local requester turn with activity': ('another execution', URL, local_turn),
        }

        def check(case):
            message, url, change = case
            owner, requester, work, prs, report = self.finished()
            change(owner, requester, work, prs)
            self.assert_no_writes(lambda: self.choose(self.service._get('tasks', requester['id']), url), message)
        self.each(cases, check)

        def check_requester(name):
            owner, requester, work, prs, report = self.finished()
            self.requester_cases(requester['id'])[name]()
            self.assert_no_writes(lambda: self.choose(self.service._get('tasks', requester['id']), URL), 'another execution')
        owner, requester, work, prs, report = self.finished()
        names = list(self.requester_cases(requester['id']))
        self.tearDown()
        self.setUp()
        self.assertEqual(len(names), 8)
        self.each({name: name for name in names}, check_requester)


class RequesterDeliveredResultTests(unittest.TestCase):
    """The direct requester continues its delegated PR from the turn that a delivered result started, as Desk in PR 8606.

    The service resumes the requester with the result as the input of a new turn (_resume_process_task) and keeps it
    as _background_completion while that turn runs. That consumed input is not pending work of the caller. Every
    undelivered wait, relay, process, or decision wait, every other turn of its assignment, and every other guard
    still refuses the call, with no write.
    """
    setUp = fixtures.NativeWorkContinuationTests.setUp
    tearDown = fixtures.NativeWorkContinuationTests.tearDown
    write = fixtures.NativeWorkContinuationTests.write
    prepare = fixtures.NativeWorkContinuationTests.prepare
    snapshot_result = fixtures.NativeWorkContinuationTests.snapshot_result
    scheduled_native = fixtures.NativeWorkContinuationTests.scheduled_native
    resume = fixtures.NativeWorkContinuationTests.resume
    linked_requester_pr = fixtures.NativeWorkContinuationTests.linked_requester_pr
    requester_branch = DelegatedReportContinuationTests.requester_branch
    requester_cases = DelegatedReportContinuationTests.requester_cases
    requester_callback = DelegatedReportContinuationTests.requester_callback
    run_turn = LinkedRequesterActivityTests.run_turn
    each = LinkedRequesterActivityTests.each
    through_provider = callback_fixtures.RequesterCallbackContinuationTests.through_provider
    state = SelectedFinishedPrTests.state
    assert_no_writes = SelectedFinishedPrTests.assert_no_writes

    REFUSAL = '^This Work item has another execution. Continue its current owner.$'

    def end_turn(self, requester, status='waiting'):
        """The requester's turn ends, and Hermes leaves its card without a run."""
        self.service.running.pop(requester['id'], None)
        self.service._put('tasks', dict(self.service._get('tasks', requester['id']), status=status))
        self.service.kanban.tasks[requester['id']].update(status='scheduled', current_run_id=None, worker_pid=None)

    def delivered(self, path='wait'):
        """The live order: the requester waits for its helper, the helper's return starts a turn that ends waiting, and
        the service then delivers the saved wait into a new turn. Hermes claims that input, and the turn runs.

        path='process' delivers a finished background process of an earlier turn instead.
        """
        owner, requester, work, pr = self.linked_requester_pr()
        # The requester's own report of the same PR, with its review and activity, as Desk's fae77625.
        report = dict(id='requester-pr', task_id=requester['id'], bot_id=requester['bot_id'], kind='pr',
                      title='The delegated PR', status='open', pr_state='open', url=pr['url'], work_outcome_id=work['id'],
                      _review_task_id=requester['id'], _activity_task_id=requester['id'])
        self.service._put('artifacts', report)
        epoch = self.service._setting('conversation_epoch')
        helper = self.requester_branch(requester, status='working', _conversation_epoch=epoch,
                                       _kanban_claim=dict(native_id='t_helper', input='helper'))
        self.service.kanban.tasks[helper['id']] = dict(id='t_helper', status='running', current_run_id=5, worker_pid=None)
        requester = dict(self.service._get('tasks', requester['id']), _work_context_id=work['id'])
        requester = self.run_turn(requester, work['_native_outcome_id'], 'first-turn', 'Saved worker context')
        if path == 'wait':
            self.assertEqual(self.service.emit(requester['id'], 'work_wait', dict(task_id=helper['id'], reason='Need the helper result'))['status'], 'waiting')
            self.end_turn(requester)
            self.service.emit(helper['id'], 'completed', dict(summary='The helper result.'))
            self.service.kanban.tasks[helper['id']].update(status='done', current_run_id=None)
            # The helper's return reaches the requester first. That turn runs and ends waiting again.
            self.service._drain_returns()
            returned = self.service._get('tasks', requester['id'])
            self.assertEqual((returned['status'], returned.get('_background_completion')), ('queued', None))
            self.run_turn(returned, work['_native_outcome_id'], returned['_native_turn_id'], 'Saved worker context')
            self.end_turn(returned)
            self.assertIsNone(self.service._get('tasks', helper['id']).get('_return_pending'))
            # The service delivers the saved wait through _resume_process_task.
            self.service._drain_task_completions()
        else:
            call = self.service.emit(requester['id'], 'native_call', dict(name='terminal', arguments={'background': True}))
            self.service.emit(requester['id'], 'native_result', dict(id=call['id'], result=json.dumps(
                {'session_id': 'proc_check', 'notify_on_complete': True, 'status': 'running'})))
            self.end_turn(requester)
            self.service.emit(helper['id'], 'completed', dict(summary='The helper result.'))
            self.service.kanban.tasks[helper['id']].update(status='done', current_run_id=None)
            self.service._drain_returns()
            returned = self.service._get('tasks', requester['id'])
            self.run_turn(returned, work['_native_outcome_id'], returned['_native_turn_id'], 'Saved worker context')
            self.end_turn(returned)
            waiting = self.service._get('tasks', requester['id'])
            self.assertTrue(self.service._can_resume_process(waiting, self.service._get('bots', waiting['bot_id'])))
            with self.service.transaction():
                self.service._resume_process_task(requester['id'], [(self.service._get('native_receipts', call['id']),
                                                                     'The background check finished.')])
        resumed = self.service._get('tasks', requester['id'])
        self.assertEqual((resumed['status'], resumed['summary']), ('queued', 'Continuing with the result.'))
        self.assertTrue(resumed['_background_completion'])
        delivered = [r for r in self.service._native_task_receipts(requester['id'])
                     if r.get('_process_delivery') == resumed['_native_turn_id']]
        self.assertEqual(len(delivered), 1)
        # Hermes claims the delivered input. The requester's own native turn runs and makes the call.
        requester = self.run_turn(resumed, work['_native_outcome_id'], resumed['_native_turn_id'], 'Saved worker context')
        self.assertTrue(self.service._calling_native_turn(requester, self.service.kanban.tasks))
        self.assertTrue(self.service._background_pending(requester['id']), 'Its own consumed input counted before the fix')
        self.assertFalse(self.service._background_pending(requester['id'], finishing_task_id=requester['id']))
        return owner, requester, work, pr, report, self.service._get('tasks', helper['id']), delivered[0]

    def assert_admitted_once(self, owner, requester, pr, receipt, repeat):
        artifacts = {a['id']: a for a in self.service._all('artifacts') if a['id'] != pr['id']}
        tasks = {t['id']: t for t in self.service._all('tasks') if t['id'] != owner['id']}
        self.assertEqual(receipt, dict(task_id=owner['id'], native_task_id='t_existing', status='queued'))
        continued = self.service._get('tasks', owner['id'])
        for key in ('id', '_execution_session_id', 'thread_id', 'session_id', 'exchange_id', 'source_task_id',
                    'source_bot_id', '_reply_to_task_id', '_kanban_claim', 'parent_task_id'):
            self.assertEqual(continued.get(key), owner.get(key), key)
        self.assertEqual((continued['status'], continued['_work_context_id']), ('queued', pr['id']))
        self.assertEqual(self.service._get('artifacts', pr['id']), dict(pr, _native_outcome_id='t_existing'))
        self.assertEqual(repeat(), receipt)
        self.assertEqual(len([d for d in self.service._all('deliveries') if d.get('receipt') == receipt]), 1)
        self.assertEqual({a['id']: a for a in self.service._all('artifacts') if a['id'] != pr['id']}, artifacts)
        self.assertEqual({t['id']: t for t in self.service._all('tasks') if t['id'] != owner['id']}, tasks)
        # The requester keeps its turn, its consumed input, and its claim.
        self.assertEqual(self.service._get('tasks', requester['id']), requester)

    def test_the_requester_continues_its_delegated_pr_from_its_delivered_result_turn(self):
        for path in ('wait', 'process'):
            with self.subTest(path=path):
                try:
                    owner, requester, work, pr, report, helper, delivered = self.delivered(path)
                    receipt = self.resume(requester)
                    self.assert_admitted_once(owner, requester, pr, receipt, lambda: self.resume(requester))
                    self.assertEqual(self.service._get('native_receipts', delivered['id']), delivered)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_the_provider_route_admits_once(self):
        owner, requester, work, pr, report, helper, delivered = self.delivered()
        receipt = self.through_provider(requester, twice=True)
        self.assert_admitted_once(owner, requester, pr, receipt, lambda: receipt)

    def test_the_original_requester_receives_the_result_once(self):
        owner, requester, work, pr, report, helper, delivered = self.delivered()
        self.resume(requester)
        earlier = {d['id'] for d in self.service._all('deliveries')}
        continued = dict(self.service._get('tasks', owner['id']), status='completed')
        self.service.kanban.tasks[owner['id']].update(status='done', input_id=continued['_native_turn_id'])
        self.service._prepare_return(continued, 'The PR follow-up is verified.', 'completed')
        self.service._put('tasks', continued)
        self.end_turn(requester, status='completed')
        self.service._drain_returns()
        self.service._drain_returns()
        returned = [d for d in self.service._all('deliveries') if d['id'] not in earlier and d['source_task_id'] == owner['id']]
        self.assertEqual([(d['target_bot_id'], d['text']) for d in returned],
                         [(requester['bot_id'], 'The PR follow-up is verified.')])
        finished = self.service._get('tasks', owner['id'])
        self.assertEqual((finished['_reply_to_task_id'], finished['_execution_session_id'], finished['_work_context_id']),
                         (owner['_reply_to_task_id'], owner['_execution_session_id'], pr['id']))
        self.assertFalse(finished.get('_return_pending'))

    def test_unchanged_guards_still_refuse_with_no_write(self):
        def receipt(**fields):
            def change(owner, requester, helper):
                self.service._put('native_receipts', dict(dict(
                    id=str(uuid4()), task_id=requester['id'], turn_id=requester['_native_turn_id'], state='completed',
                    result='{}', created_at='2026-10-24T21:40:40Z', action={'name': 'coconut_wait', 'arguments': {}}), **fields))
            return change

        def process(owner, requester, helper):
            call = self.service.emit(requester['id'], 'native_call', dict(name='terminal', arguments={'background': True}))
            self.service.emit(requester['id'], 'native_result', dict(id=call['id'], result=json.dumps(
                {'session_id': 'proc_other', 'notify_on_complete': True, 'status': 'running'})))

        def assignment_turn(status):
            def change(owner, requester, helper):
                turn = dict(requester, id=str(uuid4()), status=status, _assignment_task_id=requester['id'],
                            _background_completion=['Another result.'], _kanban_claim=None, _native_outcome_id=None)
                turn['_execution_session_id'] = requester['_execution_session_id']
                self.service._put('tasks', turn)
            return change

        def task(**changes):
            return lambda owner, requester, helper: self.service._put('tasks', dict(requester, **changes))

        def claim(**changes):
            return lambda owner, requester, helper: self.service._put('tasks', dict(
                requester, _kanban_claim=dict(requester['_kanban_claim'], **changes)))

        def card(**changes):
            return lambda owner, requester, helper: self.service.kanban.tasks[requester['id']].update(changes)

        def sibling(change=None):
            # A finished branch of the requester that still holds a live signal. The consumed helper is not one.
            def make(owner, requester, helper):
                branch = self.requester_branch(self.service._get('tasks', requester['id']), status='completed',
                                               _kanban_claim=dict(native_id='t_sibling', input='sibling'))
                self.service.kanban.tasks[branch['id']] = dict(id='t_sibling', status='done', current_run_id=None, worker_pid=None)
                self.service._put('tasks', dict(self.service._get('tasks', requester['id']), status=requester['status']))
                change(branch)
            return make

        def sibling_receipt(**fields):
            return lambda branch: self.service._put('native_receipts', dict(dict(
                id=str(uuid4()), task_id=branch['id'], turn_id='sibling-turn', state='completed', result='{}',
                created_at='2026-10-24T21:40:40Z', action={'name': 'coconut_wait', 'arguments': {}}), **fields))

        def decision(owner, requester, helper):
            self.service._put('decisions', dict(id='pending', task_id=requester['id'], bot_id=requester['bot_id'],
                                                status='pending', title='Publish?'))

        def owner_caller(owner, requester, helper):
            caller = self.write(f'/v1/bots/{owner["bot_id"]}/messages', {'text': 'Continue the PR.'})
            self.caller = dict(self.service._get('tasks', caller['id']), status='working')
            self.service._put('tasks', self.caller)

        def other_requester_task(owner, requester, helper):
            other = dict(requester, id='other-requester-task', _execution_session_id='other-requester-task',
                         _work_context_id=None, _native_outcome_id=None)
            self.service._put('tasks', other)
            self.caller = other

        cases = {
            'an undelivered wait of the requester': receipt(_wait_task_id='other-task'),
            'an undelivered relay of the requester': receipt(_relay_task_id='other-task'),
            'an undelivered decision wait of the requester': receipt(_wait_decision_id='other-decision'),
            'an undelivered process of the requester': process,
            'a queued assignment turn with a result': assignment_turn('queued'),
            'a working assignment turn with a result': assignment_turn('working'),
            'a local turn without a native claim': task(_native_turn_id=None, _kanban_claim=None),
            'a stale input': claim(input='earlier-input'),
            'a card with an earlier input': card(input_id='earlier-input'),
            'a later turn than the claim': task(_native_turn_id='later-turn'),
            'another run': card(current_run_id=99),
            'a reviewer run': card(claim_source_status='review'),
            'another worker pid': card(worker_pid=4242),
            'a card that is not running': card(status='ready'),
            'requester steering': task(_steering=[{'text': 'Wait.'}]),
            'requester live messages': task(_live_messages=[{'text': 'Wait.'}]),
            'an undelivered requester reply': task(_native_reply_pending=True),
            'a sibling branch with a live native card': sibling(lambda branch: self.service.kanban.tasks[branch['id']].update(
                status='running', current_run_id=7)),
            'a sibling branch with an undelivered wait': sibling(sibling_receipt(_wait_task_id='other-task')),
            'a sibling branch with an unknown tool result': sibling(sibling_receipt(state='executing', result=None,
                action={'name': 'terminal', 'arguments': {}})),
            'a sibling branch with a pending decision': sibling(lambda branch: self.service._put('decisions', dict(
                id='sibling-question', task_id=branch['id'], bot_id=branch['bot_id'], status='pending', title='Which list?'))),
            'a sibling branch with an undelivered return': sibling(lambda branch: self.service._put('tasks', dict(
                branch, _return_pending={'target_bot_id': 'someone', 'text': 'Result'}))),
            'a pending requester decision': decision,
            'the owner as caller': owner_caller,
            'another requester task as caller': other_requester_task,
        }

        def check(change):
            self.caller = None
            owner, requester, work, pr, report, helper, delivered = self.delivered()
            change(owner, requester, helper)
            caller = self.caller or self.service._get('tasks', requester['id'])
            self.assert_no_writes(lambda: self.resume(caller), 'another execution|belongs to another native outcome')
        self.each(cases, check)

    def test_the_requesters_other_branches_still_refuse(self):
        def check(name):
            owner, requester, work, pr, report, helper, delivered = self.delivered()
            self.requester_cases(requester['id'])[name]()
            self.assert_no_writes(lambda: self.resume(self.service._get('tasks', requester['id'])), self.REFUSAL)
        owner, requester, work, pr, report, helper, delivered = self.delivered()
        names = list(self.requester_cases(requester['id']))
        self.tearDown()
        self.setUp()
        self.assertEqual(len(names), 8)
        self.each({name: name for name in names}, check)


@needs_hermes
class NativeDelegatedReportContinuationTests(unittest.TestCase):
    setUp = native_fixtures.NativeClaimContinuationTests.setUp
    write = native_fixtures.NativeClaimContinuationTests.write
    native_program = native_fixtures.NativeClaimContinuationTests.native_program
    prepare = native_fixtures.NativeClaimContinuationTests.prepare
    resume = native_fixtures.NativeClaimContinuationTests.resume
    check_dispatch = native_fixtures.NativeClaimContinuationTests.check_dispatch

    def requester_turn(self, owner, requester, work, delivered=False):
        """The direct requester's retained native outcome. The dispatcher claims it for its current input.

        delivered: the input is a delivered result. An earlier turn started a background check and ended waiting, and the
        service resumed the requester with the result as the input of a new turn (_resume_process_task), as for Desk.
        """
        # One work slot, so the requester's running turn holds it until the turn ends.
        self.service.mutate('PUT', '/v1/settings/work-capacity', {'limit': 1}, str(uuid4()))
        self.assertEqual(self.service.snapshot()['work_capacity']['effective_limit'], 1)
        # Like Desk in PR 8606, the requester runs as a Kanban outcome, not as a chat turn.
        requester.pop('_chat_session', None)
        requester.update(status='queued', _execution_session_id=requester['id'], _waiting_on=[],
                         _native_turn_id='requester-input', _work_context_id=work['id'],
                         _conversation_epoch=self.service._setting('conversation_epoch'))
        if delivered:
            requester.update(status='working', _native_turn_id='earlier-turn')
            self.service._put('tasks', requester)
            call = self.service.emit(requester['id'], 'native_call', dict(name='terminal', arguments={'background': True}))
            self.service.emit(requester['id'], 'native_result', dict(id=call['id'], result=json.dumps(
                {'session_id': 'proc_check', 'notify_on_complete': True, 'status': 'running'})))
            self.service._put('tasks', dict(self.service._get('tasks', requester['id']), status='waiting'))
            waiting = self.service._get('tasks', requester['id'])
            self.assertTrue(self.service._can_resume_process(waiting, self.service._get('bots', waiting['bot_id'])))
            with self.service.transaction():
                self.service._resume_process_task(requester['id'], [(self.service._get('native_receipts', call['id']),
                                                                     'The background check finished.')])
            requester = self.service._get('tasks', requester['id'])
            self.assertEqual((requester['status'], requester['_background_completion']),
                             ('queued', ['The background check finished.']))
            self.assertEqual(self.service._get('native_receipts', call['id'])['_process_delivery'], requester['_native_turn_id'])
        self.service._put('tasks', requester)
        row = self.service._kanban_row(requester, requester, 'ready', 10)
        self.native_program('import json; board.sync([json.loads(sys.argv[2])])', json.dumps(row))
        self.service.kanban.call({'op': 'snapshot'})
        native = self.service.kanban.tasks[requester['id']]['id']
        requester['_native_outcome_id'] = native
        self.service._put('tasks', requester)
        work.update(_native_outcome_id=native, _work_key='native-' + native)
        self.service._put('artifacts', work)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def run(task, bot, history, emit):
            started.set()
            if not release.wait(60):
                return
            # The requester's turn ends normally: its own run completes its card.
            current = self.service._get('tasks', task['id'])
            self.native_program("assert board.kb.complete_task(board.conn, sys.argv[2], summary='Requester turn ended', "
                                "expected_run_id=int(sys.argv[3]))",
                                current['_native_outcome_id'], str(current['_kanban_claim']['run_id']))
            emit('completed', {'summary': 'Requester turn ended'})
        self.provider.start_task = run
        self.service._dispatch_pending()
        self.assertTrue(started.wait(15), 'The dispatcher must claim and start the requester turn')
        self.service.kanban.call({'op': 'snapshot'})
        requester = self.service._get('tasks', requester['id'])
        claim, card = requester['_kanban_claim'], self.service.kanban.tasks[requester['id']]
        # The claim is Hermes' own: the retained outcome's card, its current input, and a worker context.
        self.assertEqual((requester['status'], requester['_native_outcome_id'], requester['_work_context_id']),
                         ('working', native, work['id']))
        self.assertEqual((claim['native_id'], claim['external_id']), (native, requester['id']))
        self.assertTrue(claim['context'])
        self.assertEqual((card['id'], card['status'], card['current_run_id'], card['worker_pid']),
                         (native, 'running', claim['run_id'], claim['pid']))
        self.assertEqual(card['input_id'], claim['input'])
        self.assertEqual(claim['input'], requester['_native_turn_id'])

        def end_requester_turn():
            worker = self.service.running[requester['id']]
            release.set()
            worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.service._get('tasks', requester['id'])['status'], 'completed')
            self.service.kanban.call({'op': 'snapshot'})
            self.assertEqual(self.service.kanban.tasks[requester['id']]['status'], 'done')

        def end_caller_turn():
            # The admitted owner is queued, but the requester's running card holds the only work slot.
            self.service._dispatch_pending()
            self.assertNotIn(owner['id'], self.service.running)
            self.assertEqual(self.service._get('tasks', owner['id'])['status'], 'queued')
            self.assertEqual(self.service.kanban.tasks[requester['id']]['status'], 'running')
            end_requester_turn()
        self.end_requester_turn = end_requester_turn
        self.end_caller_turn = end_caller_turn
        return requester

    def linked(self, second_pr=False, requester_turn=False, finished=False, delivered=False):
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
            if finished:
                # Every delegated PR of the Work merged, and so did the requester's own report of the first.
                pr.update(status='merged', pr_state='merged')
                requester_pr.update(status='merged', pr_state='merged')
            items = [work, pr, report, requester_pr]
            if second_pr:
                items.append(dict(pr, id=str(uuid4()), url=URL[:-1] + '2'))
            for item in items:
                self.service._put('artifacts', item)
            saved.extend(items)
            if requester_turn:
                self.other_caller = source
                source = self.requester_turn(owner, requester, work, delivered=delivered)
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
        self.assertEqual(self.service._get('artifacts', report['id']), report)
        # The requester's own Work and PR report keep their identity and links. Its turn may update their activity.
        for item, keys in ((work, ('task_id', 'kind', '_native_outcome_id')),
                           (requester_pr, ('task_id', 'kind', 'url', 'work_outcome_id', '_review_task_id', '_activity_task_id'))):
            current = self.service._get('artifacts', item['id'])
            self.assertEqual({k: current.get(k) for k in keys}, {k: item.get(k) for k in keys})
        self.assertNotEqual(work['_native_outcome_id'], 't_requester')

    def test_the_requester_dispatches_its_delegated_pr_from_its_delivered_result_turn(self):
        # The requester's turn runs on its own claim of the delivered result, which it carries until the turn ends.
        saved = self.linked(requester_turn=True, delivered=True)
        self.check_dispatch('done')
        work, pr, report, requester_pr = saved
        owner = self.service._get('tasks', pr['task_id'])
        self.assertEqual(owner['_work_context_id'], pr['id'])
        self.assertEqual(self.service._get('artifacts', report['id']), report)
        # No Work row or other artifact appears. The requester's Work and PR report keep their identity and links.
        self.assertEqual({a['id'] for a in self.service._all('artifacts')}, {item['id'] for item in saved})
        for item, keys in ((work, ('task_id', 'kind')),
                           (requester_pr, ('task_id', 'kind', 'url', 'work_outcome_id', '_review_task_id', '_activity_task_id'))):
            current = self.service._get('artifacts', item['id'])
            self.assertEqual({k: current.get(k) for k in keys}, {k: item.get(k) for k in keys})

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
        keys = ('status', '_native_turn_id', '_native_outcome_id', '_work_context_id', '_kanban_claim')
        current = self.service._get('tasks', requester['id'])
        self.assertEqual({k: current.get(k) for k in keys}, {k: requester.get(k) for k in keys})
        self.assertEqual(self.service._all('artifacts'), before)
        self.assertEqual(cards(), held)
        self.assertEqual((held[owner['id']]['status'], held[requester['id']]['status']), ('done', 'running'))
        self.end_requester_turn()

    def test_a_named_merged_pr_dispatches_once_through_the_native_board(self):
        saved = self.linked(second_pr=True, finished=True)
        prepare, plain = self.prepare, self.resume
        artifacts = set()

        def refuse_then_prepare(*args):
            # The plain call names no PR. It refuses and leaves Coconut and the board as they were.
            owner, source, identity, workspace = prepare(*args)
            before = (self.service._get('tasks', owner['id']), self.service._all('artifacts'),
                      dict(self.service.kanban.tasks[owner['id']]))
            with self.assertRaisesRegex(ServiceError, re.escape(native_outcomes.FINISHED_PRS_AMBIGUOUS)):
                plain(source, identity)
            self.service.kanban.call({'op': 'snapshot'})
            self.assertEqual((self.service._get('tasks', owner['id']), self.service._all('artifacts'),
                              dict(self.service.kanban.tasks[owner['id']])), before)
            artifacts.update(a['id'] for a in before[1])
            return owner, source, identity, workspace
        self.prepare = refuse_then_prepare
        # The same call, naming the second merged PR (see linked).
        self.resume = lambda source, identity: self.service.emit(source['id'], 'work_continue', dict(
            task_id=identity, message='The review passed. Verify locally; do not publish.', retained_pr_url=URL[:-1] + '2'))
        self.check_dispatch('done')
        work, pr, report, requester_pr, second = saved
        self.assertEqual(second['url'], URL[:-1] + '2')
        owner = self.service._get('tasks', pr['task_id'])
        # The named PR is the Work item and follows the successor card. No Work row or other artifact appears.
        self.assertEqual(owner['_work_context_id'], second['id'])
        self.assertEqual(self.service._get('artifacts', second['id'])['_native_outcome_id'],
                         self.service.kanban.tasks[owner['id']]['id'])
        self.assertEqual({a['id'] for a in self.service._all('artifacts')}, artifacts)
        for item in (pr, report, requester_pr):
            current = self.service._get('artifacts', item['id'])
            self.assertEqual({k: current.get(k) for k in ('task_id', 'url', 'pr_state', 'work_outcome_id', '_native_outcome_id')},
                             {k: item.get(k) for k in ('task_id', 'url', 'pr_state', 'work_outcome_id', '_native_outcome_id')})
        self.assertEqual(self.service._get('artifacts', work['id']), work)

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
