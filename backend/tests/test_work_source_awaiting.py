from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_sources import awaiting_user, coverage


URL = 'https://github.com/example/private/issues/7'


def graphql(viewer, *authors):
    """One GraphQL answer. Each author is (issue author, last commenter or None)."""
    data = dict(viewer=dict(login=viewer))
    for index, (author, commenter) in enumerate(authors):
        nodes = [dict(author=dict(login=commenter))] if commenter else []
        data[f'r{index}'] = dict(author=dict(login=author), comments=dict(nodes=nodes))
    return SimpleNamespace(stdout=json.dumps(dict(data=data)))


class AwaitingUserTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(id=URL, updated_at='2026-10-21T07:45:03Z', title='Payout decision', labels=['awaiting-human'])

    def test_latest_comment_by_another_account_waits_for_the_person(self):
        calls = []
        def run(args):
            calls.append(args)
            return graphql('Person', ('helper', 'helper'))
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], run), {URL})
        self.assertEqual(len(calls), 1)
        self.assertIn(f'u0={URL}', calls[0])

    def test_question_without_comments_waits_for_the_person(self):
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], lambda args: graphql('person', ('helper', None))), {URL})

    def test_answer_by_the_person_ends_the_wait(self):
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], lambda args: graphql('person', ('helper', 'Person'))), set())

    def test_every_labeled_item_is_read_in_batches(self):
        items = [dict(self.item, id=f'{URL[:-1]}{number}') for number in range(1, 121)]
        calls = []
        def run(args):
            count = sum(arg.startswith('u') and '=' in arg for arg in args)
            calls.append(count)
            return graphql('person', *[('helper', 'helper')] * count)
        self.assertEqual(awaiting_user(items, ['awaiting-human'], run), {item['id'] for item in items})
        self.assertEqual(calls, [50, 50, 20])

    def test_unlabeled_items_need_no_read(self):
        self.item['labels'] = ['enhancement']
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], lambda args: self.fail('No read expected.')), set())

    def test_failed_read_keeps_the_earlier_rules(self):
        def run(args):
            raise subprocess.CalledProcessError(1, args)
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], run), set())
        self.assertEqual(awaiting_user([self.item], ['awaiting-human'], lambda args: SimpleNamespace(stdout='{}')), set())

    def test_only_a_card_covers_an_awaiting_item(self):
        current = datetime.now(timezone.utc)
        item = dict(self.item, awaiting_user=True)
        binding = dict(updated_at=item['updated_at'], task_ids=['owner'])
        tasks = {'owner': dict(status='working')}
        self.assertFalse(coverage(item, binding, tasks, {}, {}, current))
        card = dict(id='card', status='pending', sources=[dict(kind='issue', title='Payout', url=URL + '#issuecomment-1')])
        self.assertTrue(coverage(item, binding, tasks, {'card': card}, {}, current))
        card.update(status='executed', decided_at='2026-10-21T07:50:00Z')
        self.assertTrue(coverage(item, binding, tasks, {'card': card}, {}, current))
        card['decided_at'] = '2026-10-21T07:40:00Z'
        self.assertFalse(coverage(item, binding, tasks, {'card': card}, {}, current))
        card.update(status='dismissed', decided_at=None, _inbox_dismissed_at='2026-10-21T08:00:00Z')
        self.assertTrue(coverage(item, binding, tasks, {'card': card}, {}, current))
        other = dict(id='other', status='pending', sources=[dict(kind='issue', title='Other', url=URL[:-1] + '8')])
        self.assertFalse(coverage(item, binding, tasks, {'other': other}, {}, current))


class AwaitingAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = FixtureProvider(self.temp.name)
        self.provider.environment = lambda bot: ({}, self.root)
        self.service = Service(self.temp.name, self.provider, start_workers=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
        self.source = dict(id='assigned', bot_id=self.bot['id'], command=['read-assignments'], bindings='coverage.json')
        (self.root / 'work-sources.json').write_text(json.dumps([self.source]))
        self.item = dict(id=URL, updated_at='2026-10-21T07:45:03Z', title='Payout decision', labels=['awaiting-human'])
        self.commenter = 'helper'

    def audit(self):
        with self.service.transaction():
            state = self.service._setting('work-source:assigned', {})
            state['next_check'] = 0
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        def run(args, **kwargs):
            if args[0] == 'gh':
                return graphql('person', ('helper', self.commenter))
            return SimpleNamespace(stdout=json.dumps(dict(complete=True, items=[self.item])))
        with patch('coconut.work_sources.subprocess.run', side_effect=run):
            self.service.audit_work_sources()
        return self.service._setting('work-source:assigned')

    def test_native_work_does_not_cover_a_question_for_the_person(self):
        (self.root / 'coverage.json').write_text(json.dumps({URL: dict(updated_at=self.item['updated_at'], task_ids=['owner'])}))
        owner = self.service._send(self.bot, {'text': 'Earlier owner', 'mode': 'queue'}, record_message=False)
        with self.service.transaction():
            task = self.service._get('tasks', owner['id'])
            task['status'] = 'working'
            self.service._put('tasks', task)
        (self.root / 'coverage.json').write_text(json.dumps({URL: dict(updated_at=self.item['updated_at'], task_ids=[owner['id']])}))
        state = self.audit()
        self.assertEqual(state['missing'], [URL])
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertIn('This source waits for the person', prompt)
        self.assertIn('post their answer to the source as a comment', prompt)
        self.assertIn('"awaiting_user": true', prompt)

    def test_answer_by_the_person_restores_the_earlier_rules(self):
        self.commenter = 'person'
        state = self.audit()
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertNotIn('This source waits for the person', prompt)

    def test_pending_card_stops_another_audit(self):
        with self.service.transaction():
            self.service._put('decisions', dict(id='card', task_id='owner', bot_id=self.bot['id'], status='pending',
                                                sources=[dict(kind='issue', title='Payout', url=URL)]))
        state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(self.service._all('tasks'), [])


if __name__ == '__main__':
    unittest.main()
