from datetime import datetime, timedelta, timezone
import json
import time
import unittest
from unittest.mock import patch

from coconut.native_failure import native_failure
from coconut.service import ServiceError
from coconut.work_sources import NO_CARD_MAX, retry_delay
import test_work_source_awaiting as awaiting
import test_work_source_coverage as fixtures


def later(seconds):
    return patch('coconut.work_sources.time.time', return_value=time.time() + seconds)


class NoCardTests(unittest.TestCase):
    setUp = fixtures.WorkSourceTests.setUp
    audit = fixtures.WorkSourceTests.audit

    def set_status(self, identity, status):
        with self.service.transaction():
            task = self.service._get('tasks', identity)
            task['status'] = status
            self.service._put('tasks', task)

    def no_card(self, **payload):
        task_id = self.audit()['task_id']
        self.set_status(task_id, 'working')
        result = self.service.wait_for_task(task_id, dict(no_card=True, reason='Gumclaw owns this issue.', **payload))
        self.set_status(task_id, 'completed')
        return task_id, result

    def test_no_card_covers_the_item_until_it_changes(self):
        first, result = self.no_card()
        recorded = datetime.fromisoformat(result['next_check_at'])
        self.assertAlmostEqual((recorded - datetime.now(timezone.utc)).total_seconds(), 24 * 3600, delta=60)
        with later(4000):
            state = self.audit()
        self.assertEqual(state['missing'], [])
        self.assertEqual(state['task_id'], first)
        self.assertEqual(state['no_card'][self.item['id']]['reason'], 'Gumclaw owns this issue.')
        # A new comment, label, or assignee changes updated_at, so Coconut audits the item again.
        self.item['updated_at'] = '2026-09-14T13:00:00Z'
        state = self.audit()
        self.assertEqual(state['missing'], [self.item['id']])
        self.assertNotEqual(state['task_id'], first)
        self.assertEqual(state['no_card'], {})

    def test_failed_source_read_keeps_a_no_card_record_saved_during_the_read(self):
        task_id = self.audit()['task_id']
        self.set_status(task_id, 'working')
        def run(*args, **kwargs):
            # The bot records its result while the source command runs, and then the command fails.
            self.service.wait_for_task(task_id, dict(no_card=True, reason='No new question.'))
            raise OSError('source failed')
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['next_check'] = 0
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        with patch('coconut.work_sources.subprocess.run', side_effect=run):
            self.service.audit_work_sources()
        state = self.service._setting('work-source:assigned')
        self.assertEqual(state['error'], 'OSError')
        self.assertIn(self.item['id'], state['no_card'])

    def test_no_card_ends_at_next_check_at_and_is_bounded(self):
        _, result = self.no_card(next_check_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        recorded = datetime.fromisoformat(result['next_check_at'])
        self.assertLessEqual(recorded, datetime.now(timezone.utc) + timedelta(seconds=NO_CARD_MAX))
        record = self.service._setting('work-source:assigned')['no_card'][self.item['id']]
        record['next_check_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.service.transaction():
            state = self.service._setting('work-source:assigned')
            state['no_card'][self.item['id']] = record
            self.service.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('work-source:assigned', json.dumps(state)))
        with later(4000):
            self.assertEqual(self.audit()['missing'], [self.item['id']])

    def test_no_card_with_an_owner_task_covers_while_the_owner_is_live(self):
        owner = self.service._send(self.bot, {'text': 'Owner', 'mode': 'queue'}, record_message=False)
        self.no_card(task_id=owner['id'])
        self.assertEqual(self.audit()['missing'], [])
        self.set_status(owner['id'], 'completed')
        self.assertEqual(self.audit()['missing'], [self.item['id']])

    def test_no_card_needs_a_source_audit_and_valid_values(self):
        task_id = self.audit()['task_id']
        self.set_status(task_id, 'working')
        for payload in (dict(reason='No question.', next_check_at='2020-01-01T00:00:00Z'),
                        dict(reason='No question.', next_check_at='soon'),
                        dict(reason='No question.', task_id='missing-task'),
                        dict(reason='')):
            with self.assertRaises(ServiceError):
                self.service.wait_for_task(task_id, dict(no_card=True, **payload))
        other = self.service._send(self.bot, {'text': 'Plain work', 'mode': 'queue'}, record_message=False)
        self.set_status(other['id'], 'working')
        with self.assertRaises(ServiceError):
            self.service.wait_for_task(other['id'], dict(no_card=True, reason='No question.'))

    def test_awaiting_item_accepts_no_card(self):
        test = awaiting.AwaitingAuditTests('audit')
        test.setUp()
        self.addCleanup(test.doCleanups)
        task_id = test.audit()['task_id']
        prompt = test.service._get('tasks', task_id)['_source_audit_prompt']
        self.assertIn('call coconut_wait with no_card true', prompt)
        # Every audit ends with a card or a no-card record, so a bot cannot leave the item as a silent gap.
        self.assertIn('exactly one result for each assignment', prompt)
        self.assertIn('Do not finish without a card or this record', prompt)
        from coconut.card_tools import QUESTION_SCHEMA
        revisions = QUESTION_SCHEMA['parameters']['properties']['source_revisions']['description']
        self.assertIn('no_card true', revisions)
        self.assertNotIn('continue without creating a card', revisions)
        with test.service.transaction():
            task = test.service._get('tasks', task_id)
            task['status'] = 'working'
            test.service._put('tasks', task)
        test.service.wait_for_task(task_id, dict(no_card=True, reason='No new question.'))
        self.assertEqual(test.audit()['missing'], [])


class AnswerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.test = awaiting.AwaitingAuditTests('audit')
        self.test.setUp()
        self.addCleanup(self.test.doCleanups)
        self.service = self.test.service
        owner = self.service._send(self.test.bot, {'text': 'Ask about the payout', 'mode': 'queue'}, record_message=False)
        self.owner = owner['id']
        with self.service.transaction():
            self.service._put('decisions', dict(
                id='card', task_id=self.owner, bot_id=self.test.bot['id'], status='approved', action_type='question',
                title='Payout', selected_option_label='Option B', decided_at='2026-10-21T08:00:00Z',
                sources=[dict(kind='issue', title='Payout', url=awaiting.URL)]))

    def owner_status(self, status):
        with self.service.transaction():
            task = self.service._get('tasks', self.owner)
            task['status'] = status
            self.service._put('tasks', task)

    def test_answer_covers_while_it_posts_and_after_it_posted(self):
        for status in ('working', 'completed'):
            self.owner_status(status)
            self.assertEqual(self.test.audit()['missing'], [], status)

    def test_answer_that_failed_to_post_is_a_gap_and_the_audit_posts_it(self):
        self.owner_status('failed')
        state = self.test.audit()
        self.assertEqual(state['missing'], [awaiting.URL])
        prompt = self.service._get('tasks', state['task_id'])['_source_audit_prompt']
        self.assertIn('the answer has not reached the source yet', prompt)
        self.assertIn('"answer": "Option B"', prompt)

    def test_failed_answer_status_is_a_gap(self):
        self.owner_status('completed')
        with self.service.transaction():
            card = self.service._get('decisions', 'card')
            card['status'] = 'failed'
            self.service._put('decisions', card)
        self.assertEqual(self.test.audit()['missing'], [awaiting.URL])


class ProviderLimitTests(unittest.TestCase):
    setUp = fixtures.WorkSourceTests.setUp
    audit = fixtures.WorkSourceTests.audit

    def fail(self, task_id, payload):
        self.provider.start_task = lambda task, bot, history, emit: emit('failed', payload)
        self.service._run_task(task_id)

    def test_reset_time_comes_from_the_provider_error_only(self):
        error = 'HTTP 429 {"type": "usage_limit_reached", "resets_in_seconds": 7200, "token": "private-key"}'
        failure = native_failure(dict(failure_reason='rate_limit', error=error))
        self.assertEqual(failure['retry_after'], 7200)
        self.assertNotIn('private-key', json.dumps(failure))
        self.assertNotIn('retry_after', native_failure(dict(failure_reason='rate_limit', error='HTTP 429')))
        self.assertNotIn('retry_after', native_failure(dict(failure_reason='timeout', error=error)))

    def test_rate_limit_holds_audits_until_its_reset(self):
        first = self.audit()['task_id']
        self.fail(first, dict(summary='Limited.', error_code='native_rate_limit', retry_after=7200))
        limit = self.service.provider_limit(self.bot['id'])
        self.assertAlmostEqual(limit['resets_at'], time.time() + 7200, delta=60)
        health = self.service._get('bots', self.bot['id'])['health']
        self.assertEqual((health['state'], health['error_code'], health['uncovered']), ('rate_limited', 'native_rate_limit', 1))
        self.assertTrue(health['until'])
        self.assertIn('health', next(bot for bot in self.service.snapshot()['bots'] if bot['id'] == self.bot['id']))
        # The failed audit would retry after its backoff, but the limit holds it.
        with later(4000):
            self.assertEqual(self.audit()['task_id'], first)
        audit_task = self.service._get('tasks', first)
        self.assertFalse(self.service.automatic_work_held(dict(audit_task, _source_audit_prompt='')))
        self.assertTrue(self.service.automatic_work_held(dict(audit_task, status='queued')))
        self.assertFalse(self.service._dispatch_eligible(dict(audit_task, status='queued'), time.time()))
        with later(8000):
            self.assertNotEqual(self.audit()['task_id'], first)
            self.service.expire_provider_limits()
        self.assertIsNone(self.service._get('bots', self.bot['id'])['health'])

    def test_rate_limit_without_a_reset_time_uses_a_bounded_default(self):
        first = self.audit()['task_id']
        self.fail(first, dict(summary='Limited.', error_code='native_rate_limit'))
        self.assertAlmostEqual(self.service.provider_limit(self.bot['id'])['resets_at'], time.time() + 900, delta=60)
        with self.service.transaction():
            self.service.db.execute("DELETE FROM settings WHERE key LIKE 'provider-limit:%'")
        other = self.service._send(self.bot, {'text': 'Try again', 'mode': 'queue'}, record_message=False)
        self.fail(other['id'], dict(summary='Limited.', error_code='native_rate_limit', retry_after=10 ** 9))
        self.assertLessEqual(self.service.provider_limit(self.bot['id'])['resets_at'], time.time() + 7 * 24 * 3600 + 60)

    def test_provider_error_shows_until_a_turn_completes(self):
        first = self.audit()['task_id']
        self.fail(first, dict(summary='Sign in.', error_code='native_auth_permanent'))
        self.assertIsNone(self.service.provider_limit(self.bot['id']))
        self.assertEqual(self.service._get('bots', self.bot['id'])['health']['state'], 'provider_error')
        other = self.service._send(self.bot, {'text': 'Try again', 'mode': 'queue'}, record_message=False)
        self.provider.start_task = lambda task, bot, history, emit: emit('completed', {'summary': 'Done.'})
        self.service._run_task(other['id'])
        self.assertIsNone(self.service._get('bots', self.bot['id'])['health'])


    def test_only_a_model_turn_that_started_after_the_limit_clears_it(self):
        first = self.audit()['task_id']
        self.fail(first, dict(summary='Limited.', error_code='native_rate_limit'))
        bot_id = self.bot['id']
        started = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        with self.service.transaction():
            # A script routine calls no model.
            self.service.clear_provider_failure(dict(bot_id=bot_id, _turn_started_at=started,
                                                     _native_job=dict(no_agent=True)))
            # A turn that started before the limit says nothing about the provider now.
            self.service.clear_provider_failure(dict(bot_id=bot_id, _turn_started_at='2026-01-01T00:00:00Z'))
        self.assertIsNotNone(self.service.provider_limit(bot_id))
        with self.service.transaction():
            self.service.clear_provider_failure(dict(bot_id=bot_id, _turn_started_at=started))
        self.assertIsNone(self.service.provider_limit(bot_id))
        self.assertIsNone(self.service._get('bots', bot_id)['health'])


class RetryDelayTests(unittest.TestCase):
    def test_repeated_audits_of_an_unchanged_item_back_off_to_one_day(self):
        self.assertEqual([retry_delay(attempts, {}) for attempts in range(1, 11)],
                         [600, 1200, 2400, 4800, 9600, 19200, 38400, 76800, 86400, 86400])
        self.assertEqual(retry_delay(1, dict(interval_seconds=1800)), 1800)
        self.assertEqual(retry_delay(10 ** 6, {}), 86400)


if __name__ == '__main__':
    unittest.main()
