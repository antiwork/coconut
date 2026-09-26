import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_automatic_work as fixtures
from coconut.hermes_worker import conversation_input
from coconut.service import ServiceError, YieldForDecision


class DecisionFreshnessTests(unittest.TestCase):
    setUp = fixtures.AutomaticWorkTests.setUp
    tearDown = fixtures.AutomaticWorkTests.tearDown
    start = fixtures.AutomaticWorkTests.start
    write = fixtures.AutomaticWorkTests.write

    def question(self, old=True):
        self.start()
        try:
            self.service.native_event(self.task['id'], 'native_question', dict(
                title='Install the test dependencies?', context='The requested fix needs local tests.',
                options=[dict(id='install', label='Install'), dict(id='leave', label='Leave')]))
        except YieldForDecision:
            pass
        self.service.running.clear()
        decision = self.service._all('decisions')[-1]
        if old:
            decision['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
            decision['_freshness']['checked_at'] = decision['created_at']
            self.service._put('decisions', decision)
            task = self.service._get('tasks', self.task['id'])
            task['summary'] = 'The dependency source has changed.'
            self.service._put('tasks', task)
        return decision

    def refresh(self):
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        context['status'] = 'working'
        self.service._put('contexts', context)
        return context

    def finish(self, context):
        context = self.service._get('contexts', context['id'])
        context['status'] = 'completed'
        self.service._put('contexts', context)

    def test_a_refresh_pass_parses_each_table_once_for_many_questions(self):
        decision = self.question(old=False)
        for index in range(4):
            self.service._put('decisions', dict(decision, id=f'extra-{index}', title=f'Question {index}?'))
        original, reads = self.service._all, []
        def read(table):
            reads.append(table)
            return original(table)
        # The service lock is held for the whole pass, so each whole-table read blocks every other thread.
        with patch.object(self.service, '_all', side_effect=read):
            self.service._queue_decision_refreshes()
        self.assertEqual(reads.count('artifacts'), 1)
        self.assertEqual(reads.count('tasks'), 1)
        self.assertLessEqual(reads.count('messages'), 1)

    def test_a_card_write_in_the_refresh_pass_keeps_the_other_shared_tables(self):
        decision = self.question()
        for index in range(4):
            extra = dict(decision, id=f'extra-{index}', title=f'Question {index}?', risk='It can change the order.')
            extra['_freshness'] = dict(decision['_freshness'], fingerprint='old')
            self.service._put('decisions', extra)
        original, reads = self.service._all, []
        def read(table):
            reads.append(table)
            return original(table)
        with patch.object(self.service, '_all', side_effect=read):
            self.service._queue_decision_refreshes()
        # The pass rewrote the changed cards. Messages and artifacts are still parsed once.
        self.assertTrue(all(self.service._get('decisions', f'extra-{i}')['assessment_stale'] for i in range(4)))
        self.assertEqual(reads.count('messages'), 1)
        self.assertEqual(reads.count('artifacts'), 1)

    def test_shared_reads_see_each_write(self):
        with self.service.transaction(), self.service._shared_reads():
            first = self.service._task_map()
            self.assertIs(self.service._task_map(), first)
            self.assertIs(self.service._all_shared('tasks'), self.service._all_shared('tasks'))
            task = self.service._get('tasks', self.task['id'])
            task['summary'] = 'Changed in this pass.'
            self.service._put('tasks', task)
            self.assertEqual(self.service._task_map()[task['id']]['summary'], 'Changed in this pass.')
            self.assertIsNot(self.service._task_map(), first)
        self.assertIsNot(self.service._task_map(), self.service._task_map())

    def test_a_write_to_a_graph_table_drops_the_shared_graphs(self):
        from coconut.service import now
        rows = {'tasks': self.service._get('tasks', self.task['id']),
                'artifacts': dict(id='graph-artifact', kind='work', bot_id=self.task['bot_id'], task_id=self.task['id'],
                                  title='Graph', status='active'),
                'decisions': dict(self.question(old=False), id='graph-decision'),
                'native_receipts': dict(id='graph-receipt', task_id=self.task['id'], turn_id='turn', state='completed',
                                        created_at=now(), action={'name': 'terminal', 'arguments': {}})}
        self.assertEqual(set(rows), self.service.GRAPH_TABLES)
        with self.service.transaction(), self.service._shared_reads():
            for table, row in rows.items():
                self.service._graph_memo()['probe'] = True
                self.service._put(table, row)
                self.assertNotIn('probe', self.service._graph_memo(), table)
            # A write to another table keeps the graphs.
            self.service._graph_memo()['probe'] = True
            self.service._put('bots', self.service._get('bots', self.task['bot_id']))
            self.assertIn('probe', self.service._graph_memo())

    def test_other_threads_and_read_views_do_not_see_shared_reads(self):
        import threading
        seen = []
        with self.service.transaction(), self.service._shared_reads():
            shared = self.service._task_map()
            other = threading.Thread(target=lambda: seen.append(self.service._shared_cache()))
            other.start()
            other.join()
            with self.service._read_view() as view:
                self.assertIsNone(view._shared_cache())
                self.assertIsNot(view._task_map(), shared)
            self.assertIs(self.service._task_map(), shared)
        self.assertEqual(seen, [None])
        # Another thread waits for the service lock before it opens its own block.
        with self.service._shared_reads():
            opened = threading.Event()
            def other_block():
                with self.service._shared_reads():
                    opened.set()
            other = threading.Thread(target=other_block)
            other.start()
            self.assertFalse(opened.wait(0.2))
        other.join()
        self.assertTrue(opened.is_set())

    def test_changed_source_hides_assessment_until_fenced_check_confirms_it(self):
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision.update(sources=[dict(kind='pull_request', title='Release', url=url)],
            risk_level='low', risk='Only the release note changes.',
            uncertainty='The publication date remains undecided.')
        decision['options'][0].update(risk_level='medium', risk='Customers will see this date.')
        self.service._put('decisions', decision)
        observation = SimpleNamespace(observed=True, pr_state='open', base_branch='main', head_sha='first-head',
            title='Release', draft=False, checks_state='passed', required_checks_state='passed',
            review_checks='approved', check_failures=None)
        tracker = SimpleNamespace(observe=lambda _: observation, observe_discussion=lambda _: 'Checked date')
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({})
            saved = self.service._get('decisions', decision['id'])
            self.assertTrue(self.service._public(saved)['assessment_stale'])
            self.assertEqual(saved['status'], 'pending')
            context = self.refresh()
            self.assertIn('Customers will see this date.', context['question'])
            self.assertIn('publication date remains undecided', context['question'])
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked the first head and its date.'))
            self.finish(context)
            saved = self.service._get('decisions', decision['id'])
            self.assertFalse(saved['assessment_stale'])
            observation.head_sha = 'second-head'
            self.service._refresh_decision_sources({})
            saved = self.service._get('decisions', decision['id'])
            self.assertTrue(saved['assessment_stale'])
            saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
            self.service._put('decisions', saved)
            context = self.refresh()
            observation.head_sha = 'third-head'
            self.service._refresh_decision_sources({})
            with self.assertRaises(ServiceError):
                self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked the second head.'))
            self.assertTrue(self.service._get('decisions', decision['id'])['assessment_stale'])

    def test_changed_work_hides_assessment_and_revision_replaces_it(self):
        decision = self.question()
        decision.update(risk_level='low', risk='Only the local test folder changes.')
        self.service._put('decisions', decision)
        context = self.refresh()
        self.assertTrue(self.service._get('decisions', decision['id'])['assessment_stale'])
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The setup now affects shared data.',
            revision=dict(title='Change the shared test setup?', options=decision['options'],
                risk_level='high', risk='Other tests can lose their data.')))
        saved = self.service._get('decisions', decision['id'])
        self.assertFalse(saved['assessment_stale'])
        self.assertEqual(saved['risk_level'], 'high')
        self.assertEqual(saved['status'], 'pending')

    def test_work_change_invalidates_assessment_even_when_refresh_cannot_start(self):
        from coconut.service import now
        decision = self.question(old=False)
        decision.update(risk_level='low', risk='Only local files change.')
        self.service._put('decisions', decision)
        task = self.service._get('tasks', self.task['id'])
        task['summary'] = 'The scope now includes shared files.'
        self.service._put('tasks', task)
        for blocked in ('retry_interval', 'session_busy', 'another_refresh'):
            with self.subTest(blocked=blocked):
                saved = self.service._get('decisions', decision['id'])
                saved['assessment_stale'] = False
                saved['_freshness']['queued_at'] = now() if blocked == 'retry_interval' else '2000-01-01T00:00:00Z'
                self.service._put('decisions', saved)
                if blocked == 'another_refresh':
                    self.service._put('contexts', dict(id='busy', task_id=task['id'], bot_id=task['bot_id'],
                        decision_id=decision['id'], status='working', _decision_refresh=True))
                with patch.object(self.service, '_refresh_session_busy', return_value=blocked == 'session_busy'), \
                     patch.object(self.service, '_cancel_stale_decision_refreshes'):
                    self.service._queue_decision_refreshes()
                self.assertTrue(self.service._get('decisions', decision['id'])['assessment_stale'])
                self.assertEqual(len(self.service._all('contexts')), 1 if blocked == 'another_refresh' else 0)

    def test_temporary_change_still_gets_a_check_after_facts_return_to_original(self):
        from coconut.service import now
        decision = self.question(old=False)
        decision.update(risk_level='low', risk='Only local files change.')
        decision['_freshness']['queued_at'] = now()
        self.service._put('decisions', decision)
        task = self.service._get('tasks', self.task['id'])
        self.service._put('tasks', dict(task, summary='A temporary change.'))
        self.service._queue_decision_refreshes()
        saved = self.service._get('decisions', decision['id'])
        self.assertTrue(saved['assessment_stale'])
        self.assertEqual(self.service._all('contexts'), [])
        self.service._put('tasks', task)
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The original scope still applies.'))
        self.assertFalse(self.service._get('decisions', decision['id'])['assessment_stale'])

    def test_uncertainty_alone_is_invalidated_when_work_changes(self):
        decision = self.question()
        decision['uncertainty'] = 'The test dependency is unavailable.'
        self.service._put('decisions', decision)
        context = self.refresh()
        self.assertTrue(self.service._get('decisions', decision['id'])['assessment_stale'])
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The dependency is now available.',
            revision=dict(title=decision['title'], options=decision['options'], uncertainty='')))
        saved = self.service._get('decisions', decision['id'])
        self.assertFalse(saved['assessment_stale'])
        self.assertEqual(saved['uncertainty'], '')

    def test_no_pending_cards_or_refreshes_skip_historical_collections(self):
        with patch.object(self.service, '_all', side_effect=AssertionError('No card needs observation')):
            self.service._queue_decision_refreshes()

    def test_scoped_receipts_keep_journal_order_when_delivery_is_acknowledged(self):
        decision = self.question()
        records = [dict(id='earlier', task_id=self.task['id'], state='completed', action={'name': 'terminal'}, result='{}'),
                   dict(id='later', task_id=self.task['id'], state='completed', action={'name': 'delegate_task'},
                        result=json.dumps({'status': 'dispatched', 'delegation_id': 'native-worker'}),
                        _process_delivery='consumed', _delegation_ack_pending=True)]
        for record in records:
            self.service._put('native_receipts', record)
        tasks = {t['id']: t for t in self.service._all('tasks')}
        artifacts = self.service._all('artifacts')
        before = self.service._decision_records(decision, tasks, artifacts)
        fingerprint, _ = self.service._decision_evidence(decision, tasks, before, artifacts)
        records[1]['_delegation_ack_pending'] = False
        self.service._put('native_receipts', records[1])
        after = self.service._decision_records(decision, tasks, artifacts)
        self.assertEqual([r['id'] for r in before], [r['id'] for r in after])
        self.assertEqual(fingerprint, self.service._decision_evidence(decision, tasks, after, artifacts)[0])

    def test_card_checks_read_only_related_tool_history(self):
        original = self.service._all
        def scoped(table):
            self.assertNotEqual(table, 'native_receipts', 'A card check must not read the full tool journal')
            return original(table)
        with patch.object(self.service, '_all', side_effect=scoped):
            self.question()
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='The original choice still needs an answer.'))
        self.assertTrue(self.service._get('contexts', context['id'])['_refresh_recorded'])

    def test_changed_evidence_queues_one_silent_check_without_a_human_message(self):
        self.question()
        messages = self.service._all('messages')
        self.refresh()
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.assertEqual(self.service._all('messages'), messages)
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')

    def test_age_alone_never_rechecks_an_unchanged_card(self):
        decision = self.question(old=False)
        decision['created_at'] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        decision['_freshness']['checked_at'] = decision['created_at']
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_changed_issue_facts_refresh_once_and_failed_reads_keep_evidence(self):
        decision = self.question(old=False)
        url = 'https://github.com/example/project/issues/12'
        decision['sources'] = [dict(kind='issue', title='Original issue', url=url + '#issuecomment-1')]
        self.service._put('decisions', decision)
        facts = dict(state='open', title='Original issue', updated_at='2026-09-18T00:00:00Z', comments=1)
        tracker = SimpleNamespace(observe_issue=lambda _: facts)
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({})
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Read the linked issue.'))
            self.finish(context)
            self.service._refresh_decision_sources({})
            self.service._queue_decision_refreshes()
            self.assertEqual(len(self.service._all('contexts')), 1)
            tracker.observe_issue = lambda _: None
            self.service._refresh_decision_sources({})
            self.assertEqual(self.service._get('decisions', decision['id'])['_source_facts'][url], facts)
            tracker.observe_issue = lambda _: dict(facts, state='closed', comments=2)
            self.service._refresh_decision_sources({})
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_cards_reuse_one_pr_observation_and_new_review_triggers_a_check(self):
        from unittest.mock import Mock
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        observation = SimpleNamespace(observed=True, pr_state='open', base_branch='main', head_sha='head', title='Fix', draft=False,
            checks_state='passed', required_checks_state='passed', review_checks='first-review', check_failures=None)
        tracker = Mock()
        tracker.observe_discussion.return_value = 'first-discussion'
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({(decision['bot_id'], url): observation})
            tracker.observe.assert_not_called()
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked the current review.'))
            self.finish(context)
            observation.review_checks = 'new-review'
            self.service._refresh_decision_sources({(decision['bot_id'], url): observation})
        saved = self.service._get('decisions', decision['id'])
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_pr_comment_change_refreshes_a_card_with_unchanged_checks(self):
        from unittest.mock import Mock
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        observation = SimpleNamespace(observed=True, pr_state='open', base_branch='main', head_sha='head', title='Fix', draft=False,
            checks_state='passed', required_checks_state='passed', review_checks='same-review', check_failures=None)
        tracker = Mock()
        tracker.observe_discussion.return_value = 'before-comment'
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({(decision['bot_id'], url): observation})
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Read current comments.'))
            self.finish(context)
            tracker.observe_discussion.return_value = 'after-comment'
            self.service._refresh_decision_sources({(decision['bot_id'], url): observation})
        saved = self.service._get('decisions', decision['id'])
        saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', saved)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_pr_discussion_refresh_does_not_require_checks_permission(self):
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision['sources'] = [dict(kind='pull_request', title='Review changes', url=url + '/files?diff=split#review')]
        self.service._put('decisions', decision)
        tracker = SimpleNamespace(observe_discussion=lambda _: 'new-discussion')
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({(decision['bot_id'], url): SimpleNamespace(observed=False)})
        self.assertEqual(self.service._get('decisions', decision['id'])['_source_facts'],
                         {url: {'discussion': 'new-discussion'}})
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_partial_check_failure_preserves_the_last_same_head_fact(self):
        from unittest.mock import Mock
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        value = SimpleNamespace(observed=True, pr_state='open', base_branch='main', head_sha='head', title='Fix', draft=False,
            checks_state='passed', required_checks_state='passed', review_checks=None, check_failures=None)
        artifact = dict(id='linked-facts', kind='pr', task_id=self.task['id'], bot_id=self.bot['id'],
            title='Fix', status='ready_for_review', created_at=decision['created_at'], url=url, head_sha='head',
            checks_state='passed', required_checks_state='passed')
        self.service._put('artifacts', artifact)
        tracker = Mock()
        tracker.observe_discussion.return_value = 'discussion'
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            before = self.service._get('decisions', decision['id'])['_source_facts']
            context = self.refresh()
            self.service.refresh_decision(context['id'], dict(status='current', evidence='Checked current source facts.'))
            self.finish(context)
            value.required_checks_state = None
            artifact['required_checks_state'] = None
            self.service._put('artifacts', artifact)
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            self.assertEqual(self.service._get('decisions', decision['id'])['_source_facts'], before)
            saved = self.service._get('decisions', decision['id'])
            saved['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
            self.service._put('decisions', saved)
            self.service._queue_decision_refreshes()
            self.assertEqual(len(self.service._all('contexts')), 1)
            value.base_branch = 'release'
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            self.assertIsNone(self.service._get('decisions', decision['id'])['_source_facts'][url]['required_checks_state'])
            value.required_checks_state = 'passed'
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            value.required_checks_state = None
            value.required_checks_observed = True
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            self.assertIsNone(self.service._get('decisions', decision['id'])['_source_facts'][url]['required_checks_state'])
            value.required_checks_observed = False
            value.head_sha = 'new-head'
            self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            self.assertIsNone(self.service._get('decisions', decision['id'])['_source_facts'][url]['required_checks_state'])

    def test_partial_pr_read_invalidates_checks_for_a_changed_head_or_target(self):
        decision = self.question(old=False)
        url = 'https://github.com/example/project/pull/42'
        decision['merge_pr_url'] = url
        tracker = SimpleNamespace(observe_discussion=lambda _: None)
        for head, base in (('new-head', 'main'), ('head', 'release')):
            decision['_source_facts'] = {url: dict(head_sha='head', base_branch='main', required_checks_state='passed')}
            self.service._put('decisions', decision)
            value = SimpleNamespace(observed=False, head_sha=head, base_branch=base)
            with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
                self.service._refresh_decision_sources({(decision['bot_id'], url): value})
            facts = self.service._get('decisions', decision['id'])['_source_facts'][url]
            self.assertEqual(facts['head_sha'], head)
            self.assertEqual(facts['base_branch'], base)
            self.assertNotIn('required_checks_state', facts)

    def test_final_pr_identity_read_invalidates_pending_card_checks(self):
        from test_pr_tracking import tracker_for, pull, check, SHA, URL
        decision = self.question(old=False)
        decision['merge_pr_url'] = URL
        for final in (pull(head={'sha': 'b' * 40}, base={'ref': 'main'}), pull(base={'ref': 'release'})):
            decision['_source_facts'] = {URL: dict(head_sha=SHA, base_branch='main', required_checks_state='passed')}
            self.service._put('decisions', decision)
            tracker = tracker_for([check()], before=pull(base={'ref': 'main'}), after=final)
            with patch.object(tracker, '_required_checks', return_value=('passed', True)), \
                 patch.object(tracker, 'observe_discussion', return_value=None), \
                 patch.object(self.service, '_pr_tracker_for', return_value=tracker):
                self.service._refresh_decision_sources({})
            facts = self.service._get('decisions', decision['id'])['_source_facts'][URL]
            self.assertEqual(facts['head_sha'], final['head']['sha'])
            self.assertEqual(facts['base_branch'], final['base']['ref'])
            self.assertNotIn('required_checks_state', facts)

    def test_source_observation_cannot_replace_a_card_revised_during_fetch(self):
        decision = self.question(old=False)
        decision['sources'] = [dict(kind='issue', title='Original issue', url='https://github.com/example/project/issues/12')]
        self.service._put('decisions', decision)
        def observe(_):
            current = self.service._get('decisions', decision['id'])
            current['revision'] += 1
            self.service._put('decisions', current)
            return dict(state='closed')
        with patch.object(self.service, '_pr_tracker_for', return_value=SimpleNamespace(observe_issue=observe)):
            self.service._refresh_decision_sources({})
        self.assertNotIn('_source_facts', self.service._get('decisions', decision['id']))

    def test_fresh_card_waits_but_changed_work_queues_a_check(self):
        self.question(old=False)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])
        task = self.service._get('tasks', self.task['id'])
        task['summary'] = 'New source evidence arrived'
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_current_card_does_not_repeat_a_model_check_on_each_scan(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The verified account choice still requires the owner.'))
        self.finish(context)
        for _ in range(3):
            self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], 1)

    def peer_question(self, decision, **fields):
        decision_task = self.service._get('tasks', decision['task_id'])
        task = dict(decision_task, id='independent-task',
                    exchange_id='independent-exchange', status='working')
        exchange = dict(self.service._get('exchanges', decision_task['exchange_id']), id=task['exchange_id'])
        self.service._put('exchanges', exchange)
        self.service._put('tasks', task)
        from coconut.decision_sources import github_links
        revision = '2026-09-23T11:46:31Z'
        fields['source_revisions'] = {url: revision for url in github_links(fields)}
        tracker = SimpleNamespace(observe_issue=lambda url: dict(updated_at=revision))
        with patch.object(self.service, '_pr_tracker_for', return_value=tracker):
            try:
                self.service.native_event(task['id'], 'native_question', dict(
                    title='Publish the unrelated report?', risk='Readers can see the report.',
                    options=[dict(id='publish', label='Publish'), dict(id='leave', label='Leave')], **fields))
            except YieldForDecision:
                pass
        return self.service._all('decisions')[-1]

    def test_unrelated_cards_in_one_chat_do_not_invalidate_an_assessment(self):
        decision = self.question(old=False)
        decision.update(risk='The dependency changes local files.')
        self.service._put('decisions', decision)
        peer = self.peer_question(decision)
        for state in ('created', 'revised', 'resolved'):
            with self.subTest(state=state):
                if state == 'revised':
                    peer.update(revision=2, risk='The report becomes public.')
                    self.service._put('decisions', peer)
                    self.service._initialize_decision_freshness(peer)
                elif state == 'resolved':
                    peer['status'] = 'resolved'
                    self.service._put('decisions', peer)
                self.service._queue_decision_refreshes()
                self.assertFalse(self.service._get('decisions', decision['id']).get('assessment_stale'))
                self.assertEqual(self.service._all('contexts'), [])

    def test_unrelated_card_change_does_not_reject_a_current_check(self):
        decision = self.question()
        peer = self.peer_question(decision)
        context = self.refresh()
        self.assertEqual(context['decision_id'], decision['id'])
        self.assertNotIn(peer['title'], context['question'])
        peer['revision'] += 1
        self.service._put('decisions', peer)
        self.service.refresh_decision(context['id'], dict(status='current', evidence='The local scope is unchanged.'))
        self.assertFalse(self.service._get('decisions', decision['id'])['assessment_stale'])

    def test_delegated_card_publication_does_not_change_its_evidence(self):
        self.start()
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        delivery = self.service._store_bot_message(self.task['id'], dict(
            target_bot_id=bot['id'], text='Verify the local scope.', require_response=True))
        task = self.service._get('tasks', delivery['task_id'])
        task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        try:
            self.service.native_event(task['id'], 'native_question', dict(
                title='Use the local database?', risk='Only local test data changes.',
                options=[dict(id='use', label='Use it'), dict(id='leave', label='Leave')]))
        except YieldForDecision:
            pass
        decision = self.service._all('decisions')[-1]
        tasks = {t['id']: t for t in self.service._all('tasks')}
        artifacts = self.service._all('artifacts')
        current, _ = self.service._decision_evidence(decision, tasks,
            self.service._decision_records(decision, tasks, artifacts), artifacts)
        self.assertEqual(current, decision['_freshness']['fingerprint'])

    def shared_source_fences_check(self, kind, url, peer_url):
        decision = self.question()
        decision['sources'] = [dict(kind=kind, title='Original source', url=url)]
        self.service._put('decisions', decision)
        peer = self.peer_question(decision, sources=[dict(kind=kind, title='Same source', url=peer_url)])
        context = self.refresh()
        self.assertIn(peer['title'], context['question'])
        peer['revision'] += 1
        self.service._put('decisions', peer)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The peer covers this.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_shared_pr_in_an_independent_task_still_fences_a_check(self):
        self.shared_source_fences_check('pull_request', 'https://github.com/example/project/pull/42',
            'https://github.com/Example/Project/pull/42#issuecomment-7')

    def test_shared_pr_issue_alias_still_fences_a_check(self):
        self.shared_source_fences_check('pull_request', 'https://github.com/example/project/pull/42',
            'https://github.com/Example/Project/issues/42#issuecomment-7')

    def test_distinct_github_numbers_do_not_share_assessment_changes(self):
        decision = self.question(old=False)
        decision['sources'] = [dict(kind='issue', title='First issue',
                                   url='https://github.com/example/project/issues/42')]
        self.service._put('decisions', decision)
        peer = self.peer_question(decision, sources=[dict(kind='pull_request', title='Other PR',
            url='https://github.com/example/project/pull/43')])
        peer['revision'] += 1
        self.service._put('decisions', peer)
        self.service._initialize_decision_freshness(peer)
        self.service._queue_decision_refreshes()
        self.assertFalse(self.service._get('decisions', decision['id']).get('assessment_stale'))
        self.assertEqual(self.service._all('contexts'), [])

    def test_shared_email_in_an_independent_task_still_fences_a_check(self):
        self.shared_source_fences_check('email', 'https://mail.google.com/mail/u/0/#inbox/18f2c9a10b7d4321',
            'https://mail.google.com/mail/u/1/#all/18f2c9a10b7d4321')

    def test_shared_email_query_link_still_fences_a_check(self):
        self.shared_source_fences_check('email', 'https://mail.google.com/mail/u/0/#inbox/18f2c9a10b7d4321',
            'https://mail.google.com/mail/?view=pt&search=all&th=18f2c9a10b7d4321')

    def distinct_email_sources(self, first, second):
        decision = self.question(old=False)
        decision.update(risk='The email discloses the report.',
            sources=[dict(kind='email', title='First email', url=first)])
        self.service._put('decisions', decision)
        peer = self.peer_question(decision, sources=[dict(kind='email', title='Second email', url=second)])
        peer['revision'] += 1
        self.service._put('decisions', peer)
        self.service._initialize_decision_freshness(peer)
        self.service._queue_decision_refreshes()
        self.assertFalse(self.service._get('decisions', decision['id']).get('assessment_stale'))
        self.assertEqual(self.service._all('contexts'), [])

    def test_distinct_email_threads_do_not_share_assessment_changes(self):
        self.distinct_email_sources('https://mail.google.com/mail/u/0/#inbox/18f2c9a10b7d4321',
            'https://mail.google.com/mail/u/1/#inbox/18f2c9a10b7d4322')

    def test_distinct_email_searches_do_not_share_assessment_changes(self):
        self.distinct_email_sources('https://mail.google.com/mail/u/0/#search/after%3A2026%2F09%2F01',
            'https://mail.google.com/mail/u/1/#search/before%3A2026%2F08%2F01')

    def test_distinct_gmail_pages_do_not_share_assessment_changes(self):
        self.distinct_email_sources('https://mail.google.com/mail/u/0/#search/invoices/p2',
            'https://mail.google.com/mail/u/1/#search/receipts/p2')

    def test_shared_gmail_search_thread_still_fences_a_check(self):
        self.shared_source_fences_check('email', 'https://mail.google.com/mail/u/0/#inbox/FMfcgzQZSxLmNBhYwTpjAKrDV',
            'https://mail.google.com/mail/u/1/#search/after%3A2026%2F09%2F01/FMfcgzQZSxLmNBhYwTpjAKrDV')

    def test_refresh_revises_same_card_and_rejects_an_old_choice(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The source no longer needs installation.',
            revision=dict(title='Use the new account?', context='The old account closed.', risk='This changes the destination.',
                          options=[dict(id='new', label='New account'), dict(id='leave', label='Leave')], recommended_option_id='new')))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['revision'], saved['title'], saved['status']), (2, 'Use the new account?', 'pending'))
        self.assertIsNone(saved['decided_by'])
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))

    def test_copy_revision_keeps_the_pr_link_and_records_its_head(self):
        decision = self.question()
        url = 'https://github.com/example/project/pull/42'
        self.service._put('artifacts', dict(id='linked-pr', kind='pr', task_id=self.task['id'], bot_id=self.bot['id'],
            title='Fix the export', url=url, head_sha='reviewed-head', status='ready_for_review', created_at=decision['created_at']))
        decision['merge_pr_url'] = url
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='The current head is ready.',
            revision=dict(title='Merge the export fix?', context='The tests pass.', options=decision['options'])))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['merge_pr_url'], saved['_merge_pr_head']), (url, 'reviewed-head'))
        self.service._revise_question(saved, self.service._get('tasks', self.task['id']),
            dict(title='Deploy this change?', options=decision['options'], merge_pr_url=None), 'New action.')
        self.assertIsNone(saved['merge_pr_url'])
        self.assertIsNone(saved['_merge_pr_head'])

    def test_recovery_revision_preserves_fixed_actions(self):
        decision = self.question()
        decision.update(_recovery_work_ids=['work'], options=[dict(id='retry', label='Retry'), dict(id='stop', label='Stop')])
        self.service._put('decisions', decision)
        context = self.refresh()
        revision = dict(title='Continue the interrupted test?', context='The tool result is available.',
                        options=[dict(id='retry', label='Retry'), dict(id='stop-work', label='Stop this work')])
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='revise', evidence='Read the tool result.', revision=revision))
        self.assertEqual(self.service._get('decisions', decision['id'])['options'], decision['options'])
        revision['options'] = decision['options']
        self.service.refresh_decision(context['id'], dict(status='revise', evidence='Read the tool result.', revision=revision))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['title'], revision['title'])
        self.assertEqual([(o['id'], o['label']) for o in saved['options']], [('retry', 'Retry'), ('stop', 'Stop')])

    def test_callback_card_includes_changes_from_a_sibling_delegate(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        child = self.service._store_bot_message(owner['id'], dict(target_bot_id=bot['id'], text='Verify result', require_response=True))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'needs_you'
        self.service._put('tasks', owner)
        callback = dict(owner, id='parent-callback', _assignment_task_id=owner['id'],
                        source_task_id='another-helper', _is_return=True,
                        created_at=datetime.now(timezone.utc).isoformat())
        self.service._put('tasks', callback)
        decision['task_id'] = callback['id']
        self.service._put('decisions', decision)
        context = self.refresh()
        helper = self.service._get('tasks', child['task_id'])
        helper['summary'] = 'New evidence contradicts the pending question.'
        self.service._put('tasks', helper)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old evidence.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_withdrawal_resumes_existing_task_without_fabricating_approval(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The requested local fix already covers isolated test setup.'))
        saved = self.service._get('decisions', decision['id'])
        task = self.service._get('tasks', self.task['id'])
        self.assertEqual(saved['status'], 'resolved')
        self.assertIsNone(saved['decided_by'])
        self.assertEqual((task['status'], task['_resume_decision']), ('queued', decision['id']))
        prompt, _ = conversation_input(dict(operation='resume', task=task, decision=saved, history=[]))
        self.assertIn('No option was selected', prompt)

    def test_closed_source_with_completed_exact_action_can_withdraw(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw',
            evidence='The exact requested release is recorded complete at the source URL.'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['status'], 'resolved')
        self.assertIsNone(saved['decided_by'])

    def test_closed_source_with_remaining_followup_revises_same_card(self):
        decision = self.question()
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='revise',
            evidence='The source is closed, but deployment remains unverified.',
            revision=dict(title='Deploy the completed change?', context='Implementation finished; deployment remains.',
                          options=[dict(id='deploy', label='Deploy'), dict(id='later', label='Later')])))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision'], saved['title']),
                         ('pending', 2, 'Deploy the completed change?'))

    def test_rejected_decision_never_queues_a_freshness_check(self):
        decision = self.question()
        decision['status'] = 'rejected'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_source_change_during_check_rejects_stale_withdrawal(self):
        decision = self.question()
        context = self.refresh()
        task = self.service._get('tasks', self.task['id'])
        task['summary'] = 'New evidence contradicts the old result'
        self.service._put('tasks', task)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old source result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_refresh_reads_do_not_invalidate_their_own_evidence(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'saved-test-result'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result='Verified test result'), context_id=context['id'])
        result = self.service.refresh_decision(context['id'], dict(status='current', evidence='The saved result confirms the current question.'))
        self.assertEqual(result['status'], 'recorded')

    def process_receipt(self):
        task = self.service._get('tasks', self.task['id'])
        receipt = dict(id='original-process', task_id=task['id'], turn_id=task['_native_turn_id'],
            action=dict(name='terminal', arguments={'command': 'run tests'}), state='completed',
            completed_at=task['created_at'], created_at=task['created_at'],
            result=json.dumps(dict(session_id='test-process', notify_on_complete=True)))
        self.service._put('native_receipts', receipt)
        return receipt

    def test_new_process_output_invalidates_an_older_check(self):
        decision = self.question()
        receipt = self.process_receipt()
        context = self.refresh()
        receipt['_completion_text'] = 'The tests failed.'
        self.service._put('native_receipts', receipt)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Tests still running.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_cancelling_a_refresh_clears_bot_activity(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'completed'
        self.service._put('tasks', owner)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service._refresh_bot(self.bot['id'])
        self.assertEqual(self.service._get('bots', self.bot['id'])['status'], 'working')
        decision = self.service._get('decisions', decision['id'])
        decision['status'] = 'rejected'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('bots', self.bot['id'])['status'], 'idle')

    def test_maintenance_does_not_replace_failed_human_reply_recovery(self):
        decision = self.question()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Use the local setup.', revision=1))
        human = self.service._all('contexts')[-1]
        human['status'] = 'failed'
        self.service._put('contexts', human)
        automatic = self.refresh()
        self.finish(automatic)
        self.service._queue_context_recoveries()
        saved = self.service._get('contexts', human['id'])
        retry = self.service._get('contexts', saved['_recovery_context_id'])
        self.assertEqual(retry['_reply_device'], human['_reply_device'])
        self.assertFalse(retry.get('_decision_refresh'))

    def test_maintenance_read_preserves_delivery_to_the_process_owner(self):
        decision = self.question()
        original = self.process_receipt()
        context = self.refresh()
        read = self.service.native_event(self.task['id'], 'native_call',
            dict(name='process', arguments={'session_id': 'test-process'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=read['id'],
            result=json.dumps(dict(status='exited', session_id='test-process', output='Tests passed'))), context_id=context['id'])
        self.assertFalse(self.service._get('native_receipts', original['id']).get('_process_delivery'))
        self.assertTrue(self.service._background_pending(self.task['id']))
        self.assertIn('Tests passed', self.service._get('native_receipts', original['id'])['_completion_text'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        task = self.service._get('tasks', self.task['id'])
        task['status'] = 'waiting'
        self.service._put('tasks', task)
        self.service._migrate_process_deliveries()
        self.assertFalse(self.service._get('native_receipts', original['id']).get('_process_delivery'))
        self.service.provider.process_completions = lambda bot, processes: self.fail('The saved result must not be fetched again.')
        self.service._drain_process_completions()
        resumed = self.service._get('tasks', self.task['id'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertIn('Tests passed', str(resumed['_background_completion']))

    def test_poller_preserves_an_owner_result_before_the_refresh_read_returns(self):
        self.question()
        original = self.process_receipt()
        context = self.refresh()
        self.service.native_event(self.task['id'], 'native_call',
            dict(name='process', arguments={'session_id': 'test-process'}), context_id=context['id'])
        self.provider.process_completions = lambda bot, processes: [dict(id=original['id'], consumed=True, text='Tests passed')]
        self.service._drain_process_completions()
        saved = self.service._get('native_receipts', original['id'])
        self.assertFalse(saved.get('_process_delivery'))
        self.assertEqual(saved['_completion_text'], 'Tests passed')

    def test_outstanding_refresh_process_prevents_duplicate_research(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record.update(id='refresh-process', turn_id=context['id'])
        self.service._put('native_receipts', record)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.provider.process_completions = lambda bot, processes: [dict(id=record['id'], text='Research is complete')]
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertEqual(self.service._all('contexts')[-1]['_background_completion'], ['Research is complete'])

    def test_interrupted_refresh_passes_unknown_effects_to_its_retry(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect result'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn(receipt['id'], retry['_context_recovery'])
        self.assertEqual(retry['_recovery_unknown_tools'][0]['id'], receipt['id'])

    def test_cancelled_check_preserves_a_late_synchronous_result(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect result'}), context_id=context['id'])
        context['status'] = 'cancelled'
        self.service._put('contexts', context)
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result='The local dependency is installed and verified.'), context_id=context['id'])
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn('The local dependency is installed and verified.', retry['_context_recovery'])
        self.assertEqual(retry['_recovery_unknown_tools'][0]['state'], 'completed')

    def test_recovery_card_checks_receipts_from_previous_work_attempts(self):
        decision = self.question()
        record = self.process_receipt()
        previous = self.service._get('tasks', self.task['id'])
        current = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Recover this outcome', mode='queue'))
        current = self.service._get('tasks', current['id'])
        current.update(status='needs_you', _conversation_epoch=previous['_conversation_epoch'])
        self.service._put('tasks', current)
        self.service._put('artifacts', dict(id='work-history', kind='work', bot_id=self.bot['id'], task_id=current['id'],
            _work_previous_task_ids=[previous['id']], title='Verify the export', status='blocked', created_at=previous['created_at']))
        decision.update(task_id=current['id'], _recovery_work_ids=['work-history'])
        self.service._put('decisions', decision)
        context = self.refresh()
        record['_completion_text'] = 'Export verification failed.'
        self.service._put('native_receipts', record)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result.'))

    def test_answer_interrupts_only_its_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_peer_card_reply_invalidates_the_coordinators_check(self):
        decision = self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        bot = self.write('POST', '/v1/bots', dict(name='Helper', role='Verify'))
        message = self.service._store_bot_message(owner['id'], dict(target_bot_id=bot['id'], text='Verify result', require_response=True))
        owner = self.service._get('tasks', owner['id'])
        owner['status'] = 'needs_you'
        self.service._put('tasks', owner)
        peer = dict(decision, id='peer-card', task_id=message['task_id'], bot_id=bot['id'], title='Use this narrower scope?')
        self.service._put('decisions', peer)
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + peer['id'] + '/reply', dict(text='Keep the original scope.', revision=1))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The peer card replaces this question.'))

    def test_unknown_tool_does_not_strand_completed_background_research(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record.update(turn_id=context['id'], _completion_text='The research result is available.')
        self.service._put('native_receipts', record)
        self.service.native_event(self.task['id'], 'native_call', dict(name='read_file', arguments={'path': 'result'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], context['id'])
        self.assertIn('The research result is available.', retry['_context_recovery'])
        self.assertIn('Reuse their processes', retry['_context_recovery'])

    def test_maintenance_process_is_not_a_task_dependency(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record['turn_id'] = context['id']
        self.service._put('native_receipts', record)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        owner = self.service._get('tasks', self.task['id'])
        owner['status'] = 'working'
        self.service._put('tasks', owner)
        self.assertEqual(self.service.wait_for_task(owner['id'], {})['processes'], [])
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(owner['id'], dict(process_id='test-process', reason='Wait for research'))

    def test_partial_completion_does_not_forget_a_second_research_process(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._resume_process_context(context['id'], [(first, 'The first result is ready.')])
        continuation = self.service._all('contexts')[-1]
        continuation['status'] = 'failed'
        self.service._put('contexts', continuation)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_human_reply_interrupts_the_check_before_changing_the_card(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Change the scope.', revision=1))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('decisions', decision['id'])['revision'], 1)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_result_for_a_cancelled_callback_reaches_the_replacement_check(self):
        decision = self.question()
        context = self.refresh()
        record = self.process_receipt()
        record['turn_id'] = context['id']
        self.service._put('native_receipts', record)
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._resume_process_context(context['id'], [(record, 'The dependency is already installed.')])
        callback = self.service._all('contexts')[-1]
        callback['status'] = 'cancelled'
        self.service._put('contexts', callback)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        retry = self.service._all('contexts')[-1]
        self.assertNotEqual(retry['id'], callback['id'])
        self.assertIn('The dependency is already installed.', retry['_context_recovery'])

    def test_normal_chat_cancels_and_defers_automatic_checks(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Use a different scope.', mode='queue'))
        with patch.object(self.provider, 'cancel_context', create=True) as cancel:
            self.service._cancel_stale_decision_refreshes()
        cancel.assert_called_once_with(self.task['id'], context['id'])
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_cancelled_check_keeps_a_result_for_an_existing_tool_call(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect source'}), context_id=context['id'])
        context['status'] = 'cancelled'
        self.service._put('contexts', context)
        result = json.dumps(dict(session_id='still-running', notify_on_complete=True))
        self.service.native_event(self.task['id'], 'native_result',
            dict(id=receipt['id'], result=result), context_id=context['id'])
        saved = self.service._get('native_receipts', receipt['id'])
        self.assertEqual((saved['state'], saved['result']), ('completed', result))
        with self.assertRaises(ServiceError):
            self.service.native_event(self.task['id'], 'native_call',
                dict(name='terminal', arguments={'command': 'new action'}), context_id=context['id'])
        other = self.process_receipt()
        with self.assertRaises(ServiceError):
            self.service.native_event(self.task['id'], 'native_result',
                dict(id=other['id'], result=other['result']), context_id=context['id'])

    def test_second_research_result_invalidates_a_running_callback(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        self.finish(context)
        self.service._resume_process_context(context['id'], [(first, 'The first result is ready.')])
        callback = self.service._all('contexts')[-1]
        callback['status'] = 'working'
        self.service._put('contexts', callback)
        # The callback starts with the delivered result in its current evidence.
        self.service.refresh_decision(callback['id'], dict(status='current', evidence='The second process still runs.'))
        second['_completion_text'] = 'The second result changes the required decision.'
        self.service._put('native_receipts', second)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(callback['id'], dict(status='withdraw', evidence='Old first result.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_working_human_chat_keeps_priority_until_its_turn_finishes(self):
        decision = self.question()
        context = self.refresh()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Change this scope.', mode='queue'))
        task = self.service._get('tasks', reply['id'])
        task.update(status='working', _turn_preference_source_ids=task['_preference_source_ids'])
        self.service._put('tasks', task)
        # Update the input snapshot to isolate priority from the separate new-message guard.
        owner = self.service._get('tasks', self.task['id'])
        fingerprint, _ = self.service._decision_evidence(decision, {t['id']: t for t in self.service._all('tasks')},
            self.service._all('native_receipts'), self.service._all('artifacts'))
        context.update(_evidence_fingerprint=fingerprint, _human_input_ids=[m['id'] for m in self.service._decision_human_messages(owner)])
        self.service._put('contexts', context)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Premature result.'))
        self.finish(context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        task['status'] = 'completed'
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 2)

    def test_human_reply_research_keeps_priority_after_its_turn_ends(self):
        decision = self.question()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Check the source before changing this.', revision=1))
        human = self.service._all('contexts')[-1]
        human['status'] = 'working'
        self.service._put('contexts', human)
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'inspect source'}), context_id=human['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='human-research', notify_on_complete=True))), context_id=human['id'])
        self.finish(human)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)
        self.provider.process_completions = lambda bot, processes: [dict(id=receipt['id'], text='The source confirms a change.')]
        self.service._drain_process_completions()
        self.assertEqual(len(self.service._all('contexts')), 2)
        self.assertFalse(self.service._all('contexts')[-1].get('_decision_refresh'))

    def test_busy_owner_does_not_block_another_cards_refresh(self):
        import threading
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        task.update(status='working', _turn_preference_source_ids=[])
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        bot = self.write('POST', '/v1/bots', dict(name='Other bot', role='Independent work'))
        other = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Other work', mode='queue'))
        other = self.service._get('tasks', other['id'])
        other['status'] = 'needs_you'
        self.service._put('tasks', other)
        peer = dict(decision, id='other-card', bot_id=bot['id'], task_id=other['id'])
        self.service._put('decisions', peer)
        self.service.running[task['id']] = threading.current_thread()
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts')[-1]['decision_id'], peer['id'])
        self.service.running[other['id']] = threading.current_thread()
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._all('contexts')[-1]['status'], 'cancelled')

    def test_answered_required_card_preempts_an_optional_card_check(self):
        decision = self.question()
        proposal = dict(decision, id='optional-card', inbox_kind='proposal')
        self.service._put('decisions', proposal)
        decision['_freshness']['queued_at'] = datetime.now(timezone.utc).isoformat()
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.assertEqual(context['decision_id'], proposal['id'])
        context['status'] = 'working'
        self.service._put('contexts', context)
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')

    def test_waiting_human_chat_research_defers_card_checks(self):
        self.question()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Inspect the source first.', mode='queue'))
        task = self.service._get('tasks', reply['id'])
        task.update(status='working', _turn_preference_source_ids=task['_preference_source_ids'])
        self.service._put('tasks', task)
        receipt = self.service.native_event(task['id'], 'native_call', dict(name='terminal', arguments={'command': 'inspect source'}))
        self.service.native_event(task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='human-research', notify_on_complete=True))))
        task.update(status='waiting', _preference_source_ids=[], _turn_preference_source_ids=[])
        self.service._put('tasks', task)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_retry_keeps_original_research_after_recording_a_result(self):
        decision = self.question()
        context = self.refresh()
        process = self.process_receipt()
        process['turn_id'] = context['id']
        self.service._put('native_receipts', process)
        self.service.native_event(self.task['id'], 'native_call', dict(name='read_file', arguments={'path': 'source'}), context_id=context['id'])
        context['status'] = 'failed'
        self.service._put('contexts', context)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        retry = self.refresh()
        self.service.refresh_decision(retry['id'], dict(status='current', evidence='The required decision remains unchanged.'))
        self.finish(retry)
        process['_completion_text'] = 'The original research found a changed source.'
        self.service._put('native_receipts', process)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = '2000-01-01T00:00:00Z'
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        replacement = self.service._all('contexts')[-1]
        self.assertNotEqual(replacement['id'], retry['id'])
        self.assertIn('The original research found a changed source.', replacement['_context_recovery'])

    def test_approved_continuation_in_another_session_preempts_the_check(self):
        decision = self.question()
        context = self.refresh()
        owner = self.service._get('tasks', self.task['id'])
        continuation = dict(owner, id='approved-proposal', status='queued', _resume_decision=decision['id'],
                            _execution_session_id='independent-session', _preference_source_ids=[], _turn_preference_source_ids=[])
        self.service._put('tasks', continuation)
        self.service._cancel_stale_decision_refreshes()
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'cancelled')

    def test_unrelated_native_guarded_callback_does_not_starve_refresh(self):
        decision = self.question()
        other_bot = self.write('POST', '/v1/bots', dict(name='Care', role='Handle callbacks'))
        callback = self.write('POST', '/v1/bots/' + other_bot['id'] + '/messages', dict(text='Unrelated callback', mode='queue'))
        callback = self.service._get('tasks', callback['id'])
        callback['_background_completion'] = ['Done']
        self.service._put('tasks', callback)
        self.service.kanban = SimpleNamespace(tasks={callback['id']: dict(status='ready', dispatch_blocker='active_pr')}, close=lambda: None)
        self.service._queue_decision_refreshes()
        contexts = self.service._all('contexts')
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]['decision_id'], decision['id'])

    def test_related_genuinely_runnable_callback_defers_refresh(self):
        self.question()
        owner = self.service._get('tasks', self.task['id'])
        callback = dict(owner, id='related-callback', status='queued',
                        _background_completion=['Done'], _preference_source_ids=[], _turn_preference_source_ids=[])
        self.service._put('tasks', callback)
        self.service.kanban = SimpleNamespace(tasks={callback['id']: dict(status='ready', dispatch_blocker=None)}, close=lambda: None)
        self.service._queue_decision_refreshes()
        self.assertEqual(self.service._all('contexts'), [])

    def test_ready_human_reply_context_owns_work_while_its_task_needs_you(self):
        decision = self.question()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply',
                   dict(text='Inspect the current source before changing this.', revision=1))
        reply = self.service._all('contexts')[-1]
        owner = self.service._get('tasks', self.task['id'])
        self.assertEqual(owner['status'], 'needs_you')

        # Use the native projection built by the service, including its independent
        # context identity, instead of inventing task-shaped state for the reply.
        self.service.kanban = SimpleNamespace(tasks={}, close=lambda: None)
        rows = {row['id']: row for row in self.service._kanban_rows()}
        self.assertEqual(rows[reply['id']]['state'], 'ready')
        self.assertEqual(rows[owner['id']]['state'], 'review')
        self.service.kanban.tasks = {
            identity: dict(id=identity, status=row['state'], dispatch_blocker=None)
            for identity, row in rows.items()
        }

        self.assertTrue(self.service._refresh_has_queued_owner(owner))

    def test_second_completion_carries_an_unknown_effect_from_its_sibling(self):
        decision = self.question()
        context = self.refresh()
        first = self.process_receipt()
        first['turn_id'] = context['id']
        self.service._put('native_receipts', first)
        second = dict(first, id='second-research', result=json.dumps(dict(session_id='second-process', notify_on_complete=True)))
        self.service._put('native_receipts', second)
        self.finish(context)
        self.service._resume_process_context(context['id'], [(first, 'First research result.')])
        sibling = self.service._all('contexts')[-1]
        sibling['status'] = 'working'
        self.service._put('contexts', sibling)
        unknown = self.service.native_event(self.task['id'], 'native_call', dict(name='terminal', arguments={'command': 'inspect result'}), context_id=sibling['id'])
        sibling['status'] = 'failed'
        self.service._put('contexts', sibling)
        self.service._resume_process_context(context['id'], [(second, 'Second research result.')])
        callback = self.service._all('contexts')[-1]
        self.assertIn(unknown['id'], callback['_context_recovery'])
        callback['status'] = 'working'
        self.service._put('contexts', callback)
        self.service.refresh_decision(callback['id'], dict(status='current', evidence='The question still applies.'))
        self.finish(callback)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness'].update(checked_at='2000-01-01T00:00:00Z', queued_at='2000-01-01T00:00:00Z')
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertIn(unknown['id'], self.service._all('contexts')[-1]['_context_recovery'])

    def test_dispatch_runs_only_one_automatic_callback_at_a_time(self):
        import threading
        decision = self.question()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        bot = self.write('POST', '/v1/bots', dict(name='Other bot', role='Independent work'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Other work', mode='queue'))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'needs_you'
        self.service._put('tasks', task)
        peer = dict(decision, id='peer-card', bot_id=bot['id'], task_id=task['id'])
        self.service._put('decisions', peer)
        callback = dict(context, id='peer-callback', bot_id=bot['id'], task_id=task['id'], decision_id=peer['id'],
                        _conversation_epoch=task.get('_conversation_epoch'),
                        _human_input_ids=[m['id'] for m in self.service._decision_human_messages(task)])
        self.service._put('contexts', callback)
        started = []
        def start(identity):
            started.append(identity)
            self.service.context_threads[identity] = threading.current_thread()
        try:
            with patch.object(self.service, '_start_context', start):
                self.service._dispatch_pending()
            self.assertEqual(len(started), 1)
        finally:
            self.service.context_threads.clear()

    def test_archived_reply_does_not_block_another_bots_card_check(self):
        decision = self.question()
        bot = self.write('POST', '/v1/bots', dict(name='Archived bot', role='Old work'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Old request', mode='queue'))
        task = self.service._get('tasks', task['id'])
        task['status'] = 'needs_you'
        self.service._put('tasks', task)
        peer = dict(decision, id='old-card', bot_id=bot['id'], task_id=task['id'])
        self.service._put('decisions', peer)
        self.write('POST', '/v1/decisions/' + peer['id'] + '/reply', dict(text='Check this.', revision=1))
        bot = self.service._get('bots', bot['id'])
        bot['archived'] = True
        self.service._put('bots', bot)
        context = self.refresh()
        self.assertTrue(context.get('_decision_refresh'))
        self.assertEqual(context['decision_id'], decision['id'])

    def test_evidence_change_before_the_first_scan_triggers_a_check(self):
        self.question(old=False)
        owner = self.service._get('tasks', self.task['id'])
        owner['summary'] = 'The source changed after the card was created.'
        self.service._put('tasks', owner)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_same_source_id_does_not_expose_an_unrelated_bots_work(self):
        self.question()
        owner = self.service._get('tasks', self.task['id'])
        owner['_source_assignment'] = {'id': 'issue-2'}
        self.service._put('tasks', owner)
        bot = self.write('POST', '/v1/bots', dict(name='Private', role='Private work'))
        other = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Private task', mode='queue'))
        other = self.service._get('tasks', other['id'])
        other.update(status='working', _source_assignment={'id': 'issue-2'})
        self.service._put('tasks', other)
        self.service._register_work(other['id'], dict(key='private-work', title='Private record for another source',
            activity='Private evidence', status='working'))
        context = self.refresh()
        self.assertNotIn('Private record for another source', context['question'])
        self.assertNotIn('Private evidence', context['question'])

    def test_changed_peer_question_invalidates_duplicate_withdrawal(self):
        decision = self.question()
        peer = dict(decision, id='peer', title='Use the local database?')
        self.service._put('decisions', peer)
        context = self.refresh()
        peer.update(revision=2, context='Only inspect the schema. Do not change it.')
        self.service._put('decisions', peer)
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The other card covers this.'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_maintenance_process_does_not_hold_an_answered_assignment(self):
        decision = self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='terminal', arguments={'command': 'read evidence'}), context_id=context['id'])
        self.service.native_event(self.task['id'], 'native_result', dict(id=receipt['id'],
            result=json.dumps(dict(session_id='maintenance-process', notify_on_complete=True))), context_id=context['id'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        self.assertFalse(self.service._background_pending(self.task['id']))
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'completed')

    def test_interrupted_maintenance_stays_silent_and_keeps_its_receipt(self):
        self.question()
        context = self.refresh()
        receipt = self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'evidence'}), context_id=context['id'])
        before = self.service._all('messages')
        self.assertTrue(self.service._block_uncertain_context(context))
        self.assertEqual(self.service._all('messages'), before)
        self.assertEqual(self.service._get('native_receipts', receipt['id'])['state'], 'executing')

    def test_automatic_work_maintenance_does_not_invalidate_its_card_check(self):
        self.question()
        self.service._sync_automatic_work()
        context = self.refresh()
        self.service.native_event(self.task['id'], 'native_call',
            dict(name='read_file', arguments={'path': 'evidence'}), context_id=context['id'])
        self.service._sync_automatic_work()
        rows = [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']
        self.assertEqual(rows[0]['work_state'], 'needs_decision')
        result = self.service.refresh_decision(context['id'], dict(status='current', evidence='The question still applies.'))
        self.assertEqual(result['status'], 'recorded')

    def test_instruction_addressed_from_another_chat_invalidates_the_check(self):
        decision = self.question()
        context = self.refresh()
        other = self.write('POST', '/v1/bots', dict(name='Coordinator', role='Coordinate'))
        self.write('POST', '/v1/bots/' + other['id'] + '/messages',
            dict(text='Do not install dependencies.', recipient_bot_ids=[self.bot['id']], mode='queue'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_next_check_includes_new_human_instructions(self):
        self.question()
        reply = self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Do not install dependencies.', mode='auto'))
        task = self.service._get('tasks', reply['id'])
        task['status'] = 'completed'
        self.service._put('tasks', task)
        context = self.refresh()
        self.assertIn('Do not install dependencies.', context['question'])

    def test_withdrawn_recovery_preserves_unknown_effects_in_a_new_turn(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        task['_self_recovery'] = dict(attempt=2, unknown_tools=[])
        previous_turn = task['_native_turn_id']
        self.service._put('tasks', task)
        self.service._put('native_receipts', dict(id='unknown', task_id=task['id'], turn_id=previous_turn,
            action={'name': 'terminal'}, state='executing', created_at=task['created_at']))
        decision['_recovery_work_ids'] = ['work']
        self.service._put('decisions', decision)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='The result can be verified within the existing request.'))
        saved = self.service._get('tasks', task['id'])
        self.assertNotEqual(saved['_native_turn_id'], previous_turn)
        self.assertFalse(self.service._has_uncertain_receipt(task['id'], saved['_native_turn_id']))
        self.assertEqual(saved['_self_recovery']['unknown_tools'][0]['id'], 'unknown')
        self.assertEqual(self.service._get('native_receipts', 'unknown')['state'], 'executing')

    def test_new_chat_instruction_invalidates_an_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/bots/' + self.bot['id'] + '/messages', dict(text='Do not install the dependencies.', mode='auto'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_automatic_check_does_not_disable_human_replies(self):
        decision = self.question()
        self.refresh()
        shown = next(d for d in self.service.snapshot()['decisions'] if d['id'] == decision['id'])
        self.assertFalse(shown['reply_pending'])
        self.assertFalse(self.service.card(decision['id'])['decision']['reply_pending'])
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Keep the old setup.', revision=1))
        self.assertTrue(self.service.card(decision['id'])['decision']['reply_pending'])

    def test_human_reply_invalidates_an_automatic_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Use the existing local services.', revision=1))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Old result'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision']), ('pending', 1))

    def test_human_answer_wins_over_late_check(self):
        decision = self.question()
        context = self.refresh()
        self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(revision=1, option_id='install'))
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='No longer required'))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'approved')

    def test_check_cannot_approve_and_cannot_refresh_native_tool_approval(self):
        decision = self.question()
        context = self.refresh()
        with self.assertRaises(ServiceError):
            self.service.refresh_decision(context['id'], dict(status='approve', evidence='This looks safe'))
        decision['action_type'] = 'native_approval'
        self.service._put('decisions', decision)
        self.finish(context)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_automatic_failure_obeys_backoff_without_chat_spam(self):
        self.question()
        context = self.refresh()
        context['status'] = 'failed'
        self.service._put('contexts', context)
        self.service._queue_context_recoveries()
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_refresh_prompt_excludes_unrelated_private_cards(self):
        self.question()
        bot = self.write('POST', '/v1/bots', dict(name='Private bot', role='Private'))
        task = self.write('POST', '/v1/bots/' + bot['id'] + '/messages', dict(text='Private request', mode='queue'))
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        try:
            self.service.native_event(task['id'], 'native_question', dict(title='Private account secret',
                options=[dict(id='first', label='First'), dict(id='second', label='Second')]))
        except YieldForDecision:
            pass
        context = self.refresh()
        self.assertNotIn('Private account secret', context['question'])

    def test_optional_proposal_does_not_block_required_question_withdrawal(self):
        decision = self.question()
        proposal = dict(decision, id='optional-proposal', inbox_kind='proposal', title='Optional next step')
        self.service._put('decisions', proposal)
        context = self.refresh()
        self.service.refresh_decision(context['id'], dict(status='withdraw', evidence='Local setup is already covered.'))
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'queued')
        self.assertEqual(self.service._get('decisions', proposal['id'])['status'], 'pending')

    def test_completed_routine_card_uses_an_independent_context(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        job = dict(id='finished-routine', fire_claim={'by': 'old-owner'})
        task.update(status='completed', _native_job=job, _native_job_finished=True)
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        def answer(task, bot, history, question, emit):
            if task.get('_native_job'):
                emit('failed', {'summary': 'This routine no longer owns its native execution claim.'})
                return
            emit('decision_response', dict(status='current', evidence='The pending question still applies.'))
        self.provider.answer_context = answer
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertTrue(self.service._get('contexts', context['id'])['_refresh_recorded'])
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_job'], job)

    def test_human_reply_to_a_completed_routine_uses_its_own_context(self):
        decision = self.question()
        task = self.service._get('tasks', self.task['id'])
        job = dict(id='finished-routine', fire_claim={'by': 'old-owner'})
        task.update(status='completed', _native_job=job, _native_job_finished=True)
        self.service._put('tasks', task)
        decision['inbox_kind'] = 'proposal'
        self.service._put('decisions', decision)
        def answer(task, bot, history, question, emit):
            if task.get('_native_job'):
                emit('failed', {'summary': 'This routine no longer owns its native execution claim.'})
                return
            receipt = emit('native_call', dict(name='read_file', arguments={'path': 'saved-result'}))
            emit('native_result', dict(id=receipt['id'], result='Verified result'))
            emit('completed', {'summary': 'The saved result confirms the stated risk.'})
        self.provider.answer_context = answer
        self.write('POST', '/v1/decisions/' + decision['id'] + '/reply', dict(text='Explain the risk.', revision=1))
        context = self.service._all('contexts')[-1]
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['_native_job'], job)

    def test_worker_ready_event_does_not_fail_refresh_or_repeat_it(self):
        decision = self.question()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        messages = self.service._all('messages')

        def answer(task, bot, history, question, emit):
            emit('steering_ready', {'turn_id': None})
            emit('decision_response', dict(status='current', evidence='Verified the pending choice still applies.'))

        self.provider.answer_context = answer
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        saved = self.service._get('contexts', context['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertTrue(saved['_refresh_recorded'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'needs_you')
        self.assertEqual(self.service._all('messages'), messages)
        decision = self.service._get('decisions', decision['id'])
        decision['_freshness']['queued_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.service._put('decisions', decision)
        self.service._queue_decision_refreshes()
        self.assertEqual(len(self.service._all('contexts')), 1)

    def test_real_context_worker_records_refresh_and_stays_silent(self):
        decision = self.question()
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        messages = self.service._all('messages')
        def answer(task, bot, history, question, emit):
            self.assertTrue(task['_decision_refresh'])
            self.assertNotIn('Do not change the pending proposal', conversation_input(dict(
                operation='context', task=task, history=history, question=question))[0])
            emit('decision_response', dict(status='current', evidence='Verified source; the account choice remains open.'))
            emit('completed', dict(summary='[SILENT]'))
        self.provider.answer_context = answer
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.assertEqual(self.service._get('contexts', context['id'])['status'], 'completed')
        self.assertIn('checked_at', self.service._get('decisions', decision['id'])['_freshness'])
        self.assertEqual(self.service._all('messages'), messages)


import test_provider as provider_fixtures


class RefreshCancellationTests(unittest.TestCase):
    setUp = provider_fixtures.ProviderTests.setUp
    tearDown = provider_fixtures.ProviderTests.tearDown
    fake_hermes = provider_fixtures.ProviderTests.fake_hermes
    wait_for_path = provider_fixtures.ProviderTests.wait_for_path

    def test_native_context_cancellation_preserves_the_task_and_runtime(self):
        import os
        import threading
        started = self.root / 'started'
        provider = self.fake_hermes(
            "from pathlib import Path\nimport time\n"
            "if 'pendingcheck' in prompt:\n"
            f"    Path({str(started)!r}).touch()\n"
            "    while not getattr(self, 'interrupted', False): time.sleep(0.01)\n"
            "return {'completed': True, 'final_response': 'Done.'}\n")
        source = provider.hermes_root / 'run_agent.py'
        source.write_text(source.read_text().replace('class AIAgent:', 'class AIAgent:\n    def hard_interrupt(self, reason): self.interrupted = True'))
        context = dict(self.task, _context_id='freshness-check')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            thread = threading.Thread(target=lambda: provider.answer_context(context, self.bot, [], 'pendingcheck', lambda *_: None))
            thread.start()
            try:
                self.wait_for_path(started)
                runtime = provider._runtimes[self.bot['id']]
                provider.cancel_context(self.task['id'], context['_context_id'])
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(runtime.poll())
                events = []
                provider.start_task(self.task, self.bot, [], lambda kind, data: events.append(kind))
                self.assertIn('completed', events)
            finally:
                if thread.is_alive():
                    provider.cancel_task(self.task['id'])
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
