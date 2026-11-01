from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from turn_text import turn_input
from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.work_sources import ANSWER_STEPS, awaiting_user, card_answer_steps, coverage


URL = 'https://github.com/example/private/issues/7'


def graphql(viewer, *authors, kind='Issue'):
    """One GraphQL answer. Each author is (issue author, last commenter or None)."""
    data = dict(viewer=dict(login=viewer))
    for index, (author, commenter) in enumerate(authors):
        nodes = [dict(author=dict(login=commenter))] if commenter else []
        data[f'r{index}'] = dict(__typename=kind, author=dict(login=author), comments=dict(nodes=nodes))
    return SimpleNamespace(stdout=json.dumps(dict(data=data)))


class AwaitingUserTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(id=URL, updated_at='2026-10-21T07:45:03Z', title='Payout decision', labels=['needs-answer'])

    def test_latest_comment_by_another_account_waits_for_the_person(self):
        calls = []
        def run(args):
            calls.append(args)
            return graphql('Person', ('helper', 'helper'))
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], run), {URL: 'Issue'})
        self.assertEqual(len(calls), 1)
        self.assertIn(f'u0={URL}', calls[0])

    def test_question_without_comments_waits_for_the_person(self):
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], lambda args: graphql('person', ('helper', None))), {URL: 'Issue'})

    def test_answer_by_the_person_ends_the_wait(self):
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], lambda args: graphql('person', ('helper', 'Person'))), {})

    def test_every_labeled_item_is_read_in_batches(self):
        items = [dict(self.item, id=f'{URL[:-1]}{number}') for number in range(1, 121)]
        calls = []
        def run(args):
            count = sum(arg.startswith('u') and '=' in arg for arg in args)
            calls.append(count)
            return graphql('person', *[('helper', 'helper')] * count)
        self.assertEqual(set(awaiting_user(items, ['needs-answer'], run)), {item['id'] for item in items})
        self.assertEqual(calls, [50, 50, 20])

    def test_unlabeled_items_need_no_read(self):
        self.item['labels'] = ['enhancement']
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], lambda args: self.fail('No read expected.')), {})

    def test_failed_read_keeps_the_earlier_rules(self):
        def run(args):
            raise subprocess.CalledProcessError(1, args)
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], run), {})
        self.assertEqual(awaiting_user([self.item], ['needs-answer'], lambda args: SimpleNamespace(stdout='{}')), {})

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

    def test_a_dismissed_card_covers_its_source_until_the_source_changes(self):
        current = datetime.now(timezone.utc)
        card = dict(id='card', status='dismissed', decided_at=None, _inbox_dismissed_at='2026-10-21T08:00:00Z',
                    sources=[dict(kind='issue', title='Payout', url=URL)])
        for awaiting in (True, False):
            with self.subTest(awaiting=awaiting):
                item = dict(self.item, awaiting_user=awaiting)
                # No live work covers the item. Only the dismissal does.
                self.assertTrue(coverage(item, {}, {}, {'card': card}, {}, current))
                # A new comment, label, or assignee after the dismissal is a change. Then a new card may come.
                changed = dict(item, updated_at='2026-10-21T09:00:00Z')
                self.assertFalse(coverage(changed, {}, {}, {'card': card}, {}, current))


