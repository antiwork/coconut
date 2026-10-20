"""A delegate's hidden Work report and its delegated PR retain one execution and one outcome."""
import json
import unittest
from uuid import uuid4

from coconut.service import ServiceError
import test_native_claim_continuation as native_fixtures
import test_native_work_continuation as fixtures

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


@unittest.skipUnless(native_fixtures.fixtures.SOURCE and native_fixtures.fixtures.PYTHON,
                     'Set Coconut Hermes paths for native integration checks.')
class NativeDelegatedReportContinuationTests(unittest.TestCase):
    setUp = native_fixtures.NativeClaimContinuationTests.setUp
    write = native_fixtures.NativeClaimContinuationTests.write
    native_program = native_fixtures.NativeClaimContinuationTests.native_program
    prepare = native_fixtures.NativeClaimContinuationTests.prepare
    resume = native_fixtures.NativeClaimContinuationTests.resume
    check_dispatch = native_fixtures.NativeClaimContinuationTests.check_dispatch

    def linked(self, second_pr=False):
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
            items = [work, pr, report, requester_pr]
            if second_pr:
                items.append(dict(pr, id=str(uuid4()), url=URL[:-1] + '2'))
            for item in items:
                self.service._put('artifacts', item)
            saved.extend(items)
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
