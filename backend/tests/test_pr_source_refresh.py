"""Only cards with completed source reads may take the global refresh slot."""
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_service_pr as fixture


class PRSourceRefreshTests(unittest.TestCase):
    setUp = fixture.PRServiceTests.setUp
    tearDown = fixture.PRServiceTests.tearDown
    write = fixture.PRServiceTests.write
    run_task = fixture.PRServiceTests.run_task

    def cards(self, *, unchanged=False, shared=False):
        first = self.run_task()['artifacts'][0]
        second_url = fixture.URL.replace('/42', '/43')
        with self.service.transaction():
            task = self.service._get('tasks', self.task['id'])
            task['status'] = 'needs_you'
            self.service._put('tasks', task)
            self.service._put('tasks', dict(task, id='newer-task'))
            self.service._put('artifacts', dict(first, id='newer-pr', task_id='newer-task', url=second_url))
            for identity, task_id, url, date in (
                    ('older-card', task['id'], fixture.URL, '2000-01-01T00:00:00Z'),
                    ('newer-card', 'newer-task', second_url, '2000-01-02T00:00:00Z')):
                self.service._put('decisions', dict(id=identity, bot_id=self.bot['id'], task_id=task_id,
                    status='pending', revision=1, created_at=date, action_type='question', _native=True,
                    sources=[dict(kind='pull_request', title='PR', url=link) for link in
                             ([url, second_url] if shared and identity == 'older-card' else [url])]))
        # The unchanged variant already has the incoming source facts, but its
        # artifact still changes during the poll and requires a freshness check.
        def baseline(url):
            sha = 'b' * 40 if unchanged and url == second_url else fixture.SHA
            return replace(fixture.observation('ready', sha=sha), url=url)
        self.tracker.observe = baseline
        self.tracker.observe_discussion = lambda url: {'fingerprint': 'current'}
        self.service._refresh_decision_sources({})
        for card in self.service._all('decisions'):
            self.service._initialize_decision_freshness(card)
        self.tracker.observe = lambda url: replace(fixture.observation('ready', sha='b' * 40), url=url)
        return second_url

    def check_ready_card(self, *, unchanged=False, dispatcher=False, shared=False):
        second_url = self.cards(unchanged=unchanged, shared=shared)
        before = self.service._get('decisions', 'newer-card')['_source_facts']
        older_blocked, newer_blocked = threading.Event(), threading.Event()
        release_older, release_newer, scheduled = threading.Event(), threading.Event(), threading.Event()
        errors, reads = [], []

        def discussion(url):
            reads.append(url)
            blocked, release = (older_blocked, release_older) if url == fixture.URL else (newer_blocked, release_newer)
            blocked.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release discussion read')
            return {'fingerprint': 'current'}

        self.tracker.observe_discussion = discussion
        queue = self.service._queue_decision_refreshes

        def queue_and_signal():
            queue()
            scheduled.set()

        def refresh():
            try:
                self.service.refresh_pull_requests()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=refresh)
        with patch.object(self.service, '_queue_decision_refreshes', side_effect=queue_and_signal):
            worker.start()
            try:
                self.assertTrue(older_blocked.wait(2))
                self.assertTrue(newer_blocked.wait(2))
                # Both PR artifacts are now updated; neither discussion is ready.
                if dispatcher:
                    queue()  # The same global entry point used by _dispatch.
                    self.assertEqual(self.service._all('contexts'), [])
                release_newer.set()
                self.assertTrue(scheduled.wait(2), 'Even unchanged source completion must schedule eligible cards')
                with self.service.lock:
                    contexts = self.service._all('contexts')
                    self.assertEqual([c['decision_id'] for c in contexts], ['newer-card'])
                    newer = self.service._get('decisions', 'newer-card')
                    self.assertEqual(newer['_source_facts'][second_url]['head_sha'], 'b' * 40)
                    if unchanged:
                        self.assertEqual(newer['_source_facts'], before)
                    self.assertNotIn('queued_at', self.service._get('decisions', 'older-card')['_freshness'])
                self.assertTrue(worker.is_alive(), 'The older discussion must still be blocked')
            finally:
                release_newer.set()
                release_older.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.service._pending_decision_sources, set())
        self.assertCountEqual(reads, [fixture.URL, second_url])
        if shared:
            older = self.service._get('decisions', 'older-card')
            self.assertEqual(set(older['_source_facts']), {fixture.URL, second_url})
            self.assertTrue(all(facts['head_sha'] == 'b' * 40 for facts in older['_source_facts'].values()))
        context = self.service._all('contexts')[0]
        tasks = {t['id']: t for t in self.service._all('tasks')}
        artifacts = self.service._all('artifacts')
        newer = self.service._get('decisions', 'newer-card')
        fingerprint, _ = self.service._decision_evidence(newer, tasks,
            self.service._decision_records(newer, tasks, artifacts), artifacts)
        self.assertEqual(context['_evidence_fingerprint'], fingerprint)
        context['status'] = 'completed'
        self.service._put('contexts', context)
        queue()
        self.assertEqual([c['decision_id'] for c in self.service._all('contexts')
                          if c['status'] == 'queued'], ['older-card'])

    def test_completed_newer_card_takes_slot_while_older_discussion_waits(self):
        self.check_ready_card()

    def test_dispatcher_skips_cards_with_pending_source_reads(self):
        self.check_ready_card(dispatcher=True)

    def test_unchanged_source_completion_still_readies_newer_card(self):
        self.check_ready_card(unchanged=True)

    def test_shared_source_read_readies_only_card_with_all_sources_complete(self):
        self.check_ready_card(shared=True, dispatcher=True)

    def test_worker_failure_cancels_other_active_read_before_releasing_fence(self):
        self.cards()
        before = self.service._get('decisions', 'older-card')['_source_facts']
        blocked, release, returned = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.service._refresh_decision_sources

        def source(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                if kwargs['decision_ids'] == {'older-card'}:
                    returned.set()

        def discussion(url):
            if url == fixture.URL:
                blocked.set()
                if not release.wait(5):
                    raise TimeoutError('Test did not release active read')
                return {'fingerprint': 'late failed-poll result'}
            if not blocked.wait(2):
                raise TimeoutError('Other source worker never started')
            raise RuntimeError('one source failed')

        def refresh():
            try:
                self.service.refresh_pull_requests()
            except Exception as error:
                errors.append(error)

        self.tracker.observe_discussion = discussion
        worker = threading.Thread(target=refresh)
        with patch.object(self.service, '_refresh_decision_sources', side_effect=source):
            worker.start()
            try:
                self.assertTrue(blocked.wait(2))
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'Failure must cancel without waiting for active HTTP reads')
                self.assertEqual([str(error) for error in errors], ['one source failed'])
                self.assertEqual(self.service._pending_decision_sources, set())
                self.service._queue_decision_refreshes()  # Periodic admission after failure.
                context = self.service._all('contexts')[0]
                self.assertEqual(context['decision_id'], 'older-card')
                release.set()
                self.assertTrue(returned.wait(2))
                self.assertEqual(self.service._get('decisions', 'older-card')['_source_facts'], before,
                                 'A cancelled poll must not write after releasing its fence')
                self.service._queue_decision_refreshes()
                self.assertEqual(self.service._get('contexts', context['id'])['status'], 'queued')
            finally:
                release.set()
                worker.join(5)
                returned.wait(2)
        self.tracker.observe_discussion = lambda url: {'fingerprint': 'next poll'}
        self.service.refresh_pull_requests()
        self.assertEqual(self.service._pending_decision_sources, set())
        for card in self.service._all('decisions'):
            self.assertTrue(all(facts['discussion']['fingerprint'] == 'next poll'
                                for facts in card['_source_facts'].values()))

    def check_revised_read(self, *, change_links=False, after_write=False):
        second_url = self.cards()
        new_url = fixture.URL.replace('/42', '/44')
        older_blocked, newer_blocked, retry_blocked = (threading.Event() for _ in range(3))
        release_older, release_newer, release_retry = (threading.Event() for _ in range(3))
        scheduled, admission_checked = threading.Event(), threading.Event()
        errors, calls = [], []
        original = self.service._refresh_decision_sources
        queue = self.service._queue_decision_refreshes

        def revise():
            with self.service.transaction():
                card = self.service._get('decisions', 'older-card')
                if change_links:
                    card['sources'][0]['url'] = new_url
                else:
                    card['revision'] += 1
                self.service._put('decisions', card)

        def source(*args, **kwargs):
            older = kwargs['decision_ids'] == {'older-card'}
            if older:
                calls.append('older')
                if len(calls) == 2:
                    retry_blocked.set()
                    if not release_retry.wait(5):
                        raise TimeoutError('Test did not release revised read')
            result = original(*args, **kwargs)
            if older and len(calls) == 1 and after_write:
                revise()  # The read committed, but its completion has not been consumed.
            return result

        def discussion(url):
            if url in (fixture.URL, second_url):
                blocked, release = ((older_blocked, release_older) if url == fixture.URL
                                    else (newer_blocked, release_newer))
                blocked.set()
                if not release.wait(5):
                    raise TimeoutError('Test did not release discussion read')
            return {'fingerprint': url}

        def queue_and_signal():
            queue()
            admission_checked.set()
            if self.service._all('contexts'):
                scheduled.set()

        def refresh():
            try:
                self.service.refresh_pull_requests()
            except Exception as error:
                errors.append(error)

        self.tracker.observe_discussion = discussion
        worker = threading.Thread(target=refresh)
        with patch.object(self.service, '_refresh_decision_sources', side_effect=source), \
                patch.object(self.service, '_queue_decision_refreshes', side_effect=queue_and_signal):
            worker.start()
            try:
                self.assertTrue(older_blocked.wait(2))
                self.assertTrue(newer_blocked.wait(2))
                if not after_write:
                    revise()
                release_older.set()
                self.assertTrue(admission_checked.wait(2))
                queue()  # A periodic tick must also respect the retained fence.
                self.assertEqual(self.service._all('contexts'), [])
                self.assertTrue(retry_blocked.wait(2), 'A discarded completion must retry the current card')
                self.assertIn('older-card', self.service._pending_decision_sources)
                release_newer.set()
                self.assertTrue(scheduled.wait(2))
                self.assertEqual([c['decision_id'] for c in self.service._all('contexts')], ['newer-card'])
            finally:
                release_older.set()
                release_newer.set()
                release_retry.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, ['older', 'older'])
        self.assertEqual(self.service._pending_decision_sources, set())
        older = self.service._get('decisions', 'older-card')
        expected_url = new_url if change_links else fixture.URL
        self.assertEqual(set(older['_source_facts']), {expected_url})
        self.assertEqual(older['_source_facts'][expected_url]['discussion']['fingerprint'], expected_url)
        context = self.service._all('contexts')[0]
        context['status'] = 'completed'
        self.service._put('contexts', context)
        queue()
        queued = [c for c in self.service._all('contexts') if c['status'] == 'queued']
        self.assertEqual([c['decision_id'] for c in queued], ['older-card'])
        self.assertEqual(queued[0]['_decision_revision'], older['revision'])

    def test_revision_during_read_retries_before_admission(self):
        self.check_revised_read()

    def test_source_change_during_read_retries_before_admission(self):
        self.check_revised_read(change_links=True)

    def test_revision_after_write_retries_before_admission(self):
        self.check_revised_read(after_write=True)

    def test_source_change_after_write_retries_before_admission(self):
        self.check_revised_read(change_links=True, after_write=True)

    def check_midpoll_card(self, change, *, admission=False, cleanup=None):
        second_url = self.cards()
        new_url = fixture.URL.replace('/42', '/44')
        # The first card's initial read changes no evidence and needs no refresh.
        self.tracker.observe = lambda url: replace(fixture.observation(), url=url)
        self.service._refresh_decision_sources({})
        for card in self.service._all('decisions'):
            self.service._initialize_decision_freshness(card)
        creating = change in ('create', 'empty')
        target = 'late-card' if creating else 'older-card'
        template = self.service._get('decisions', 'older-card')
        if creating:
            with self.service.transaction():
                for card in self.service._all('decisions'):
                    if change == 'empty' or card['id'] == 'older-card':
                        card['status'] = 'resolved'
                        self.service._put('decisions', card)
        anchor_blocked, release_anchor = threading.Event(), threading.Event()
        completed, changed, target_blocked = (threading.Event() for _ in range(3))
        release_target, target_returned, queued = (threading.Event() for _ in range(3))
        errors, source_errors, calls, discussions = [], [], [], []
        original = self.service._refresh_decision_sources
        queue = self.service._queue_decision_refreshes

        def observe(url):
            if url == second_url:
                anchor_blocked.set()
                if not release_anchor.wait(5):
                    raise TimeoutError('Test did not release unrelated PR observation')
                if cleanup == 'failure':
                    raise RuntimeError('unrelated PR failed')
            return replace(fixture.observation(), url=url)

        def discussion(url):
            discussions.append(url)
            return {'fingerprint': 'current'}

        def source(*args, **kwargs):
            is_target = kwargs['decision_ids'] == {target}
            if is_target:
                calls.append(target)
            late = is_target and changed.is_set()
            try:
                if late:
                    target_blocked.set()
                    if not release_target.wait(5):
                        raise TimeoutError('Test did not release new or revised card read')
                return original(*args, **kwargs)
            except Exception as error:
                source_errors.append(error)
                raise
            finally:
                if late:
                    target_returned.set()

        def queue_and_signal():
            queue()
            if target not in self.service._pending_decision_sources:
                completed.set()  # Completion has been consumed and its fence released.
            if any(c['decision_id'] == target and c['status'] == 'queued'
                   for c in self.service._all('contexts')):
                queued.set()

        def refresh():
            try:
                self.service.refresh_pull_requests()
            except Exception as error:
                errors.append(error)

        self.tracker.observe = observe
        self.tracker.observe_discussion = discussion
        worker = threading.Thread(target=refresh)
        with patch.object(self.service, '_refresh_decision_sources', side_effect=source), \
                patch.object(self.service, '_queue_decision_refreshes', side_effect=queue_and_signal):
            worker.start()
            try:
                self.assertTrue(anchor_blocked.wait(2))
                if not creating:
                    self.assertTrue(completed.wait(2))
                    self.assertEqual(self.service._all('contexts'), [])
                    self.assertNotIn(target, self.service._pending_decision_sources)
                with self.service.lock:
                    with self.service.transaction():
                        card = dict(template, id=target, status='pending') if creating else self.service._get('decisions', target)
                        if creating:
                            card.pop('_source_facts', None)
                            card.pop('_freshness', None)
                        elif change == 'revision':
                            card['revision'] += 1
                        else:
                            card['sources'][0]['url'] = new_url  # Same revision, different links.
                        self.service._put('decisions', card)
                        artifact = next(a for a in self.service._all('artifacts') if a['url'] == fixture.URL)
                        artifact['head_sha'] = 'c' * 40  # A later artifact change makes the card eligible.
                        self.service._put('artifacts', artifact)
                    changed.set()
                    if admission:
                        queue()  # Periodic tick before the poll can rediscover this identity.
                        self.assertEqual(self.service._all('contexts'), [],
                                         'Global admission must reject missing or stale cycle readiness')
                self.assertTrue(target_blocked.wait(2),
                                'New or revised cards must join the active poll while unrelated reads wait')
                self.assertTrue(worker.is_alive())
                queue()
                self.assertEqual(self.service._all('contexts'), [])
                if cleanup:
                    before = self.service._get('decisions', target).get('_source_facts')
                    if cleanup == 'shutdown':
                        self.service.stopping.set()
                    else:
                        release_anchor.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive(), 'Cleanup must not wait for the late card read')
                    self.assertEqual(self.service._pending_decision_sources, set())
                    self.assertIsNone(self.service._ready_decision_sources)
                    with patch.object(self.service, 'transaction', side_effect=AssertionError('late database access')):
                        release_target.set()
                        self.assertTrue(target_returned.wait(2))
                    self.assertEqual(self.service._get('decisions', target).get('_source_facts'), before)
                else:
                    release_target.set()
                    self.assertTrue(queued.wait(2), 'The current card must progress in this same poll')
                    self.assertFalse(release_anchor.is_set())
                    self.assertTrue(worker.is_alive())
                    current = self.service._get('decisions', target)
                    expected_url = new_url if change == 'links' else fixture.URL
                    self.assertEqual(set(current['_source_facts']), {expected_url})
                    self.assertEqual(current['_source_facts'][expected_url]['discussion'], {'fingerprint': 'current'})
                    context, = self.service._all('contexts')
                    self.assertEqual(context['_decision_revision'], current['revision'])
                    tasks = {t['id']: t for t in self.service._all('tasks')}
                    artifacts = self.service._all('artifacts')
                    fingerprint, _ = self.service._decision_evidence(current, tasks,
                        self.service._decision_records(current, tasks, artifacts), artifacts)
                    self.assertEqual(context['_evidence_fingerprint'], fingerprint)
            finally:
                release_target.set()
                release_anchor.set()
                worker.join(5)
                if target_blocked.is_set():
                    self.assertTrue(target_returned.wait(2))
        self.assertFalse(worker.is_alive())
        self.assertEqual([str(error) for error in errors], ['unrelated PR failed'] if cleanup == 'failure' else [])
        self.assertEqual(source_errors, [])
        self.assertEqual(calls, [target] * (1 if creating else 2), 'Stable identities must not spin or duplicate jobs')
        self.assertEqual(len(discussions), len(set(discussions)), 'Retries must reuse the poll source cache')
        self.assertEqual(self.service._pending_decision_sources, set())
        self.assertIsNone(self.service._ready_decision_sources)

    def test_midpoll_creation_is_fenced_at_global_admission(self):
        self.check_midpoll_card('create', admission=True)

    def test_midpoll_creation_joins_active_poll(self):
        self.check_midpoll_card('create')

    def test_revision_after_completed_read_is_fenced_at_global_admission(self):
        self.check_midpoll_card('revision', admission=True)

    def test_revision_after_completed_read_rejoins_active_poll(self):
        self.check_midpoll_card('revision')

    def test_links_after_completed_read_are_fenced_at_global_admission(self):
        self.check_midpoll_card('links', admission=True)

    def test_links_after_completed_read_rejoin_active_poll(self):
        self.check_midpoll_card('links')

    def test_empty_snapshot_later_card_is_fenced_at_global_admission(self):
        self.check_midpoll_card('empty', admission=True)

    def test_empty_snapshot_later_card_joins_active_poll(self):
        self.check_midpoll_card('empty')

    def test_midpoll_creation_cleanup_on_failure(self):
        self.check_midpoll_card('create', cleanup='failure')

    def test_midpoll_creation_cleanup_on_shutdown(self):
        self.check_midpoll_card('create', cleanup='shutdown')

    def test_pending_sources_cleared_after_read_or_worker_start_failure(self):
        self.cards()
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('thread start failed')):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                self.service.refresh_pull_requests()
        self.assertEqual(self.service._pending_decision_sources, set())
        self.assertIsNone(self.service._ready_decision_sources)
        with patch.object(self.service, '_refresh_decision_sources', side_effect=RuntimeError('source failed')):
            with self.assertRaisesRegex(RuntimeError, 'source failed'):
                self.service.refresh_pull_requests()
        self.assertEqual(self.service._pending_decision_sources, set())
        self.assertIsNone(self.service._ready_decision_sources)
        self.service.refresh_pull_requests()
        # The queue starts one check at a time. It takes the first card whose source read has finished, and under load the
        # newer card can finish first. So the test checks that the failures left the queue usable, not which card goes first.
        queued = [c['decision_id'] for c in self.service._all('contexts')]
        self.assertEqual(len(queued), 1)
        self.assertIn(queued[0], ('older-card', 'newer-card'))

    def test_shutdown_clears_pending_sources_without_late_database_write(self):
        self.cards()
        blocked, release, returned = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.service._refresh_decision_sources

        def source(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except Exception as error:
                errors.append(error)
                raise
            finally:
                returned.set()

        def discussion(url):
            blocked.set()
            if not release.wait(5):
                raise TimeoutError('Test did not release discussion read')
            return {'fingerprint': 'after shutdown'}

        # One card ensures returned signals the only in-flight source job.
        with self.service.transaction():
            newer = self.service._get('decisions', 'newer-card')
            newer['status'] = 'resolved'
            self.service._put('decisions', newer)
        self.tracker.observe_discussion = discussion
        with patch.object(self.service, '_refresh_decision_sources', side_effect=source):
            worker = threading.Thread(target=self.service.refresh_pull_requests)
            worker.start()
            try:
                self.assertTrue(blocked.wait(2))
                self.service.stopping.set()
                worker.join(1)
                self.assertFalse(worker.is_alive(), 'Shutdown must not wait for discussion reads')
                self.assertEqual(self.service._pending_decision_sources, set())
                # Model a closed database without closing the fixture twice.
                with patch.object(self.service, 'transaction', side_effect=AssertionError('late database access')):
                    release.set()
                    self.assertTrue(returned.wait(2))
                    self.assertEqual(errors, [])
            finally:
                release.set()
                worker.join(5)
