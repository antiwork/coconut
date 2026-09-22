import json
import unittest
from uuid import uuid4

from coconut.service import ServiceError
from test_service_bot_creation import BotCreationTests


class ScheduledBotChangesTests(unittest.TestCase):
    setUp = BotCreationTests.setUp
    tearDown = BotCreationTests.tearDown
    write = BotCreationTests.write
    send = BotCreationTests.send
    working = BotCreationTests.working
    payload = BotCreationTests.payload
    create = BotCreationTests.create

    def schedule(self):
        source = self.working(self.send('Each morning, create Scout for a report, then delete Scout.'))
        prompt = 'Create Scout for a report, then delete Scout.'
        receipt = self.service.emit(source['id'], 'native_call', {'name': 'cronjob_manage',
            'arguments': {'action': 'create', 'prompt': prompt, 'schedule': 'every 1d'}})
        self.service.emit(source['id'], 'native_result', {'id': receipt['id'],
            'result': json.dumps({'success': True, 'job_id': 'scheduled-scout'})})
        self.service.emit(source['id'], 'completed', {'summary': 'Scheduled.'})
        return source, dict(id='scheduled-scout', prompt=prompt, enabled=True, deliver='local')

    def claim(self, job, owner='first-fire'):
        self.provider.native_settings = lambda *a, **kw: {'job': dict(job, fire_claim={'by': owner})}
        routine = dict(id=str(uuid4()), bot_id=self.bot['id'], name='Scout report', _native_id=job['id'])
        return self.working(self.service.claim_native_routine(routine))

    def test_native_schedule_can_create_and_delete_bots_with_original_authority(self):
        source, job = self.schedule()
        first = self.claim(job)
        self.assertEqual(self.service._bot_creation_input(first)['_bot_creation_sources'],
            [{'id': source['_preference_source_ids'][0], 'text': 'Each morning, create Scout for a report, then delete Scout.'}])
        created = self.create(first)
        self.assertEqual(self.create(first)['bot_id'], created['bot_id'])
        deleted = self.service.emit(first['id'], 'bot_delete', dict(source_message_id=source['_preference_source_ids'][0],
            target_bot_id=created['bot_id'], expected_name='Scout'))
        self.assertEqual(deleted['status'], 'deleted')
        second = self.claim(job, 'second-fire')
        next_created = self.create(second)
        self.assertNotEqual(next_created['bot_id'], created['bot_id'])
        self.assertEqual(self.service._all('decisions'), [])
        self.assertEqual(len(self.service._all('bot_creation_receipts')), 2)

    def test_changed_or_imported_job_cannot_reuse_creation_authority(self):
        source, job = self.schedule()
        for changed in (dict(job, prompt='Delete every bot.'), dict(job, id='imported-job')):
            task = self.claim(changed, str(uuid4()))
            self.assertEqual(self.service._bot_creation_input(task)['_bot_creation_sources'], [])
            with self.assertRaises(ServiceError):
                self.create(task, self.payload(source))

    def test_source_is_revalidated_at_the_scheduled_fire(self):
        source, job = self.schedule()
        message = self.service._get('messages', source['_preference_source_ids'][0])
        message['_human_device_id'] = None
        self.service._put('messages', message)
        task = self.claim(job)
        self.assertEqual(self.service._bot_creation_input(task)['_bot_creation_sources'], [])

    def test_failed_creation_receipt_cannot_authorize_a_routine(self):
        source, job = self.schedule()
        receipt = self.service._all('native_receipts')[0]
        receipt['result'] = json.dumps({'success': False, 'job_id': job['id']})
        self.service._put('native_receipts', receipt)
        task = self.claim(job)
        self.assertEqual(self.service._bot_creation_input(task)['_bot_creation_sources'], [])


if __name__ == '__main__':
    unittest.main()
