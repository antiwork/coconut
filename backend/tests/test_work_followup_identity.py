import copy
import unittest
from unittest.mock import patch

import test_followup_conversation_scope as fixtures
from coconut.tracked_work import TrackedWork
import legacy_work_sync

setUpModule, tearDownModule = legacy_work_sync.scope()


class WorkFollowupIdentityTests(unittest.TestCase):
    setUp = fixtures.FollowupConversationScopeTests.setUp
    tearDown = fixtures.FollowupConversationScopeTests.tearDown
    write = fixtures.FollowupConversationScopeTests.write
    event = fixtures.FollowupConversationScopeTests.event
    question = fixtures.FollowupConversationScopeTests.question
    followup = fixtures.FollowupConversationScopeTests.followup
    finish_answer = fixtures.FollowupConversationScopeTests.finish_answer

    def report(self, key=None, status='blocked', title='Unblock the trial'):
        key = key or 'task-' + self.task['id']
        task = self.service._get('tasks', self.task['id'])
        task.update(status='working', _conversation_epoch=self.service._setting('conversation_epoch'))
        self.service._put('tasks', task)
        return self.service._register_work(task['id'], dict(key=key, title=title, status=status,
            activity='Inspecting staging' if status == 'working' else 'Inspection needs the app image'))

    def stages(self):
        first = self.report()
        decision = self.finish_answer('completed')
        original = copy.deepcopy(self.service._get('tasks', self.task['id']))
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            {'text': 'Inspect staging next', 'mode': 'queue'})
        followup = self.followup(previous_decision_id=decision['id'])
        self.write('POST', f'/v1/decisions/{followup["id"]}/answer', {'option_id': 'wait', 'revision': 1})
        return first, decision, original, followup

    def work(self):
        return [a for a in self.service.snapshot()['artifacts'] if a['kind'] == 'work']

    def legacy_pair(self):
        first, decision, original, followup = self.stages()
        with patch.object(TrackedWork, '_work_for_followup', return_value=None):
            later = self.report(title='Inspect staging')
        return first, later, original

    def test_linked_stage_updates_the_original_item_despite_a_new_key(self):
        first, decision, original, followup = self.stages()
        later = self.report(title='Inspect staging')
        self.assertEqual(later['id'], first['id'])
        self.assertEqual(later['key'], first['key'])
        self.assertEqual(len(self.work()), 1)
        self.assertEqual(self.work()[0]['title'], 'Inspect staging')
        context = self.service.work_context_input(first['id'])
        self.assertEqual({t['id'] for t in context['tasks']}, {original['id'], self.task['id']})
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'executed')
        self.assertEqual(self.report()['id'], first['id'])

    def test_existing_pair_keeps_latest_report_and_old_links(self):
        first, later, original = self.legacy_pair()
        self.service._sync_automatic_work()
        self.assertEqual([a['id'] for a in self.work()], [later['id']])
        self.assertEqual(self.work()[0]['title'], 'Inspect staging')
        context = self.service.work_context_input(first['id'])
        self.assertEqual(context['id'], later['id'])
        self.assertIn(original['id'], {t['id'] for t in context['tasks']})
        reply = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
            dict(text='How is this going?', work_artifact_id=first['id'], work_status_only=True))
        self.assertEqual(self.service._get('tasks', reply['id'])['_work_context_id'], later['id'])
        self.assertEqual(self.report(first['key'])['id'], later['id'])
        self.service._sync_automatic_work()
        self.assertEqual(len(self.work()), 1)

    def test_later_owner_still_resolves_the_full_decision_chain(self):
        first, later, original = self.legacy_pair()
        middle_task = self.task['id']
        middle_decision = self.service.snapshot()['decisions'][-1]
        self.event('completed', {'summary': 'The staging check ended.'})
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Find the app image', 'mode': 'queue'})
        question = self.followup(previous_decision_id=middle_decision['id'])
        self.write('POST', f'/v1/decisions/{question["id"]}/answer', {'option_id': 'wait', 'revision': 1})
        proposal_task = self.task['id']
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue the answered proposal', 'mode': 'queue'})
        resumed = self.service._get('tasks', self.task['id'])
        resumed['_resume_decision'] = question['id']
        self.service._put('tasks', resumed)
        self.report(later['key'], title='Find the staging app image')
        self.service._sync_automatic_work()
        self.assertEqual([a['id'] for a in self.work()], [later['id']])
        self.assertEqual(self.work()[0]['title'], 'Find the staging app image')
        context = self.service.work_context_input(first['id'])
        self.assertEqual({t['id'] for t in context['tasks']}, {original['id'], middle_task, proposal_task, self.task['id']})
        source = next(m for m in self.service._all('messages') if m['task_id'] == original['id'] and m['role'] == 'user')
        self.assertEqual(context['source_message']['id'], source['id'])

    def test_adoption_includes_a_middle_stage_without_a_work_report(self):
        first, _, original, middle_decision = self.stages()
        middle_task = self.task['id']
        task = self.service._get('tasks', middle_task); task['status'] = 'working'
        self.service._put('tasks', task)
        self.event('completed', {'summary': 'The middle stage found missing evidence.'})
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Continue the inspection', 'mode': 'queue'})
        question = self.followup(previous_decision_id=middle_decision['id'])
        self.write('POST', f'/v1/decisions/{question["id"]}/answer', {'option_id': 'wait', 'revision': 1})
        self.assertEqual(self.report()['id'], first['id'])
        context = self.service.work_context_input(first['id'])
        self.assertEqual({t['id'] for t in context['tasks']}, {original['id'], middle_task, self.task['id']})

    def test_similar_titles_without_a_decision_link_stay_separate(self):
        first = self.report()
        task = self.service._get('tasks', self.task['id']); task['status'] = 'completed'
        self.service._put('tasks', task)
        self.task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Another trial', 'mode': 'queue'})
        later = self.report('other-trial')
        self.service._sync_automatic_work()
        self.assertEqual({a['id'] for a in self.work()}, {first['id'], later['id']})

    def test_an_explicit_distinct_key_is_not_combined_with_previous_work(self):
        first, _, _, _ = self.stages()
        later = self.report('separate-request')
        self.service._sync_automatic_work()
        self.assertEqual({a['id'] for a in self.work()}, {first['id'], later['id']})

    def test_completed_work_is_not_reopened_by_a_linked_question(self):
        first, _, _, _ = self.stages()
        saved = self.service._get('artifacts', first['id']); saved['status'] = 'completed'
        self.service._put('artifacts', saved)
        later = self.report()
        self.service._sync_automatic_work()
        self.assertNotEqual(later['id'], first['id'])
        self.assertEqual(len(self.work()), 2)

    def test_separate_sessions_and_active_owners_are_not_taken_over(self):
        first, _, original, _ = self.stages()
        original['status'] = 'working'; self.service._put('tasks', original)
        later = self.report()
        self.service._sync_automatic_work()
        self.assertEqual(len(self.work()), 2)
        original.update(status='completed', session_id='old-session')
        self.service._put('tasks', original)
        self.service._sync_automatic_work()
        self.assertEqual(len(self.work()), 2)


if __name__ == '__main__':
    unittest.main()
