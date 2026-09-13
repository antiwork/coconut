import tempfile
import unittest
from dataclasses import replace
from uuid import uuid4

from coconut.service import Service
from test_service_pr import Tracker, observation, URL


class Provider:
    name = 'pr-review-fixture'
    ready = True
    model = 'fixture'
    def __init__(self): self.starts = []
    def start_task(self, task, bot, history, emit):
        self.starts.append(task)
        if task.get('_pr_review'):
            emit('text_delta', {'text': 'Current checks and reviews pass. Offer a merge decision for ' + URL})
        elif bot['name'] == 'Dev':
            emit('artifact', {'kind': 'pr', 'url': URL})
            emit('text_delta', {'text': 'Checks are running.'})
        elif task.get('_is_return') and 'Offer a merge decision' in str(task.get('_bot_messages')):
            emit('native_question', {'title': 'Merge PR #42?', 'recommended_option_id': 'merge',
                'options': [{'id':'merge','label':'Merge','explanation':'Merge the reviewed changes into main after checking the head.'},
                            {'id':'leave','label':'Leave open','explanation':'Keep the pull request open without merging.'}]})
        emit('completed', {'summary': 'Reported the current result.'})
    def cancel_task(self, task_id): pass


class PRReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.tracker = Tracker(replace(observation(), pr_state='open', draft=False, checks_state='running'))
        self.service = Service(self.temp.name, self.provider, start_workers=False, pr_tracker=self.tracker)
        self.desk = self.write('/v1/bots', {'name':'Desk','role':'Coordinator'})
        self.dev = self.write('/v1/bots', {'name':'Dev','role':'Development'})
        parent = self.write(f'/v1/bots/{self.desk["id"]}/messages', {'text':'Ask Dev to finish PR #42'})
        with self.service.transaction():
            task = self.service._get('tasks', parent['id']);task['status']='working';self.service._put('tasks',task)
            child = self.service._store_bot_message(task['id'], {'target_bot_id':self.dev['id'],'text':'Finish PR #42'})
            task = self.service._get('tasks', parent['id']);task['status']='waiting';self.service._put('tasks',task)
        self.child_id = child['task_id']
        self.service._run_task(self.child_id)
        self.service._drain_returns()
        for task in self.service._all('tasks'):
            if task.get('_is_return'): self.service._run_task(task['id'])
        self.artifact_id = self.service.snapshot()['artifacts'][0]['id']

    def tearDown(self):
        self.service.close();self.temp.cleanup()

    def write(self, path, data): return self.service.mutate('POST', path, data, str(uuid4()), 'test-phone')

    def green(self, **changes):
        result = replace(observation('ready_for_review'), pr_state='open', draft=False, checks_state='passed')
        self.tracker.observations.append(replace(result, **changes))
        self.service.refresh_pull_requests()

    def test_green_wakes_dev_then_desk_offers_one_pending_merge_card(self):
        before = len(self.service._all('messages'))
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
        self.assertEqual(len(self.service._all('messages')),before)
        self.service._run_task(self.child_id)
        self.assertEqual(self.provider.starts[-1]['_pr_review']['url'],URL)
        self.service._drain_returns()
        returns = [t for t in self.service._all('tasks') if t.get('_is_return') and t['status']=='queued']
        self.assertEqual(len(returns),1)
        self.assertEqual(returns[0]['bot_id'],self.desk['id'])
        self.service._run_task(returns[0]['id'])
        card = self.service.snapshot()['decisions'][0]
        self.assertEqual(card['bot_id'],self.desk['id'])
        self.assertEqual(card['status'],'pending')
        self.assertEqual(card['recommended_option_id'],'merge')
        count = len(self.provider.starts)
        self.green()
        self.assertEqual(len(self.provider.starts),count)
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')

    def test_restart_does_not_duplicate_queued_review(self):
        self.green()
        turn=self.service._get('tasks',self.child_id)['_native_turn_id']
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['_native_turn_id'],turn)

    def test_draft_or_failed_observation_never_wakes(self):
        self.green(draft=True)
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')
        self.green(observed=False,detail='No access')
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')

    def test_pending_decision_defers_review(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='needs_you';self.service._put('tasks',task)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'needs_you')
        self.assertNotIn('_reviewed_head',self.service._get('artifacts',self.artifact_id))

    def test_cancelled_or_reset_conversation_never_wakes(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='cancelled';self.service._put('tasks',task)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'cancelled')

    def test_review_binding_rejects_another_bots_task(self):
        from coconut.service import ServiceError
        desk_task=next(t for t in self.service._all('tasks') if t['bot_id']==self.desk['id'])
        with self.assertRaises(ServiceError):
            self.write('/v1/work/pull-requests', {'bot_id':self.dev['id'],'url':URL,'review_task_id':desk_task['id']})


    def test_waiting_dev_cannot_lose_review_to_desk(self):
        parent=next(t for t in self.service._all('tasks') if t['bot_id']==self.desk['id'] and not t.get('_is_return'))
        self.tracker.observations.append(replace(observation(),pr_state='open',draft=False,checks_state='running'))
        self.write('/v1/work/pull-requests',{'bot_id':self.desk['id'],'url':URL,'review_task_id':parent['id']})
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id);task['status']='needs_you';self.service._put('tasks',task)
        result=replace(observation('ready_for_review'),pr_state='open',draft=False,checks_state='passed')
        self.tracker.observations.extend([result,result]);self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks',parent['id'])['status'],'completed')
        self.assertFalse(any(a.get('_reviewed_head') for a in self.service._all('artifacts')))

    def test_failed_assignment_is_restored_before_continuation(self):
        with self.service.transaction():
            child=self.service._get('tasks',self.child_id)
            parent=dict(child,id=str(uuid4()),status='failed',_result_task_id=child['id'])
            self.service._put('tasks',parent)
            child['_assignment_task_id']=parent['id'];self.service._put('tasks',child)
        self.green()
        self.assertEqual(self.service._get('tasks',parent['id'])['status'],'waiting')
        self.service._run_task(self.child_id)
        self.assertEqual(self.provider.starts[-1]['_pr_review']['url'],URL)

    def test_native_prompt_keeps_verification_and_approval_boundaries(self):
        from coconut.hermes_worker import conversation_input
        prompt,_=conversation_input({'operation':'start','task':{'title':'Check PR','_pr_review':{'url':URL,'head_sha':'a'*40}},'history':[]})
        self.assertIn('not permission to merge',prompt)
        self.assertIn('required checks',prompt)
        self.assertIn('requesting bot',prompt)
        self.assertIn(URL,prompt)


    def test_upgrade_binds_legacy_artifact_to_original_task(self):
        with self.service.transaction():
            artifact=self.service._get('artifacts',self.artifact_id)
            artifact.pop('_review_task_id');self.service._put('artifacts',artifact)
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')

    def test_routine_registration_keeps_original_return_address(self):
        later=self.write(f'/v1/bots/{self.dev["id"]}/messages',{'text':'Check the same PR in this conversation'})
        self.service._run_task(later['id'])
        self.assertEqual(self.service._get('artifacts',self.artifact_id)['_review_task_id'],self.child_id)
        self.green()
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
        self.assertEqual(self.service._get('tasks',later['id'])['status'],'completed')

    def test_return_to_earlier_head_does_not_repeat_review(self):
        for head in ('a' * 40, 'b' * 40):
            self.green(head_sha=head)
            self.assertEqual(self.service._get('tasks',self.child_id)['status'],'queued')
            self.service._run_task(self.child_id)
            self.service._drain_returns()
            for task in self.service._all('tasks'):
                if task.get('_is_return') and task['status']=='queued':
                    self.service._run_task(task['id'])
        self.service.close()
        self.service=Service(self.temp.name,self.provider,start_workers=False,pr_tracker=self.tracker)
        self.green(head_sha='a' * 40)
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'completed')
        artifact=self.service._get('artifacts',self.artifact_id)
        self.assertEqual(set(artifact['_reviewed_heads']),{'a' * 40,'b' * 40})

    def test_cancelled_owner_does_not_block_new_assignment(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id)
            task['status']='cancelled';self.service._put('tasks',task)
        self.tracker.observations.append(replace(observation(),pr_state='open',draft=False,checks_state='running'))
        worker=self.write('/v1/bots',{'name':'Dev','role':'Development'})
        request=self.write(f'/v1/bots/{worker["id"]}/messages',{'text':'Finish PR #42'})
        self.service._run_task(request['id'])
        result=replace(observation('ready_for_review'),pr_state='open',draft=False,checks_state='passed')
        self.tracker.observations.extend([result,result]);self.service.refresh_pull_requests()
        self.assertEqual(self.service._get('tasks',request['id'])['status'],'queued')
        self.assertEqual(self.service._get('tasks',self.child_id)['status'],'cancelled')

    def test_registration_replaces_cancelled_source_for_same_bot(self):
        with self.service.transaction():
            task=self.service._get('tasks',self.child_id)
            task['status']='cancelled';self.service._put('tasks',task)
        request=self.write(f'/v1/bots/{self.dev["id"]}/messages',{'text':'Finish PR #42'})
        self.service._run_task(request['id'])
        self.assertEqual(self.service._get('artifacts',self.artifact_id)['_review_task_id'],request['id'])
        self.green()
        self.assertEqual(self.service._get('tasks',request['id'])['status'],'queued')

    def test_recorded_merge_answer_does_not_repeat_initial_review_request(self):
        from coconut.hermes_worker import conversation_input
        prompt,_=conversation_input({'operation':'resume','task':{'title':'Check PR','_pr_review':{'url':URL,'head_sha':'a'*40}},
            'decision':{'action_type':'question','status':'answered','answer':'merge'},'history':[]})
        self.assertIn('using this recorded answer',prompt)
        self.assertNotIn('Do not merge now',prompt)
        self.assertNotIn('offer that decision yourself',prompt)
