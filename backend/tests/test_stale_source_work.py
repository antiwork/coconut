"""Work whose GitHub sources closed must end, unless someone still needs it."""
import json
import time
import unittest
from unittest.mock import patch

import test_native_kanban as native
import test_native_source_ownership as ownership
import test_work_source_coverage as legacy
from hermes_env import needs_hermes


@needs_hermes
class StaleSourceWorkTests(unittest.TestCase):
    setUp = native.NativeKanbanTests.setUp
    write = native.NativeKanbanTests.write
    native_program = native.NativeKanbanTests.native_program
    audit = legacy.WorkSourceTests.audit
    source = ownership.NativeSourceOwnershipTests.source

    def outcome(self, origin):
        """A native card of the source that waits, as the stale cards on the Mac mini do."""
        identity = ownership.NativeSourceOwnershipTests.outcome(self, origin)
        self.native_program("board.kb.schedule_task(board.conn, sys.argv[2], reason='Waiting for a check')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        return identity

    def card(self, identity):
        return json.loads(self.native_program("""
import json
task = board.kb.get_task(board.conn, sys.argv[2])
print(json.dumps(dict(status=task.status, comments=[c.body for c in board.kb.list_comments(board.conn, sys.argv[2])])))
""", identity))

    def close(self, idle_age=0):
        """Read the source after it left the open list. The second read ends the work that is still idle."""
        self.items = []
        with patch('coconut.work_sources.STALE_IDLE_AGE', idle_age):
            return self.audit()

    def test_idle_card_of_a_closed_source_ends_with_a_note(self):
        from coconut.work_sources import STALE_NOTE
        identity = self.outcome(self.source())
        time.sleep(1.1)
        state = self.close()
        self.assertIn(self.item['id'], state['source_left'])
        card = self.card(identity)
        self.assertEqual(card['status'], 'archived')
        self.assertEqual(card['comments'], [STALE_NOTE])
        self.assertEqual(next(n['status'] for n in self.service.kanban.tasks.values() if n['id'] == identity), 'archived')

    def test_delegated_card_of_another_bot_ends_with_its_source(self):
        identity = self.outcome(self.source())
        delegated = self.native_program("""
child = board.kb.create_task(board.conn, title='Delegated review', assignee='default', tenant='another-bot')
board.kb.schedule_task(board.conn, child, reason='Waiting for a check')
print(child)
""")
        self.audit()
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['native_links'][self.item['id']]['task_ids'].append(delegated)
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        self.service.kanban.call({'op': 'snapshot', 'native_ids': [delegated]})
        time.sleep(1.1)
        self.close()
        self.assertEqual(self.card(identity)['status'], 'archived')
        self.assertEqual(self.card(delegated)['status'], 'archived')

    def test_recently_active_card_waits_for_the_idle_age(self):
        identity = self.outcome(self.source())
        self.close(idle_age=3600)
        self.assertEqual(self.card(identity)['status'], 'scheduled')

    def test_card_with_activity_after_the_source_left_stays(self):
        identity = self.outcome(self.source())
        self.close(idle_age=3600)
        # Someone worked on the card after the source left, so it is still in use.
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['source_left'][self.item['id']] = time.time() - 100
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        self.native_program("board.kb.add_comment(board.conn, sys.argv[2], 'bot', 'Diagnosing the post-merge failure')", identity)
        self.service.kanban.call({'op': 'snapshot'})
        self.close()
        self.assertEqual(self.card(identity)['status'], 'scheduled')

    def test_card_with_an_unanswered_card_for_the_person_stays(self):
        identity = self.outcome(self.source())
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.service._put('decisions', dict(id='pending-card', task_id=owner['id'], status='pending', revision=1))
        time.sleep(1.1)
        self.close()
        self.assertEqual(self.card(identity)['status'], 'scheduled')

    def test_card_with_another_open_source_stays(self):
        identity = self.outcome(self.source())
        other = 'https://github.com/example/repo/pull/2'
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['native_links'][other] = dict(updated_at='2026-09-18T12:00:00Z', task_ids=[identity], decision_ids=[])
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        time.sleep(1.1)
        self.items = [dict(id=other, updated_at='2026-09-18T12:00:00Z', title='Review the open PR')]
        with patch('coconut.work_sources.STALE_IDLE_AGE', 0):
            state = self.audit()
        self.assertEqual(self.card(identity)['status'], 'scheduled')
        self.assertNotIn(other, state['source_left'])

    def test_card_whose_archive_would_start_an_open_child_stays(self):
        identity = self.outcome(self.source())
        self.native_program("""
child = board.kb.create_task(board.conn, title='Unrelated follow-up', assignee='default', tenant=sys.argv[3])
board.kb.link_tasks(board.conn, sys.argv[2], child)
""", identity, self.bot['id'])
        self.service.kanban.call({'op': 'snapshot'})
        time.sleep(1.1)
        self.close()
        self.assertEqual(self.card(identity)['status'], 'scheduled')

    def test_queued_audit_of_a_closed_source_ends(self):
        from coconut.work_sources import STALE_NOTE
        origin = self.source()
        task = self.service._get('tasks', origin)
        task['status'] = 'queued'
        self.service._put('tasks', task)
        self.close()
        task = self.service._get('tasks', origin)
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['summary'], STALE_NOTE)

    def test_reconcile_audit_for_an_unanswered_card_stays(self):
        identity = self.outcome(self.source())
        owner = next(t for t in self.service._all('tasks') if t.get('_native_outcome_id') == identity)
        self.service._put('decisions', dict(id='pending-card', task_id=owner['id'], status='pending', revision=1))
        reconcile = self.close()['task_id']
        self.assertEqual(self.service._get('tasks', reconcile)['status'], 'queued')
        self.close()
        self.assertEqual(self.service._get('tasks', reconcile)['status'], 'queued')

    def test_pull_request_audit_outranks_bot_messages(self):
        self.source()
        self.items = [dict(id='https://github.com/example/repo/pull/3', updated_at='2026-09-18T12:00:00Z',
                           title='Review the PR', kind='pull_request', labels=[])]
        audit = self.audit()['task_id']
        self.assertEqual(self.service._get('tasks', audit)['_source_assignment']['kind'], 'pull_request')
        rows = {row['id']: row['priority'] for row in self.service._kanban_rows()}
        self.assertEqual(rows[audit], 15)


