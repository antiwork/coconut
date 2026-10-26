"""Source delivery must not create a second lifecycle for native work."""
import json
import unittest
from unittest.mock import patch

import test_native_kanban as native
import test_work_source_coverage as legacy
from hermes_env import needs_hermes


@needs_hermes
class NativeSourceOwnershipTests(unittest.TestCase):
    setUp = native.NativeKanbanTests.setUp
    write = native.NativeKanbanTests.write
    native_program = native.NativeKanbanTests.native_program
    audit = legacy.WorkSourceTests.audit

    def source(self):
        self.root = self.service.root
        self.provider.environment = lambda bot: ({}, self.root)
        self.provider.supports_native_outcomes = True
        self.source_config = dict(id='assigned', bot_id=self.bot['id'], command=['read-assignments'], bindings='coverage.json')
        (self.root / 'work-sources.json').write_text(json.dumps([self.source_config]))
        self.item = dict(id='https://github.com/example/repo/issues/1', updated_at='2026-09-18T12:00:00Z', title='Fix the accepted issue')
        return self.audit()['task_id']

    def outcome(self, origin):
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Finish the accepted issue', body='Verify the result', assignee='default',
    tenant=sys.argv[2], session_id=sys.argv[3], initial_status='blocked'))
""", self.bot['id'], origin)
        self.service.kanban.call({'op': 'snapshot'})
        self.service._adopt_native_outcomes()
        for task in self.service._all('tasks'):
            task['status'] = 'completed'
            self.service._put('tasks', task)
        return identity

    def test_completed_audit_keeps_coverage_after_native_snapshot_window_moves(self):
        origin = self.source()
        identity = self.native_program("""
rows = [dict(id=external, bot_id=sys.argv[2], title='Reconciled assignment',
    state='done', input=external, priority=10, workspace=sys.argv[1], summary='Verified')
    for external in [sys.argv[3], *('later-' + str(index) for index in range(40))]]
