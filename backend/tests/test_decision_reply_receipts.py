import os
import unittest
from unittest.mock import patch

import test_provider as provider_tests
from coconut.service import ServiceError


class DecisionReplyReceiptTests(unittest.TestCase):
    setUp = provider_tests.ProviderTests.setUp
    tearDown = provider_tests.ProviderTests.tearDown
    fake_hermes = provider_tests.ProviderTests.fake_hermes

    def test_rejected_revision_can_be_corrected_without_an_uncertain_retry(self):
        self.task['_decision_reply'] = True
        provider = self.fake_hermes("""
            import json
            payload = {'choice': 'modify', 'quote': 'Revise the card.',
                'revision': {'title': 'Continue?', 'options': [{'id': 'wait', 'label': 'Wait'}], 'recommended_option_id': ''}}
            result = json.loads(registry.dispatch('coconut_resolve_reply', payload))
            assert result == {'status': 'rejected', 'error': 'Recommend a current choice.'}, result
            del payload['revision']['recommended_option_id']
            result = json.loads(registry.dispatch('coconut_resolve_reply', payload))
            assert result['status'] == 'recorded', result
            return {'completed': True, 'final_response': 'Card updated.'}
        """)
        attempts = []
        def emit(kind, data):
            if kind == 'decision_response':
                attempts.append(data)
                if 'recommended_option_id' in data['revision']:
                    raise ServiceError(400, 'Recommend a current choice.')
                return {'status': 'recorded'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}):
            provider.answer_context(self.task, self.bot, [], 'Revise the card.', emit)
        self.assertEqual(len(attempts), 2)