class StaleCardRuleTests(unittest.TestCase):
    def test_rule_keeps_open_sources_owner_cards_and_running_cards(self):
        from coconut.work_sources import source_left, stale_cards
        links = {'closed': dict(task_ids=['t_idle', 't_asked', 't_running', 'owner-of-t_owned'], decision_ids=[]),
                 'open': dict(task_ids=['t_shared'], decision_ids=[]),
                 'closed-too': dict(task_ids=['t_shared'], decision_ids=[]),
                 'carded': dict(task_ids=['t_held'], decision_ids=['card'])}
        native = {external: dict(id=identity, status='scheduled', worker_pid=None, current_run_id=None)
                  for external, identity in [('a', 't_idle'), ('b', 't_asked'), ('c', 't_shared'), ('d', 't_held'),
                                             ('e', 't_owned')]}
        native['f'] = dict(id='t_running', status='running', worker_pid=7, current_run_id=1)
        tasks = {'owner-of-t_owned': dict(id='owner-of-t_owned', _native_outcome_id='t_owned')}
        decisions = {'card': dict(id='card', task_id='x', status='pending'),
                     'asked': dict(id='asked', task_id='b', status='approved')}
        left = source_left({'open'}, links, {'closed': 100.0}, 200.0)
        self.assertEqual(left, {'closed': 100.0, 'closed-too': 200.0, 'carded': 200.0})
        self.assertEqual(stale_cards({'open'}, links, left, native, tasks, decisions, 300.0), {'t_idle': 100.0, 't_owned': 100.0})
        # A card that changed after its source left, or in the last hours, cannot end, so the board is not asked.
        native['a']['last_activity_at'] = 150.0
        native['e']['last_activity_at'] = 50.0
        self.assertEqual(stale_cards({'open'}, links, left, native, tasks, decisions, 300.0 + 6 * 3600), {'t_owned': 100.0})
        self.assertEqual(stale_cards({'open'}, links, left, native, tasks, decisions, 300.0), {})


if __name__ == '__main__':
    unittest.main()