board.sync(rows)
identity = board.conn.execute('SELECT native_id FROM coconut_bindings WHERE external_id=?', (sys.argv[3],)).fetchone()[0]
print(identity)
""", self.bot['id'], origin)
        task = self.service._get('tasks', origin)
        task.update(status='completed', _source_native_ids=[identity])
        self.service._put('tasks', task)
        self.service.kanban.call({'op': 'snapshot'})
        self.assertNotIn(identity, {row['id'] for row in self.service.kanban.tasks.values()})
        count = len(self.service._all('tasks'))
        for _ in range(2):
            self.service.kanban.call({'op': 'snapshot'})
            state = self.audit()
            self.assertEqual(state['missing'], [])
            self.assertIn(identity, state['native_links'][self.item['id']]['task_ids'])
            self.assertEqual(len(self.service._all('tasks')), count)
        delegate = dict(task, id='completed-delegate', parent_task_id=origin)
        delegate.pop('_source_assignment', None)
        delegate.pop('_source_audit_prompt', None)
        self.service._put('tasks', delegate)
        task.pop('_source_native_ids')
        self.service._put('tasks', task)
        state['native_links'][self.item['id']]['task_ids'] = ['older-missing-native']
        self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                                ('work-source:assigned', json.dumps(state)))
        self.service.kanban.call({'op': 'snapshot'})
        state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(state['native_links'][self.item['id']]['task_ids'], [identity])
        self.assertEqual(len(self.service._all('tasks')), count + 1)
        self.item['updated_at'] = '2026-09-18T13:00:00Z'
        self.assertNotEqual(self.audit()['task_id'], origin)

    def test_new_source_audits_store_labels_and_schedule_by_urgency(self):
        self.source()
        self.source_config['label_priority'] = {'high': 15, 'medium': 12}
        (self.root / 'work-sources.json').write_text(json.dumps([self.source_config]))
        self.items = [dict(self.item, id=self.item['id'] + str(index), labels=[label])
                      for index, label in enumerate(('high', 'medium'))]
        self.audit()
        priorities = {row['id']: row['priority'] for row in self.service._kanban_rows()}
        tasks = {task.get('_source_assignment', {}).get('id'): task for task in self.service._all('tasks')}
        for item, expected in zip(self.items, (15, 12)):
            task = tasks[item['id']]
            self.assertEqual(task['_source_assignment']['labels'], item['labels'])
            self.assertEqual(priorities[task['id']], expected)

    def test_source_label_priority_sets_the_label_scheme(self):
        self.source()
        self.source_config['label_priority'] = {'P1': 17, 'P3': 3}
        (self.root / 'work-sources.json').write_text(json.dumps([self.source_config]))
        self.items = [dict(self.item, id=self.item['id'] + str(index), labels=[label])
                      for index, label in enumerate(('P1', 'P3', 'unknown'))]
        self.audit()
        priorities = {row['id']: row['priority'] for row in self.service._kanban_rows()}
        tasks = {task.get('_source_assignment', {}).get('id'): task for task in self.service._all('tasks')}
        for item, expected in zip(self.items, (17, 3, 10)):
            task = tasks[item['id']]
            self.assertEqual(task['_source_assignment']['label_priority'], self.source_config['label_priority'])
            self.assertEqual(priorities[task['id']], expected)

    def test_native_pause_and_completion_do_not_restart_an_assignment_audit(self):
        origin = self.source()
        identity = self.outcome(origin)
        count = len(self.service._all('tasks'))
        self.assertFalse((self.root / 'coverage.json').exists())
        for transition in ('', "board.kb.complete_task(board.conn, sys.argv[2], summary='Accepted outcome verified')"):
            if transition:
                self.native_program(transition, identity)
                self.service.kanban.call({'op': 'snapshot'})
            state = self.audit()
            self.assertEqual(state['missing'], [])
            self.assertEqual(len(self.service._all('tasks')), count)
        self.item['updated_at'] = '2026-09-18T13:00:00Z'
        self.assertNotEqual(self.audit()['task_id'], origin)

    def test_existing_native_id_binding_does_not_depend_on_chat_status(self):
        origin = self.source()
        identity = self.outcome(origin)
        task = self.service._get('tasks', origin)
        task.pop('_source_assignment')
        task.pop('_source_audit_prompt')
        self.service._put('tasks', task)
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: dict(
            updated_at=self.item['updated_at'], task_ids=[identity])}))
        self.assertEqual(self.audit()['missing'], [])

    def test_native_source_prompt_does_not_request_duplicate_bookkeeping(self):
        origin = self.source()
        prompt = self.service._get('tasks', origin)['_source_audit_prompt']
        self.assertIn('Hermes task', prompt)
        self.assertNotIn('save coverage', prompt)
        self.assertNotIn('Update existing Work rows', prompt)

    def test_unrelated_native_work_does_not_cover_another_source(self):
        origin = self.source()
        self.outcome(origin)
        self.item['id'] = 'https://github.com/example/repo/issues/2'
        self.assertNotEqual(self.audit()['task_id'], origin)

    def test_source_removal_refreshes_the_native_owners_card(self):
        origin = self.source()
        identity = self.outcome(origin)
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.service._put('decisions', dict(id='pending-card', task_id=owner['id'], status='pending', revision=1))
        self.items = []
        state = self.audit()
        self.assertNotEqual(state['task_id'], origin)
        self.assertIn('Fetch the exact source URL', self.service._get('tasks', state['task_id'])['_source_audit_prompt'])
        count = len(self.service._all('tasks'))
        self.audit()
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_native_work_from_a_delegated_origin_keeps_its_source_link(self):
        origin = self.source()
        child = self.service._send(self.service._get('bots', self.bot['id']), {'text': 'Handle the assigned outcome', 'mode': 'queue'}, record_message=False)
        task = self.service._get('tasks', child['id'])
        task['_assignment_task_id'] = origin
        self.service._put('tasks', task)
        self.outcome(child['id'])
        self.assertEqual(self.audit()['missing'], [])

    def test_new_revision_reuses_native_comment_receipt_without_a_status_file(self):
        origin = self.source()
        identity = self.outcome(origin)
        self.item['updated_at'] = '2026-09-18T13:00:00Z'
        audit = self.audit()['task_id']
        task = self.service._get('tasks', audit)
        task['status'] = 'working'
        self.service._put('tasks', task)
        receipt = self.service.native_event(audit, 'native_call', dict(name='kanban_comment', arguments=dict(task_id=identity, body='The source changed. Keep the recorded pause.')))
        result = self.native_program("import json; cid=board.kb.add_comment(board.conn,sys.argv[2],author='Desk',body='The source changed. Keep the recorded pause.'); print(json.dumps(dict(ok=True,task_id=sys.argv[2],comment_id=cid)))", identity)
        self.service.native_event(audit, 'native_result', dict(id=receipt['id'], result=result))
        task = self.service._get('tasks', audit)
        task['status'] = 'completed'
        self.service._put('tasks', task)
        count = len(self.service._all('tasks'))
        self.assertEqual(self.audit()['missing'], [])
        self.assertEqual(len(self.service._all('tasks')), count)

    def test_rejected_comment_does_not_establish_source_ownership(self):
        origin = self.source()
        task = self.service._get('tasks', origin)
        task['status'] = 'working'
        self.service._put('tasks', task)
        receipt = self.service.native_event(origin, 'native_call', dict(name='kanban_comment', arguments=dict(task_id='t_missing', body='Check the source')))
        self.service.native_event(origin, 'native_result', dict(id=receipt['id'], result=json.dumps(dict(ok=False,error='Missing task'))))
        self.assertNotIn('_source_native_ids', self.service._get('tasks', origin))

    def test_source_removal_retains_a_card_on_the_origin_audit(self):
        origin = self.source()
        self.outcome(origin)
        self.service._put('decisions', dict(id='origin-card', task_id=origin, status='pending', revision=1))
        self.items = []
        state = self.audit()
        self.assertNotEqual(state['task_id'], origin)
        self.assertIn('origin-card', state['native_links'][self.item['id']]['decision_ids'])

    def test_source_removal_retains_new_owner_before_its_first_link_poll(self):
        first = self.source()
        self.outcome(first)
        self.audit()
        self.item['updated_at'] = '2026-09-18T13:00:00Z'
        second = self.audit()['task_id']
        native = self.outcome(second)
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == native)
        self.service._put('decisions', dict(id='new-card', task_id=owner['id'], status='pending', revision=1))
        self.items = []
        state = self.audit()
        self.assertNotIn(state['task_id'], (first, second))
        self.assertIn('new-card', state['native_links'][self.item['id']]['decision_ids'])
        self.assertEqual(state['native_links'][self.item['id']]['updated_at'], self.item['updated_at'])

    def test_other_sources_do_not_reconcile_each_others_removed_items(self):
        origin = self.source()
        self.outcome(origin)
        self.service._put('decisions', dict(id='source-card', task_id=origin, status='pending', revision=1))
        second = dict(self.source_config, id='another-source', bindings='another.json')
        (self.root / 'work-sources.json').write_text(json.dumps([self.source_config, second]))
        count = len(self.service._all('tasks'))
        with patch('coconut.work_sources.subprocess.run') as run:
            run.return_value.stdout = json.dumps(dict(complete=True, items=[]))
            self.service.audit_work_sources()
        self.assertEqual(len(self.service._all('tasks')), count)
        self.assertEqual(self.service._setting('work-source:another-source')['missing'], [])

    def test_imported_answered_decision_without_task_ids_can_reconcile(self):
        self.source()
        self.item['id'] = 'https://github.com/example/repo/issues/2'
        self.service._put('decisions', dict(id='answered-card', task_id='old', status='executed'))
        (self.root / 'coverage.json').write_text(json.dumps({self.item['id']: dict(
            updated_at=self.item['updated_at'], decision_ids=['answered-card'])}))
        self.assertEqual(self.audit()['missing'], [self.item['id']])

    def test_legacy_null_revisions_do_not_stop_native_source_checks(self):
        origin = self.source()
        identity = self.outcome(origin)
        self.audit()
        task = self.service._get('tasks', origin)
        task['_source_assignment']['updated_at'] = None
        self.service._put('tasks', task)
        (self.root / 'coverage.json').write_text(json.dumps({
            self.item['id']: dict(updated_at=None, task_ids=[identity]),
            'https://github.com/example/repo/issues/old': dict(updated_at=None, task_ids=[]),
        }))
        state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(state['native_links'][self.item['id']]['updated_at'], self.item['updated_at'])
        self.assertIsNone(state['error'])

    def test_imported_link_without_a_revision_does_not_stop_the_audit(self):
        origin = self.source()
        identity = self.outcome(origin)
        self.audit()
        (self.root / 'coverage.json').write_text(json.dumps({
            'https://github.com/example/repo/pull/9': dict(task_ids=[identity], reason='Own draft, no review request'),
            'https://github.com/example/repo/pull/10': dict(decision_ids=['unknown-card']),
        }))
        state = self.audit()
        self.assertIsNone(state['error'])
        self.assertEqual(state['missing'], [])
        self.assertIsNone(state['native_links']['https://github.com/example/repo/pull/9']['updated_at'])

    def test_requested_unbound_native_task_is_not_duplicated(self):
        identity = self.native_program("""
