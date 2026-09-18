"""Source delivery must not create a second lifecycle for native work."""
import json
import unittest
from unittest.mock import patch

import test_native_kanban as native
import test_work_source_coverage as legacy


@unittest.skipUnless(native.SOURCE and native.PYTHON, 'Set Coconut Hermes paths for native integration checks.')
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
