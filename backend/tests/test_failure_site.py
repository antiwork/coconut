import tempfile
import unittest
from uuid import uuid4

from coconut.hermes_worker import exception_site
from coconut.service import Service
from test_service import ControlledProvider


def prepare_with_a_secret():
    raise ConnectionError('POST https://api.example.test/v1 body={"api_key": "sk-secret-value"} failed')


class FailureSiteTests(unittest.TestCase):
    def test_the_site_names_the_class_and_the_last_frame_and_never_the_message(self):
        try:
            prepare_with_a_secret()
        except Exception as error:
            detail = exception_site(error)
        self.assertEqual(detail, 'ConnectionError at test_failure_site.py:%d in prepare_with_a_secret' % (prepare_with_a_secret.__code__.co_firstlineno + 1))
        self.assertNotIn('sk-secret-value', detail)
        self.assertNotIn('api.example.test', detail)

    def test_an_exception_without_a_traceback_has_an_unknown_place(self):
        self.assertEqual(exception_site(ValueError('x')), 'ValueError at unknown place')

    def test_the_service_keeps_the_detail_on_the_failed_task_and_out_of_the_public_view(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory, ControlledProvider(), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Work', 'mode': 'queue'}, str(uuid4()))
            with service.transaction():
                saved = service._get('tasks', task['id'])
                saved['status'] = 'working'
                service._put('tasks', saved)
            service.emit(task['id'], 'failed', {'summary': 'Coconut could not prepare this conversation.',
                                                'error_code': 'worker_preparation_failed',
                                                'error_detail': 'OperationalError at hermes_state.py:412 in connect'})
            stored = service._get('tasks', task['id'])
            self.assertEqual((stored['status'], stored['error_code']), ('failed', 'worker_preparation_failed'))
            self.assertEqual(stored['_error_detail'], 'OperationalError at hermes_state.py:412 in connect')
            self.assertNotIn('_error_detail', service._public(stored))

    def test_a_very_long_detail_is_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory, ControlledProvider(), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Work', 'mode': 'queue'}, str(uuid4()))
            with service.transaction():
                saved = service._get('tasks', task['id'])
                saved['status'] = 'working'
                service._put('tasks', saved)
            service.emit(task['id'], 'failed', {'summary': 'Failed.', 'error_detail': 'x' * 900})
            self.assertEqual(len(service._get('tasks', task['id'])['_error_detail']), 200)

    def test_a_later_failure_or_a_new_dispatch_does_not_keep_an_earlier_place(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory, ControlledProvider(), start_workers=False)
            self.addCleanup(service.close)
            bot = service.mutate('POST', '/v1/bots', {'name': 'Desk', 'role': ''}, str(uuid4()))
            task = service.mutate('POST', f"/v1/bots/{bot['id']}/messages", {'text': 'Work', 'mode': 'queue'}, str(uuid4()))
            def working():
                with service.transaction():
                    saved = service._get('tasks', task['id'])
                    saved['status'] = 'working'
                    service._put('tasks', saved)
            working()
            service.emit(task['id'], 'failed', {'summary': 'Failed.', 'error_code': 'worker_preparation_failed',
                                                'error_detail': 'OperationalError at hermes_state.py:412 in connect'})
            self.assertIn('_error_detail', service._get('tasks', task['id']))
            # The next failure has no detail of its own, so the old place must go.
            working()
            service.emit(task['id'], 'failed', {'summary': 'Failed again.', 'error_code': 'native_turn_failed'})
            self.assertNotIn('_error_detail', service._get('tasks', task['id']))
            # A new detail replaces the old one.
            working()
            service.emit(task['id'], 'failed', {'summary': 'Failed.', 'error_detail': 'KeyError at a.py:1 in b'})
            self.assertEqual(service._get('tasks', task['id'])['_error_detail'], 'KeyError at a.py:1 in b')
            # A task that runs again starts without the detail.
            with service.transaction():
                saved = service._get('tasks', task['id'])
                saved['status'] = 'queued'
                service._put('tasks', saved)
            service._dispatch_pending()
            self.assertNotIn('_error_detail', service._get('tasks', task['id']))


if __name__ == '__main__':
    unittest.main()
