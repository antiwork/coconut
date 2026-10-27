"""The correction tool reaches the service through the real worker registration and the provider receipt route."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_provider as workers
import test_withdrawal_correction as corrections
from coconut.providers import HermesProvider

setUpModule, tearDownModule = corrections.setUpModule, corrections.tearDownModule

NAME = 'coconut_correct_withdrawal'
BEHAVIOR = '''
    from pathlib import Path
    from native_turn_scope import current
    scope = current.get()
    names = sorted(name for name in registry.handlers if name.startswith('coconut_'))
    record = dict(names=names, results=[], schema_checked=False)
    if NAME in registry.handlers:
        schema = scope.schemas[NAME]
        record['schema'] = schema
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            Draft7Validator = None
        if Draft7Validator is not None:
            Draft7Validator.check_schema(schema['parameters'])
            validator = Draft7Validator(schema['parameters'])
            record['valid'] = [not list(validator.iter_errors(p)) for p in PAYLOADS]
            record['invalid'] = [not list(validator.iter_errors(p)) for p in INVALID]
            record['schema_checked'] = True
        for payload in PAYLOADS:
            record['results'].append(json.loads(registry.dispatch(NAME, payload)))
    Path(CAPTURE).write_text(json.dumps(record))
    return {'completed': True, 'final_response': 'Checked the withdrawn card.'}
'''


class WithdrawalCorrectionRouteTests(corrections.Fixture, unittest.TestCase):
    fake_hermes = workers.ProviderTests.fake_hermes

    def run_turn(self, task, payloads, invalid=()):
        """Run one ordinary turn of `task` in a real worker process. Return what the worker saw and received."""
        self.root = Path(self.temp.name) / 'hermes'
        self.root.mkdir(exist_ok=True)
        capture = self.root / f'{task["id"]}.json'
        fixture = self.fake_hermes(BEHAVIOR.replace('NAME', repr(NAME)).replace('PAYLOADS', repr(list(payloads)))
                                   .replace('INVALID', repr(list(invalid))).replace('CAPTURE', repr(str(capture))))
        self.service.provider = HermesProvider(fixture.data_dir, python=fixture.python,
                                               hermes_root=fixture.hermes_root, catalog=fixture.catalog)
        from profile_layout import LayoutRows
        self.service.provider.profile_rows = LayoutRows(fixture.data_dir)
        self.addCleanup(self.service.provider.close)
        self.set_task(task['id'], status='queued')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            self.service._run_task(task['id'])
        finished = self.service._get('tasks', task['id'])
        self.assertTrue(capture.exists(), (finished['status'], finished.get('summary'), finished.get('_error_detail')))
        return json.loads(capture.read_text()), finished

    def test_delegate_turn_restores_the_card_through_the_worker_receipt(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        helper = self.delegate(running=False)
        valid = self.payload(withdrawn, context)
        stale = dict(valid, expected_revision=1)
        invalid = [dict(valid, decided_by='test-phone'), dict(valid, option_id='send'),
                   {k: v for k, v in valid.items() if k != 'withdrawal_context_id'},
                   dict(valid, expected_revision='2'), dict(valid, reason='x' * 1001)]
        tasks_before = {t['id'] for t in self.service._all('tasks')}
        seen, finished = self.run_turn(helper, [stale, valid, valid], invalid)
        self.assertIn(NAME, seen['names'])
        self.assertEqual(seen['schema']['name'], NAME)
        self.assertEqual(seen['schema']['parameters']['required'],
                         ['decision_id', 'expected_revision', 'withdrawal_context_id', 'reason'])
        self.assertFalse(seen['schema']['parameters']['additionalProperties'])
        rejected, restored, duplicate = seen['results']
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIn('changed', rejected['error'])
        self.assertEqual((restored['status'], restored['decision_id'], restored['revision']), ('restored', decision['id'], 3))
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertEqual(duplicate['correction_id'], restored['correction_id'])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision'], saved['draft'], saved['draft_hash']),
                         ('pending', 3, decision['draft'], decision['draft_hash']))
        self.assertIsNone(saved['decided_by'])
        self.assertEqual(saved['_answers'], [])
        correction, = saved['_withdrawal_corrections']
        self.assertEqual((correction['caller_task_id'], correction['authority']), (helper['id'], 'delegate'))
        # The service assigned the turn when the worker started; the tool arguments did not name it.
        self.assertIsInstance(correction['caller_turn_id'], str)
        self.assertIsNone(helper.get('_native_turn_id'))
        self.assertNotEqual(finished['status'], 'failed', finished.get('summary'))
        self.assertIsNone(finished.get('error_code'))
        # The turn created no task and no decision.
        self.assertEqual({t['id'] for t in self.service._all('tasks')}, tasks_before)
        self.assertEqual(len([d for d in self.service._all('decisions') if d['id'] != decision['id']]), 1)
        if seen['schema_checked']:
            self.assertEqual(seen['valid'], [True, True, True])
            self.assertEqual(seen['invalid'], [False] * len(invalid))

    def test_other_bots_do_not_see_the_tool(self):
        decision = self.draft()
        self.withdraw(decision)
        stranger = self.write('POST', '/v1/bots', {'name': 'Care', 'role': 'Maintenance'})
        stray = self.write('POST', f'/v1/bots/{stranger["id"]}/messages', {'text': 'Repair Desk cards', 'mode': 'queue'})
        seen, _ = self.run_turn(self.service._get('tasks', stray['id']), [])
        self.assertNotIn(NAME, seen['names'])
        self.assertIn('coconut_restart_service', seen['names'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'resolved')

    def test_a_pull_request_behind_an_issue_link_is_rejected_through_the_worker(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        # GitHub reports a pull request for the linked /issues/3730 record, with unchanged issue facts.
        self.issues.pull_request = True
        seen, _ = self.run_turn(self.delegate(running=False), [self.payload(withdrawn, context)])
        result, = seen['results']
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('is a pull request', result['error'])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['status'], saved['revision']), ('resolved', 2))
        self.assertNotIn('_withdrawal_corrections', saved)

    def test_without_a_withdrawal_the_tool_is_absent(self):
        self.draft()
        seen, _ = self.run_turn(self.delegate(running=False), [])
        self.assertNotIn(NAME, seen['names'])


@unittest.skipUnless(importlib.util.find_spec('jsonschema'), 'Requires the Hermes jsonschema dependency')
class WithdrawalCorrectionSchemaTests(WithdrawalCorrectionRouteTests):
    """The native CI job imports jsonschema first, so this registered-schema check cannot skip there."""

    def test_registered_schema_validates_in_the_worker(self):
        decision = self.draft()
        withdrawn, context, _ = self.withdraw(decision)
        valid = self.payload(withdrawn, context)
        seen, _ = self.run_turn(self.delegate(running=False), [valid], [dict(valid, decided_by='test-phone'), {}])
        self.assertTrue(seen['schema_checked'])
        self.assertEqual((seen['valid'], seen['invalid']), ([True], [False, False]))
        self.assertEqual(seen['results'][0]['status'], 'restored')

    test_other_bots_do_not_see_the_tool = None
    test_a_pull_request_behind_an_issue_link_is_rejected_through_the_worker = None
    test_without_a_withdrawal_the_tool_is_absent = None
    test_delegate_turn_restores_the_card_through_the_worker_receipt = None


if __name__ == '__main__':
    unittest.main()