class AnswerStepsTests(unittest.TestCase):
    def test_answer_steps_leave_the_workflow_to_the_bot_skills(self):
        # How to post an answer, and whether to close the source, is the person's workflow in the bot's skills.
        self.assertIn('as your skills for sources that wait for the person say', ANSWER_STEPS)
        self.assertNotIn('close', ANSWER_STEPS)
        self.assertNotIn('gh ', ANSWER_STEPS)

    def test_only_a_question_card_about_the_source_gets_the_steps(self):
        task = dict(_source_answer_steps=dict(url=URL))
        card = dict(action_type='question', sources=[dict(kind='issue', title='Payout', url=URL + '#issuecomment-1')])
        self.assertEqual(card_answer_steps(task, card), ANSWER_STEPS)
        # A task saved before the policy moved to the bot's skills gets the current text, not its saved close step.
        old = dict(_source_answer_steps=dict(url=URL, text='Then close the issue with gh issue close ' + URL))
        self.assertEqual(card_answer_steps(old, card), ANSWER_STEPS)
        self.assertIsNone(card_answer_steps(task, dict(card, action_type='native_approval')))
        self.assertIsNone(card_answer_steps(task, dict(card, sources=[dict(kind='issue', title='Other', url=URL[:-1] + '8')])))
        self.assertIsNone(card_answer_steps(task, dict(card, sources=[])))
        self.assertIsNone(card_answer_steps(task, None))
        self.assertIsNone(card_answer_steps({}, card))


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
        self.source = dict(id='assigned', bot_id=self.bot['id'], command=['read-assignments'], bindings='coverage.json',
                           awaiting_labels=['needs-answer'])
        (self.root / 'work-sources.json').write_text(json.dumps([self.source]))
        self.item = dict(id=URL, updated_at='2026-10-21T07:45:03Z', title='Payout decision', labels=['needs-answer'])
        self.commenter = 'helper'
        self.kind = 'Issue'

    def audit(self):
        with self.service.transaction():
            state = self.service._setting('work-source:assigned', {})
            state['next_check'] = 0
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        def run(args, **kwargs):
            if args[0] == 'gh':
                return graphql('person', ('helper', self.commenter), kind=self.kind)
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
        self.assertIn('Follow your skills for sources that wait for the person', prompt)
        self.assertIn('use one coconut_ask card and include the source link in sources', prompt)
        self.assertIn(ANSWER_STEPS, prompt)
        self.assertNotIn('gh issue close', prompt)
        self.assertIn('"awaiting_user": true', prompt)
        self.assertIn('"github_type": "Issue"', prompt)
        self.assertEqual(self.service._get('tasks', state['task_id'])['_source_answer_steps'], dict(url=URL))

    def test_pull_request_behind_an_issue_url_reports_its_type(self):
        # The bot's skills decide what to do with each type, so the source data carries the type that GitHub reports.
        self.kind = 'PullRequest'
        state = self.audit()
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertIn(ANSWER_STEPS, prompt)
        self.assertIn('"github_type": "PullRequest"', prompt)

    def test_answered_card_repeats_the_answer_steps_when_the_bot_resumes(self):
        state = self.audit()
        task = self.service._get('tasks', state['task_id'])
        question = dict(action_type='question', sources=[dict(kind='issue', title='Payout', url=URL)])
        task = dict(task, _source_answer_steps=card_answer_steps(task, question))
        for status in ('approved', 'replied'):
            card = dict(id='card', title='Payout', action_type='question', status=status, correction='Use option B.')
            prompt, _ = turn_input(dict(operation='resume', task=task, decision=card, history=[]))
            self.assertIn('This card asks about a source that waits for the person. ' + ANSWER_STEPS, prompt)
        card = dict(id='card', title='Payout', action_type='question', status='modified', correction='Ask again.')
        prompt, _ = turn_input(dict(operation='resume', task=task, decision=card, history=[]))
        self.assertNotIn(ANSWER_STEPS, prompt)

    def test_no_label_waits_for_the_person_unless_the_source_names_one(self):
        # Which label asks for the person is the person's choice. Without awaiting_labels, Coconut reads no comments.
        del self.source['awaiting_labels']
        (self.root / 'work-sources.json').write_text(json.dumps([self.source]))
        state = self.audit()
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertNotIn('This source waits for the person', prompt)

    def test_answer_by_the_person_restores_the_earlier_rules(self):
        self.commenter = 'person'
        state = self.audit()
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertNotIn('This source waits for the person', prompt)

    def test_a_dismissed_card_stops_audits_until_the_source_changes_and_reaches_the_bot_as_dismissed(self):
        state = self.audit()
        asking = self.service._get('tasks', state['task_id'])
        with self.service.transaction():
            asking['status'] = 'completed'
            self.service._put('tasks', asking)
            self.service._put('decisions', dict(id='card', task_id=asking['id'], bot_id=self.bot['id'], title='Pay out?',
                                                status='dismissed', decided_at=None, _answers=[],
                                                _inbox_dismissed_at='2026-10-21T08:00:00Z',
                                                sources=[dict(kind='issue', title='Payout', url=URL)]))
        count = len(self.service._all('tasks'))
        state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(len(self.service._all('tasks')), count)
        self.item['updated_at'] = '2026-10-21T09:00:00Z'
        state = self.audit()
        self.assertEqual(state['missing'], [URL])
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        data = json.loads(prompt.split('Answered cards (data): ', 1)[1].split('\n', 1)[0])
        self.assertEqual(data, [dict(id='card', title='Pay out?', status='dismissed', dismissed_at='2026-10-21T08:00:00Z')])
        self.assertIn('A card with status dismissed has no answer', prompt)
        self.assertNotIn('Answers to post', prompt)

    def test_pending_card_stops_another_audit(self):
        with self.service.transaction():
            self.service._put('decisions', dict(id='card', task_id='owner', bot_id=self.bot['id'], status='pending',
                                                sources=[dict(kind='issue', title='Payout', url=URL)]))
        state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(self.service._all('tasks'), [])


if __name__ == '__main__':
    unittest.main()