print(board.kb.create_task(board.conn, title='Source follow-up', body='Verify',
    assignee='default', tenant=sys.argv[2], initial_status='blocked'))
""", self.bot['id'])
        result = self.service.kanban.call({'op': 'snapshot', 'native_ids': [identity, 'missing']})
        matches = [task for task in result['tasks'] if task['id'] == identity]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0]['unbound'])

    def test_adapter_failure_retains_coverage_and_does_not_stop_other_source(self):
        origin = self.source()
        task = self.service._get('tasks', origin)
        task.update(status='completed', _source_native_ids=['unavailable-native-task'])
        self.service._put('tasks', task)
        sources = [self.source_config, dict(self.source_config, id='other', bindings='other.json')]
        (self.root / 'work-sources.json').write_text(json.dumps(sources))
        before = self.service._setting('work-source:assigned')
        with patch.object(self.service.kanban, 'call', side_effect=RuntimeError('Adapter unavailable')):
            state = self.audit()
        self.assertEqual(state['error'], 'RuntimeError')
        self.assertEqual(state['task_id'], before['task_id'])
        self.assertEqual(state['checked_at'], before['checked_at'])
        other = self.service._setting('work-source:other')
        self.assertIsNone(other['error'])
        self.assertNotEqual(other['task_id'], origin)

    def test_requested_native_ids_exclude_other_sources_and_closed_assignments(self):
        origin = self.source()
        task = self.service._get('tasks', origin)
        task.update(status='completed', _source_native_ids=['current-native'])
        self.service._put('tasks', task)
        for identity, assignment in [
                ('closed', dict(task['_source_assignment'], id='https://github.com/example/repo/issues/closed')),
                ('other', dict(task['_source_assignment'], source_id='another-source'))]:
            self.service._put('tasks', dict(task, id=identity, _source_assignment=assignment,
                                           _source_native_ids=[identity + '-native']))
        with patch.object(self.service.kanban, 'call', wraps=self.service.kanban.call) as calls:
            self.audit()
        snapshots = [call.args[0] for call in calls.call_args_list if call.args[0]['op'] == 'snapshot']
        self.assertEqual([call['native_ids'] for call in snapshots], [['current-native']])

    def test_snapshot_lookup_follows_delegates_even_with_an_older_saved_link(self):
        origin = self.source()
        task = self.service._get('tasks', origin)
        task.update(status='completed')
        self.service._put('tasks', task)
        delegate = dict(task, id='delegate', parent_task_id=origin, _source_native_ids=['new-native'])
        delegate.pop('_source_assignment', None)
        delegate.pop('_source_audit_prompt', None)
        self.service._put('tasks', delegate)
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['native_links'] = {self.item['id']: dict(updated_at=self.item['updated_at'],
                                                         task_ids=['older-native'], decision_ids=[])}
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                                    ('work-source:assigned', json.dumps(state)))
        with patch.object(self.service.kanban, 'call', wraps=self.service.kanban.call) as calls:
            self.audit()
        snapshots = [call.args[0] for call in calls.call_args_list if call.args[0]['op'] == 'snapshot']
        self.assertEqual([call['native_ids'] for call in snapshots], [['new-native', 'older-native']])
