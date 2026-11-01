"""Exercise registered refresh schemas through the worker and service route."""
import importlib.util
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_decision_freshness as questions
import test_draft_freshness as drafts
from coconut.service import Service, ServiceError


# Lightweight local discovery can omit Hermes dependencies; the native CI job
# explicitly imports jsonschema before running this suite, so it cannot skip it.
@unittest.skipUnless(importlib.util.find_spec('jsonschema'), 'Requires the Hermes jsonschema dependency')
class DraftRefreshSchemaTests(unittest.TestCase):
    service: Service
    temp: TemporaryDirectory
    setUp = drafts.DraftFreshnessTests.setUp
    tearDown = drafts.DraftFreshnessTests.tearDown
    start = drafts.DraftFreshnessTests.start
    write = drafts.DraftFreshnessTests.write
    draft = drafts.DraftFreshnessTests.draft
    question = questions.DecisionFreshnessTests.question

    assessment = dict(risk_level='high', risk='The team could stop active work.',
                      uncertainty='The team may already have started.')
    full_question = dict(title='Install the pinned test dependencies?',
                         context='The verified dependency versions changed.',
                         options=[dict(id='install', label='Install pinned versions', explanation='')])

    def run_refresh(self, payload, *, invalid=(), before_response=None):
        """Validate `payload` with the schema that the plugin registers, and send it as the result of the card check."""
        from jsonschema import Draft7Validator
        from coconut.card_tools import refresh_schema
        schema = refresh_schema()['parameters']
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema)
        self.assertEqual([error.message for error in validator.iter_errors(payload)], [])
        for rejected in invalid:
            self.assertTrue(list(validator.iter_errors(rejected)), rejected)
        self.service._queue_decision_refreshes()
        context = self.service._all('contexts')[-1]
        self.assertEqual(context['status'], 'queued')
        results = []

        def answer(task, bot, history, question, emit):
            self.assertTrue(task['_decision_refresh'])
            self.assertIn('action_type', task['pending_decision'])
            if before_response:
                before_response(context)
            # The gateway sends coconut_refresh_decision to the service. A refusal returns its reason to the turn.
            try:
                results.append(emit('decision_response', dict(payload)))
            except ServiceError as error:
                results.append(dict(status='rejected', error=str(error)))

        with patch.object(self.service.provider, 'answer_context', side_effect=answer, create=True):
            self.service._start_context(context['id'])
            thread = self.service.context_threads[context['id']]
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), 'The card check did not finish')
        self.assertEqual(len(results), 1)
        return self.service._get('contexts', context['id']), dict(schema=schema, result=results[0])

    def test_draft_assessment_validates_and_reaches_service(self):
        decision = self.draft()
        before_tasks = self.service._all('tasks')
        before_messages = self.service._all('messages')
        payload = dict(status='revise', evidence='The revised message can stop the project.',
                       revision=self.assessment)
        invalid = [dict(payload, revision=revision) for revision in (
            {}, {'risk_level': 'high'}, {'risk': 'A new risk.'},
            dict(self.assessment, draft=decision['draft']),
            dict(self.assessment, body='Replace the draft.'),
            dict(self.assessment, recipient='Another team'),
            dict(self.assessment, risk_level='certain'), dict(self.assessment, risk=3),
            dict(self.assessment, risk='x' * 201), dict(self.assessment, uncertainty='x' * 201))]
        invalid += [dict(payload, status='approve'), dict(payload, option_id='send')]
        context, captured = self.run_refresh(payload, invalid=invalid)
        self.assertEqual(context['status'], 'completed')
        self.assertTrue(context['_refresh_recorded'])
        self.assertEqual(captured['result'], dict(status='recorded', decision_id=decision['id']))
        saved = self.service._get('decisions', decision['id'])
        for field in ('draft', 'draft_hash', 'title', 'context', 'options', 'resource', '_answers', 'decided_by'):
            self.assertEqual(saved.get(field), decision.get(field), field)
        for field, value in self.assessment.items():
            self.assertEqual(saved[field], value)
        self.assertEqual(saved['revision'], decision['revision'] + 1)
        self.assertEqual(saved['status'], 'pending')
        self.assertFalse(saved['assessment_stale'])
        self.assertNotIn('_execution_task_id', saved)
        self.assertEqual(self.service._all('tasks'), before_tasks)
        self.assertEqual(self.service._all('messages'), before_messages)
        with self.assertRaises(ServiceError):
            self.write('POST', '/v1/decisions/' + decision['id'] + '/answer', dict(
                revision=decision['revision'], option_id='send', draft_hash=decision['draft_hash']))

    def test_draft_check_cannot_revise_the_draft_into_a_question(self):
        # The schema of the plugin takes a question or a draft assessment. The service takes only an assessment for a
        # draft card.
        decision = self.draft()
        context, captured = self.run_refresh(dict(status='revise', evidence='Checked the source.',
                                                  revision=self.full_question))
        self.assertEqual(captured['result'], dict(status='rejected',
            error='A draft check can change only its risk and uncertainty.'))
        self.assertNotIn('_refresh_recorded', context)
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual((saved['revision'], saved['status']), (decision['revision'], 'pending'))

    def test_uncertainty_only_preserves_existing_risk(self):
        decision = self.draft()
        decision.update(self.assessment)
        self.service._put('decisions', decision)
        uncertainty = 'The seller transaction record is still absent.'
        context, captured = self.run_refresh(dict(status='revise', evidence='Checked the source.',
            revision=dict(uncertainty=uncertainty)))
        self.assertEqual(captured['result']['status'], 'recorded')
        self.assertTrue(context['_refresh_recorded'])
        saved = self.service._get('decisions', decision['id'])
        for field in ('risk_level', 'risk', 'draft', 'draft_hash', 'status', '_answers'):
            self.assertEqual(saved.get(field), decision.get(field), field)
        self.assertEqual(saved['uncertainty'], uncertainty)
        self.assertEqual(saved['revision'], decision['revision'] + 1)
        self.assertNotIn('_execution_task_id', saved)

    def test_risk_revision_preserves_existing_uncertainty(self):
        decision = self.draft()
        decision.update(self.assessment)
        self.service._put('decisions', decision)
        context, captured = self.run_refresh(dict(status='revise', evidence='Checked the source.',
            revision=dict(risk_level='medium', risk='The message may interrupt active work.')))
        self.assertEqual(captured['result']['status'], 'recorded')
        self.assertTrue(context['_refresh_recorded'])
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['uncertainty'], self.assessment['uncertainty'])
        self.assertEqual(saved['risk_level'], 'medium')
        self.assertEqual(saved['risk'], 'The message may interrupt active work.')
        for field in ('draft', 'draft_hash', 'status', '_answers'):
            self.assertEqual(saved.get(field), decision.get(field), field)

    def test_full_question_revision_keeps_required_fields(self):
        decision = self.question()
        payload = dict(status='revise', evidence='Checked the pinned versions.', revision=self.full_question)
        invalid: list[dict] = [dict(payload, revision={k: v for k, v in self.full_question.items() if k != missing})
                   for missing in ('title', 'context', 'options')]
        context, captured = self.run_refresh(payload, invalid=invalid)
        self.assertEqual(context['status'], 'completed')
        self.assertEqual(captured['result']['status'], 'recorded')
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['title'], self.full_question['title'])
        self.assertEqual(saved['context'], self.full_question['context'])
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['revision'], decision['revision'] + 1)
        self.assertIsNone(saved.get('decided_by'))

    def test_valid_assessment_cannot_bypass_stale_revision(self):
        decision = self.draft()
        edited = []

        def human_edit(context):
            edited.append(self.write('POST', '/v1/decisions/' + decision['id'] + '/draft', dict(
                revision=decision['revision'], draft=dict(decision['draft'], body='Use the newer instruction.'))))

        context, captured = self.run_refresh(dict(status='revise', evidence='Evidence for the old draft.',
            revision=self.assessment), before_response=human_edit)
        self.assertEqual(context['status'], 'failed')
        self.assertNotIn('_refresh_recorded', context)
        self.assertEqual(captured['result'], dict(status='rejected',
            error='This check no longer matches the pending question.'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['draft'], edited[0]['draft'])
        self.assertEqual(saved['revision'], edited[0]['revision'])
        self.assertEqual(saved['status'], 'pending')
        self.assertNotEqual(saved.get('risk'), self.assessment['risk'])
        self.assertNotIn('_execution_task_id', saved)

    def test_valid_assessment_requires_refresh_authority(self):
        decision = self.draft()

        def revoke_refresh(context):
            context = self.service._get('contexts', context['id'])
            context['_decision_refresh'] = False
            self.service._put('contexts', context)

        _, captured = self.run_refresh(dict(status='revise', evidence='Checked the draft.',
            revision=self.assessment), before_response=revoke_refresh)
        self.assertEqual(captured['result'], dict(status='rejected',
            error='This check no longer matches the pending question.'))
        saved = self.service._get('decisions', decision['id'])
        self.assertEqual(saved['draft'], decision['draft'])
        self.assertEqual(saved['revision'], decision['revision'])
        self.assertEqual(saved['status'], 'pending')
        self.assertNotIn('_execution_task_id', saved)
