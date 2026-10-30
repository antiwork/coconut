import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class StateSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.write('POST', '/v1/bots', {'name': 'Desk'})

    def write(self, method, path, data):
        return self.service.mutate(method, path, data, str(uuid4()), 'phone')

    def test_content_and_history_do_not_wait_for_native_settings(self):
        with self.service.transaction():
            expected = [self.service._message(self.bot['id'], str(i), role='user')['id'] for i in range(135)]
        with patch.object(self.service, '_model_catalog', side_effect=AssertionError('Native discovery blocks content')):
            state = self.service.snapshot(content_only=True)
        self.assertEqual([m['id'] for m in state['messages']], expected[-60:])
        cursor = state['history_before'][self.bot['id']]
        second = self.service.message_page(self.bot['id'], cursor)
        third = self.service.message_page(self.bot['id'], second['before'])
        self.assertEqual([m['id'] for m in third['messages'] + second['messages'] + state['messages']], expected)
        self.assertIsNone(third['before'])
        self.assertEqual(state['sync_version'], 1)

    def test_native_content_reads_only_referenced_or_active_tasks(self):
        from types import SimpleNamespace
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Current request'})
        self.service.provider.supports_native_outcomes = True
        read = Service._all
        def bounded(service, table):
            if table in ('tasks', 'exchanges'):
                raise AssertionError('Content must not decode complete execution history')
            return read(service, table)
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={})), patch.object(Service, '_all', bounded):
            state = self.service.snapshot(content_only=True)
        self.assertEqual([t['id'] for t in state['tasks']], [task['id']])

    def test_full_state_retains_cancelled_original_after_recent_history_ages_out(self):
        original = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Original checkout'})
        successor = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Resumed checkout'})
        with self.service.transaction():
            original['status'] = 'cancelled'
            self.service._put('tasks', original)
            for number in range(80):
                self.service._message(self.bot['id'], str(number))
        recent = self.service.snapshot(content_only=True, compact_history=True)
        full = self.service.snapshot()
        self.assertNotIn(original['id'], {task['id'] for task in recent['tasks']})
        self.assertIn(successor['id'], {task['id'] for task in recent['tasks']})
        self.assertIn(original['id'], {task['id'] for task in full['tasks']})
        self.assertIn(successor['id'], {task['id'] for task in full['tasks']})

    def test_recent_snapshot_loads_card_history_with_its_conversation_page(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Review this outcome'})
        template = dict(bot_id=self.bot['id'], task_id=task['id'], action_type='question',
            title='Review the result?', status='resolved', revision=1, created_at=task['created_at'])
        with self.service.transaction():
            for identity in ('old-card', 'prior-choice', 'pending-card'):
                value = dict(template, id=identity)
                if identity == 'pending-card':
                    value.update(status='pending', previous_decision_id='prior-choice')
                self.service._put('decisions', value)
                self.service._message(self.bot['id'], identity, task_id=task['id'], decision_id=identity)
            for i in range(80):
                self.service._message(self.bot['id'], str(i), task_id=task['id'])
        state = self.service.snapshot(content_only=True, compact_history=True)
        self.assertEqual({d['id'] for d in state['decisions']}, {'pending-card', 'prior-choice'})
        self.assertIn('pending-card', {m.get('decision_id') for m in state['messages']})
        page = self.service.message_page(self.bot['id'], state['history_before'][self.bot['id']])
        self.assertEqual({d['id'] for d in page['decisions']}, {'old-card', 'prior-choice', 'pending-card'})
        self.assertEqual({t['id'] for t in page['tasks']}, {task['id']})
        self.assertEqual(len(self.service.snapshot(content_only=True)['decisions']), 3)
        reset = self.service.updates(state['cursor'] + 1000, collection_deltas=True, native_events=True, compact_history=True)
        self.assertEqual({d['id'] for d in reset['reset']['decisions']}, {'pending-card', 'prior-choice'})

    def test_new_followup_delivers_earlier_choice_from_an_older_task(self):
        from coconut.service import now
        old = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Original choice'})
        previous = dict(id='earlier', bot_id=self.bot['id'], task_id=old['id'], status='resolved',
            action_type='question', title='Original choice?', revision=1, created_at=now(), selected_option_label='Include both')
        self.service._put('decisions', previous)
        self.service._message(self.bot['id'], 'Earlier card', task_id=old['id'], decision_id=previous['id'])
        for i in range(80):
            self.service._message(self.bot['id'], str(i))
        state = self.service.snapshot(content_only=True, compact_history=True)
        self.assertEqual(state['decisions'], [])
        current = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'The new result'})
        followup = dict(previous, id='followup', task_id=current['id'], status='pending', previous_decision_id=previous['id'])
        self.service._put('decisions', followup)
        self.service._event('decision.created', followup, self.bot['id'], current['id'])
        page = self.service.updates(state['cursor'], collection_deltas=True, native_events=True, compact_history=True)
        self.assertEqual({d['id'] for d in page['decisions']}, {'earlier', 'followup'})
        self.assertEqual({t['id'] for t in page['tasks']}, {old['id'], current['id']})
        card = self.service.card('followup')
        self.assertEqual({d['id'] for d in card['decisions']}, {'earlier', 'followup'})
        self.assertEqual({t['id'] for t in card['tasks']}, {old['id'], current['id']})

    def test_updates_include_new_and_hidden_messages_and_changed_tasks(self):
        initial = self.service.snapshot(content_only=True)
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        page = self.service.updates(initial['cursor'])
        self.assertNotIn('reset', page)
        self.assertIn(task['id'], [t['id'] for t in page['tasks']])
        message = next(m for m in page['messages'] if m['role'] == 'user')
        with self.service.transaction():
            stored = self.service._get('messages', message['id'])
            stored['_hidden'] = True
            self.service._put('messages', stored)
            self.service._event('message.updated', stored, self.bot['id'], task['id'])
        changed = self.service.updates(page['cursor'])
        self.assertIn(message['id'], changed['removed_messages'])
        self.assertEqual(changed['messages'], [])
        observation = {'id': 'current-work', 'activity_observed_at': 'new-observation'}
        with patch.object(Service, '_work_projection', return_value=[observation]):
            idle = self.service.updates(changed['cursor'])
        self.assertEqual(idle['artifacts'], [observation])
        self.assertEqual(idle['after'], idle['cursor'])
        self.assertNotIn('messages', idle)

    def test_unknown_events_and_missing_replay_reset_content(self):
        initial = self.service.snapshot(content_only=True)
        with self.service.transaction():
            self.service._event('future.event', {})
        reset = self.service.updates(initial['cursor'])
        self.assertEqual(reset['reset']['cursor'], reset['cursor'])
        with self.service.transaction():
            self.service.db.execute('DELETE FROM events WHERE seq=?', (reset['cursor'],))
            self.service._event('task.updated', {})
        self.assertIn('reset', self.service.updates(initial['cursor']))

    def test_empty_delta_reads_no_collections_or_work_history(self):
        cursor = self.service._cursor()
        timestamp = self.service.db.execute('SELECT created_at FROM events WHERE seq=?', (cursor,)).fetchone()[0]
        with patch('coconut.service.now', return_value=timestamp), \
             patch.object(Service, '_all', side_effect=AssertionError('No collection changed')), \
             patch.object(Service, '_work_projection', side_effect=AssertionError('No work changed')):
            page = self.service.updates(cursor, collection_deltas=True)
        self.assertEqual(page, dict(after=cursor, cursor=cursor,
            server_time=timestamp, collection_deltas=True))

    def test_idle_delta_keeps_work_observations_fresh(self):
        cursor = self.service._cursor()
        observation = {'id': 'work', 'activity_observed_at': 'current'}
        with patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_work_projection', return_value=[observation]):
            page = self.service.updates(cursor, collection_deltas=True)
        self.assertEqual(page['artifacts'], [observation])

    def test_native_idle_delta_does_not_rebuild_unchanged_work_after_time_passes(self):
        from types import SimpleNamespace
        cursor = self.service._cursor()
        self.service.provider.supports_native_outcomes = True
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_all', side_effect=AssertionError('No native collection changed')), \
             patch.object(Service, '_work_projection', side_effect=AssertionError('No native work changed')):
            page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual(page, dict(after=cursor, cursor=cursor,
            server_time='2099-01-01T00:00:00Z', collection_deltas=True, native_work_observed_at='2099-01-01T00:00:00Z'))

    def test_native_progress_event_delivers_after_an_idle_read(self):
        from types import SimpleNamespace
        self.service.provider.supports_native_outcomes = True
        cursor = self.service._cursor()
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'):
            idle = self.service.updates(cursor, collection_deltas=True, native_events=True)
            with self.service.transaction():
                self.service._event('work.updated', {})
            observation = {'id': 'native-work', 'work_state': 'completed'}
            with patch.object(Service, '_work_projection', return_value=[observation]):
                page = self.service.updates(idle['cursor'], collection_deltas=True, native_events=True)
        self.assertEqual(page['artifacts'], [observation])
        self.assertGreater(page['cursor'], cursor)

    def test_native_old_clients_keep_periodic_artifact_refreshes(self):
        from types import SimpleNamespace
        cursor = self.service._cursor()
        self.service.provider.supports_native_outcomes = True
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={})), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_work_projection', return_value=[{'id': 'work'}]):
            page = self.service.updates(cursor, collection_deltas=True)
        self.assertEqual(page['artifacts'], [{'id': 'work'}])
        self.assertNotIn('native_work_observed_at', page)

    def test_protocol_three_keeps_retained_work_fresh_until_migration(self):
        from types import SimpleNamespace
        self.service.provider.supports_native_outcomes = True
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Retained work'})
        artifact = dict(id='retained', kind='work', task_id=task['id'], bot_id=self.bot['id'],
            status='blocked', title='Paused work', created_at='2026-09-18T00:00:00Z')
        with self.service.transaction():
            self.service._put('artifacts', artifact)
        cursor = self.service._cursor()
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_work_projection', return_value=[artifact]):
            for status in ('blocked', 'completed'):
                artifact['status'] = status
                with self.service.transaction():
                    self.service._put('artifacts', artifact)
                page = self.service.updates(cursor, collection_deltas=True, native_events=True)
                self.assertEqual(page['artifacts'], [artifact])
            with self.service.transaction():
                self.service.db.execute('INSERT INTO settings VALUES (?,?)', ('work.cancelled.work:retained', 'true'))
            self.assertNotIn('artifacts', self.service.updates(cursor, collection_deltas=True, native_events=True))

    def test_idle_native_update_projects_only_unfinished_retained_work(self):
        from types import SimpleNamespace
        self.service.provider.supports_native_outcomes = True
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Retained work'})
        retained = dict(id='retained', kind='work', task_id=task['id'], bot_id=self.bot['id'],
            status='working', title='Retained work', created_at='2026-09-18T00:00:00Z')
        self.service._put('artifacts', retained)
        self.service._put('artifacts', dict(retained, id='native', _native_outcome_id='t_current'))
        self.service._put('artifacts', dict(retained, id='cancelled', status='cancelled'))
        self.service._put('artifacts', dict(retained, id='hidden', _hidden=True))
        cursor = self.service._cursor()
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_all', side_effect=AssertionError('An idle native read must not decode history')), \
             patch.object(Service, '_work_projection', side_effect=lambda items: items):
            page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual([a['id'] for a in page['artifacts']], ['retained'])
        self.assertNotIn('bots', page)

    def test_native_update_in_a_new_activity_interval_sends_only_changed_and_timed_work(self):
        from types import SimpleNamespace
        self.service.provider.supports_native_outcomes = True
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Retained work'})
        unchanged = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Native work'})
        retained = dict(id='retained', kind='work', task_id=task['id'], bot_id=self.bot['id'],
            status='working', title='Retained work', created_at='2026-09-18T00:00:00Z')
        with self.service.transaction():
            self.service._put('artifacts', retained)
            self.service._put('artifacts', dict(retained, id='native', task_id=unchanged['id'], _native_outcome_id='t_unchanged'))
        cursor = self.service._cursor()
        with self.service.transaction():
            self.service._event('task.updated', self.service._public(self.service._get('tasks', task['id'])),
                                self.bot['id'], task['id'])
        # The last event is in an earlier 15-second interval, so retained work is due for a new projection.
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_work_projection', side_effect=lambda items: items):
            page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual([a['id'] for a in page['artifacts']], ['retained'])

    def native_outcomes(self, *names):
        """Create one native Work outcome with a pull request for each name, and return their tasks."""
        self.service.provider.supports_native_outcomes = True
        tasks = {name: self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': name, 'mode': 'queue'})
                 for name in names}
        with self.service.transaction():
            for name, task in tasks.items():
                self.service._put('artifacts', dict(id=f'{name}-work', kind='work', task_id=task['id'], bot_id=self.bot['id'],
                    status='working', title=name, created_at=task['created_at'], _native_outcome_id=f't_{name}'))
                self.service._put('artifacts', dict(id=f'{name}-pr', kind='pr', task_id=task['id'], bot_id=self.bot['id'],
                    status='open', title=name, url=f'https://github.com/o/r/pull/{name}', work_outcome_id=f'{name}-work',
                    created_at=task['created_at']))
        return tasks

    def native_update(self, cursor):
        from types import SimpleNamespace
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch('coconut.service.now', return_value='2099-01-01T00:00:00Z'), \
             patch.object(Service, '_work_projection', side_effect=lambda items: items):
            return self.service.updates(cursor, collection_deltas=True, native_events=True)

    def test_decision_event_sends_only_the_work_of_its_outcome(self):
        tasks = self.native_outcomes('first', 'second')
        cursor = self.service._cursor()
        decision = dict(id='card', bot_id=self.bot['id'], task_id=tasks['first']['id'], status='pending', revision=1)
        with self.service.transaction():
            self.service._put('decisions', decision)
            self.service._event('decision.created', decision, self.bot['id'], decision['task_id'])
        page = self.native_update(cursor)
        self.assertEqual([a['id'] for a in page['artifacts']], ['first-work', 'first-pr'])
        self.assertEqual([d['id'] for d in page['decisions']], ['card'])

    def test_merge_decision_sends_the_work_of_its_pull_request_before_and_after_a_new_link(self):
        self.native_outcomes('first', 'second', 'third')
        other = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Review', 'mode': 'queue'})
        decision = dict(id='merge', bot_id=self.bot['id'], task_id=other['id'], status='pending', revision=1,
                        merge_pr_url='https://github.com/o/r/pull/first')
        with self.service.transaction():
            self.service._put('decisions', decision)
            self.service._event('decision.created', decision, self.bot['id'], other['id'])
        cursor = self.service._cursor()
        self.assertEqual([a['id'] for a in self.native_update(cursor - 1)['artifacts']], ['first-work', 'first-pr'])
        # A revision names another pull request. The first outcome no longer waits for this card.
        decision.update(merge_pr_url='https://github.com/o/r/pull/second', revision=2)
        with self.service.transaction():
            self.service._put('decisions', decision)
            self.service._event('decision.updated', decision, self.bot['id'], other['id'])
        page = self.native_update(cursor)
        self.assertEqual({a['id'] for a in page['artifacts']}, {'first-work', 'first-pr', 'second-work', 'second-pr'})

    def test_work_item_event_sends_only_the_work_of_its_outcome(self):
        self.native_outcomes('first', 'second')
        cursor = self.service._cursor()
        with self.service.transaction():
            item = self.service._get('artifacts', 'second-pr')
            item['status'] = 'merged'
            self.service._put('artifacts', item)
            self.service._event('artifact.updated', self.service._public(item), self.bot['id'], item['task_id'])
        page = self.native_update(cursor)
        self.assertEqual([a['id'] for a in page['artifacts']], ['second-work', 'second-pr'])
        self.assertEqual(page['removed_artifacts'], [])

    def test_pull_request_that_leaves_an_outcome_sends_the_outcome_that_shows_its_merge_card(self):
        tasks = self.native_outcomes('first', 'second')
        decision = dict(id='merge', bot_id=self.bot['id'], task_id=tasks['second']['id'], status='pending', revision=1,
                        merge_pr_url='https://github.com/o/r/pull/first')
        with self.service.transaction():
            self.service._put('decisions', decision)
            item = self.service._get('artifacts', 'first-pr')
            self.service._event('artifact.updated', self.service._public(item), self.bot['id'], item['task_id'])
        cursor = self.service._cursor()
        with self.service.transaction():
            item.pop('work_outcome_id')
            self.service._put('artifacts', item)
            self.service._event('artifact.updated', self.service._public(item), self.bot['id'], item['task_id'])
        page = self.native_update(cursor)
        self.assertEqual([a['id'] for a in page['artifacts']], ['first-work', 'first-pr'])

    def test_structural_update_reads_only_the_rows_it_names(self):
        tasks = self.native_outcomes('first', 'second')
        chat = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'A question without Work'})
        decision = dict(id='card', bot_id=self.bot['id'], task_id=tasks['first']['id'], status='pending', revision=1)
        with self.service.transaction():
            self.service._put('decisions', decision)
            self.service._event('decision.created', decision, self.bot['id'], decision['task_id'])
        cursor = self.service._cursor()
        with self.service.transaction():
            decision['title'] = 'Changed question'
            self.service._put('decisions', decision)
            self.service._event('decision.updated', decision, self.bot['id'], decision['task_id'])
            task = self.service._get('tasks', chat['id'])
            self.service._event('task.updated', self.service._public(task), self.bot['id'], task['id'])
        read = Service._all
        def bounded(service, table):
            if table != 'bots':
                raise AssertionError(f'An update must not decode every row of {table}')
            return read(service, table)
        with patch.object(Service, '_all', bounded):
            page = self.native_update(cursor)
        self.assertEqual([d['id'] for d in page['decisions']], ['card'])
        self.assertEqual({t['id'] for t in page['tasks']}, {tasks['first']['id'], chat['id']})
        self.assertEqual([a['id'] for a in page['artifacts']], ['first-work', 'first-pr'])

    def test_removed_work_lists_only_rows_that_this_update_hides(self):
        tasks = self.native_outcomes('first', 'second')
        with self.service.transaction():
            self.service._put('artifacts', dict(id='old-hidden', kind='file', task_id=tasks['first']['id'],
                bot_id=self.bot['id'], title='Preview', created_at=tasks['first']['created_at'], _hidden=True))
        cursor = self.service._cursor()
        with self.service.transaction():
            item = self.service._get('artifacts', 'second-pr')
            item['_hidden'] = True
            self.service._put('artifacts', item)
            self.service._event('artifact.updated', dict(self.service._public(item), _hidden=True), self.bot['id'], item['task_id'])
        page = self.native_update(cursor)
        self.assertEqual(page['removed_artifacts'], ['second-pr'])
        self.assertEqual([a['id'] for a in page['artifacts']], ['second-work'])
        # A task that leaves the conversation takes its Work with it.
        cursor = page['cursor']
        with self.service.transaction():
            task = self.service._get('tasks', tasks['first']['id'])
            task['_hidden'] = True
            self.service._put('tasks', task)
            self.service._event('task.updated', self.service._public(task), self.bot['id'], task['id'])
        self.assertEqual(self.native_update(cursor)['removed_artifacts'], ['first-pr', 'first-work', 'old-hidden'])

    def test_retained_work_search_reads_only_the_tasks_of_unfinished_legacy_work(self):
        tasks = self.native_outcomes('native')
        legacy = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Legacy', 'mode': 'queue'})
        template = dict(kind='pr', task_id=legacy['id'], bot_id=self.bot['id'], title='Legacy', created_at=legacy['created_at'])
        with self.service.transaction():
            self.service._put('artifacts', dict(template, id='merged-pr', status='merged', url='https://github.com/o/r/pull/1'))
            self.service._put('artifacts', dict(template, id='open-pr', status='open', url='https://github.com/o/r/pull/2'))
        read = Service._get
        owners = []
        def counted(service, table, identity):
            if table == 'tasks':
                owners.append(identity)
            return read(service, table, identity)
        with patch.object(Service, '_get', counted), self.service._read_view() as view, view._read_transaction():
            retained = view._retained_work_observations()
        self.assertEqual([a['id'] for a in retained], ['open-pr'])
        self.assertEqual(owners, [legacy['id']])
        self.assertNotIn(tasks['native']['id'], owners)

    def test_native_update_resends_work_whose_task_changed(self):
        from types import SimpleNamespace
        self.service.provider.supports_native_outcomes = True
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Native work'})
        other = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Other work'})
        with self.service.transaction():
            for identity, outcome in ((task['id'], 't_changed'), (other['id'], 't_unchanged')):
                stored = self.service._get('tasks', identity)
                stored['_native_outcome_id'] = outcome
                self.service._put('tasks', stored)
                self.service._put('artifacts', dict(id=outcome, kind='work', task_id=identity, bot_id=self.bot['id'],
                    status='working', title='Native work', created_at='2026-09-18T00:00:00Z', _native_outcome_id=outcome))
        cursor = self.service._cursor()
        with self.service.transaction():
            self.service._event('task.updated', self.service._public(self.service._get('tasks', task['id'])),
                                self.bot['id'], task['id'])
        # A task status shows in the attempts of its native work, so only that work is sent again.
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')), \
             patch.object(Service, '_work_projection', side_effect=lambda items: items):
            page = self.service.updates(cursor, collection_deltas=True, native_events=True)
        self.assertEqual([a['id'] for a in page['artifacts']], ['t_changed'])

    def test_native_work_event_refreshes_an_idle_delta(self):
        cursor = self.service._cursor()
        with self.service.transaction():
            self.service._event('work.updated', {})
        observation = {'id': 'native-work', 'work_state': 'completed'}
        with patch.object(Service, '_work_projection', return_value=[observation]):
            page = self.service.updates(cursor, collection_deltas=True)
        self.assertEqual(page['artifacts'], [observation])
        self.assertGreater(page['cursor'], cursor)

    def test_text_replay_does_not_build_history_work_or_decisions(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        self.service.emit(task['id'], 'text_delta', {'text': 'First words'})
        timestamp = self.service.db.execute('SELECT created_at FROM events WHERE seq=?', (cursor,)).fetchone()[0]
        with patch('coconut.service.now', return_value=timestamp), \
             patch.object(Service, '_snapshot', side_effect=AssertionError('Replay must not build a snapshot')), \
             patch.object(Service, '_recent_messages', side_effect=AssertionError('Replay must not read history')), \
             patch.object(Service, '_work_projection', side_effect=AssertionError('Text must not rebuild Work')):
            page = self.service.updates(cursor)
        self.assertEqual([m['text'] for m in page['messages']], ['First words'])
        self.assertEqual([t['id'] for t in page['tasks']], [task['id']])
        self.assertNotIn('artifacts', page)
        self.assertNotIn('decisions', page)

    def test_continuous_text_refreshes_time_dependent_work(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        with self.service.transaction():
            self.service.db.execute('UPDATE events SET created_at=? WHERE seq=?', ('2026-09-16T12:00:00Z', cursor))
        self.service.emit(task['id'], 'text_delta', {'text': 'More words'})
        observation = {'id': 'work', 'activity_observed_at': 'current'}
        with patch('coconut.service.now', return_value='2026-09-16T12:00:16Z'), \
             patch.object(Service, '_work_projection', return_value=[observation]):
            page = self.service.updates(cursor)
        self.assertEqual(page['artifacts'], [observation])
        self.assertEqual(page['messages'][0]['text'], 'More words')

    def test_structural_replay_keeps_complete_replacement_collections(self):
        initial = self.service.snapshot(content_only=True)
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            for i in range(2):
                decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending', revision=1)
                self.service._put('decisions', decision)
                self.service._event('decision.created', decision, self.bot['id'], task['id'])
        page = self.service.updates(initial['cursor'])
        current = self.service.snapshot(content_only=True)
        self.assertEqual(page['decisions'], current['decisions'])
        self.assertEqual(page['confirmed_request_ids'], current['confirmed_request_ids'])
        self.assertEqual(page['bots'], current['bots'])

    def test_collection_deltas_preserve_old_cards_and_remove_hidden_records(self):
        records = []
        for index in range(3):
            task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': f'Outcome {index}', 'mode': 'queue'})
            decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='approved', revision=1)
            with self.service.transaction():
                self.service._put('decisions', decision)
            records.append(decision)
        artifact = dict(id='visible-file', bot_id=self.bot['id'], task_id=records[0]['task_id'], kind='file', title='Evidence')
        with self.service.transaction():
            self.service._put('artifacts', artifact)
        before = self.service.snapshot(content_only=True)
        with self.service.transaction():
            records[1].update(title='Current question', revision=2, status='pending')
            self.service._put('decisions', records[1])
            self.service._event('decision.updated', records[1], self.bot['id'], records[1]['task_id'])
        page = self.service.updates(before['cursor'], collection_deltas=True)
        self.assertTrue(page['collection_deltas'])
        self.assertEqual([d['id'] for d in page['decisions']], [records[1]['id']])
        self.assertEqual(page['artifacts'], [])
        legacy = self.service.updates(before['cursor'])
        self.assertEqual(len(legacy['decisions']), 3)
        self.assertNotIn('collection_deltas', legacy)
        with self.service.transaction():
            records[1]['_hidden'] = True
            self.service._put('decisions', records[1])
            self.service._event('decision.updated', records[1], self.bot['id'], records[1]['task_id'])
            artifact['_hidden'] = True
            self.service._put('artifacts', artifact)
            self.service._event('artifact.updated', artifact, self.bot['id'], artifact['task_id'])
        removed = self.service.updates(page['cursor'], collection_deltas=True)
        self.assertEqual(removed['removed_decisions'], [records[1]['id']])
        self.assertEqual(removed['removed_artifacts'], [artifact['id']])
        self.assertNotIn(records[1]['id'], [d['id'] for d in removed['decisions']])

    def test_collection_delta_context_event_updates_card_reply_state(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending', revision=1)
        with self.service.transaction():
            self.service._put('decisions', decision)
        cursor = self.service._cursor()
        with self.service.transaction():
            context = dict(id=str(uuid4()), task_id=task['id'], decision_id=decision['id'], status='queued')
            self.service._put('contexts', context)
            self.service._event('context.queued', {'id': context['id']}, self.bot['id'])
        page = self.service.updates(cursor, collection_deltas=True)
        self.assertEqual([(d['id'], d['reply_pending']) for d in page['decisions']], [(decision['id'], True)])

    def test_stream_reconciliation_updates_one_message_and_withdraws_failed_text(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored['status'] = 'working'
            self.service._put('tasks', stored)
        cursor = self.service._cursor()
        self.service.emit(task['id'], 'text_delta', {'text': 'Draft'})
        first = self.service.updates(cursor)
        identity = first['messages'][0]['id']
        self.service.emit(task['id'], 'text_delta', {'text': 'Verified result.', 'replace': True})
        final = self.service.updates(first['cursor'])
        self.assertEqual([(m['id'], m['text']) for m in final['messages']], [(identity, 'Verified result.')])
        self.service.emit(task['id'], 'text_delta', {'text': '', 'replace': True})
        removed = self.service.updates(final['cursor'])
        self.assertEqual(removed['removed_messages'], [identity])
        self.assertNotIn(identity, [m['id'] for m in self.service.message_page(self.bot['id'])['messages']])

    def test_missing_event_inside_a_batch_resets_content(self):
        initial = self.service.snapshot(content_only=True)
        with self.service.transaction():
            for _ in range(3):
                self.service._event('task.updated', {})
            self.service.db.execute('DELETE FROM events WHERE seq=?', (initial['cursor'] + 2,))
        page = self.service.updates(initial['cursor'])
        self.assertIn('reset', page)
        self.assertEqual(page['reset']['cursor'], page['cursor'])

    def test_card_reads_current_revision_without_full_snapshot(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        decision = dict(id=str(uuid4()), bot_id=self.bot['id'], task_id=task['id'], status='pending', revision=2)
        with self.service.transaction():
            self.service._put('decisions', decision)
            reply = self.service._message(self.bot['id'], 'Earlier card discussion', decision_id=decision['id'])
            for index in range(65):
                self.service._message(self.bot['id'], str(index))
        self.assertIn(reply['id'], [m['id'] for m in self.service.snapshot(content_only=True)['messages']])
        with patch.object(self.service, '_snapshot', side_effect=AssertionError('Full sync is unnecessary')):
            card = self.service.card(decision['id'])
        self.assertEqual(card['messages'], [self.service._public(reply)])
        self.assertEqual(card['decision']['revision'], 2)
        self.assertEqual(card['decision']['request_text'], 'Check this.')
        with self.service.transaction():
            decision['_hidden'] = True
            self.service._put('decisions', decision)
        with self.assertRaises(ServiceError):
            self.service.card(decision['id'])

    def test_card_origins_read_only_their_task_messages_and_refresh_after_changes(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Original request'})
        with self.service.transaction():
            for _ in range(5):
                self.service._put('decisions', dict(id=str(uuid4()), bot_id=self.bot['id'],
                    task_id=task['id'], status='pending', revision=1))
        original = Service._history
        calls = []
        def read(view, bot_id, *, task_id=None, shared_message_ids=()):
            calls.append((bot_id, task_id))
            return original(view, bot_id, task_id=task_id, shared_message_ids=shared_message_ids)
        with patch.object(Service, '_history', read):
            state = self.service.snapshot(content_only=True)
        self.assertEqual(set(calls), {(self.bot['id'], task['id'])})
        self.assertEqual({d['request_text'] for d in state['decisions']}, {'Original request'})
        with self.service.transaction():
            source = self.service._get('messages', state['decisions'][0]['request_message_id'])
            source['text'] = 'Corrected request'
            self.service._put('messages', source)
        self.assertEqual({d['request_text'] for d in self.service.snapshot(content_only=True)['decisions']},
                         {'Corrected request'})

    def test_delegated_history_stays_in_its_conversation(self):
        delegate = self.write('POST', '/v1/bots', {'name': 'Dev'})
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'Check this.'})
        with self.service.transaction():
            stored = self.service._get('tasks', task['id'])
            stored.update(bot_id=delegate['id'], conversation_bot_id=self.bot['id'], source_bot_id=self.bot['id'])
            self.service._put('tasks', stored)
            message = self.service._message(delegate['id'], '', task_id=task['id'], kind='decision', decision_id='answered-card')
        page = self.service.message_page(self.bot['id'])
        self.assertNotIn(message['id'], [m['id'] for m in page['messages']])
        delegated = self.service.message_page(delegate['id'])
        self.assertIn(message['id'], [m['id'] for m in delegated['messages']])
        self.assertIn(task['id'], [t['id'] for t in delegated['tasks']])

    def test_old_task_history_and_search_remain_available(self):
        task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': 'ÉVIDENCE hidden by pagination'})
        receipt = next(m['client_request_id'] for m in self.service._all('messages') if m.get('task_id') == task['id'] and m['role'] == 'user')
        with self.service.transaction():
            task = self.service._get('tasks', task['id'])
            task['status'] = 'completed'
            self.service._put('tasks', task)
            for i in range(65):
                self.service._message(self.bot['id'], str(i), role='assistant')
        state = self.service.snapshot(content_only=True)
        self.assertNotIn(task['id'], [t['id'] for t in state['tasks']])
        self.assertIn(receipt, state['confirmed_request_ids'])
        page = self.service.message_page(self.bot['id'], state['history_before'][self.bot['id']])
        self.assertIn(task['id'], [t['id'] for t in page['tasks']])
        self.assertEqual(self.service.search_conversations('évidence')['bot_ids'], [self.bot['id']])

    def test_relationship_index_is_reused_only_during_one_projection(self):
        tasks = {}
        for i in range(4):
            task = self.write('POST', f"/v1/bots/{self.bot['id']}/messages", {'text': str(i), 'mode': 'queue'})
            tasks[task['id']] = self.service._get('tasks', task['id'])
        artifacts = [dict(id=str(uuid4()), kind='work', task_id=t['id'], bot_id=self.bot['id'],
                          created_at=t['created_at'], title=t['title'], status='working') for t in tasks.values()]
        from coconut.work_status import WorkStatus
        # The task links come from one pass for both kinds of chain. The receipts add only their own links.
        with patch.object(WorkStatus, '_work_task_link_pairs', wraps=WorkStatus._work_task_link_pairs) as build:
            self.service._work_projection(artifacts)
            self.assertLessEqual(build.call_count, 1)
            count = build.call_count
            self.service._work_projection(artifacts)
            self.assertGreater(build.call_count, count)
        self.assertIsNone(self.service._work_graphs)

    def test_opening_the_settings_asks_for_the_live_model_lists(self):
        from coconut.state_sync import MODEL_REFRESH_SECONDS
        asked = []
        def catalog(refresh_after=None):
            asked.append(refresh_after)
        with patch.object(self.service, '_model_catalog', side_effect=catalog):
            self.service.native_snapshot()
            asked_by_settings = list(asked)
            # The content and history reads do not ask for a live look.
            self.service.snapshot(content_only=True)
        self.assertEqual(asked_by_settings, [MODEL_REFRESH_SECONDS])
        self.assertEqual(asked, asked_by_settings)

    def test_native_routines_keep_the_recorded_task(self):
        routine = dict(id=str(uuid4()), bot_id=self.bot['id'], last_task_id='recorded-task')
        with self.service.transaction():
            self.service._put('routines', routine)
        native = {'routines': [dict(routine, last_task_id=None)]}
        with patch.object(self.service.provider, 'observe_state', return_value=({}, native), create=True):
            state = self.service.native_snapshot()
        self.assertEqual(state['routines'][0]['last_task_id'], 'recorded-task')
        fallback = self.service.native_snapshot()
        self.assertEqual(fallback['routines'][0]['last_task_id'], 'recorded-task')


if __name__ == '__main__':
    unittest.main()
