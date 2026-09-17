import unittest
from coconut.service import YieldForDecision

import test_service_team


class ExchangeTranscriptTests(unittest.TestCase):
    setUp = test_service_team.TeamServiceTests.setUp
    tearDown = test_service_team.TeamServiceTests.tearDown
    write = test_service_team.TeamServiceTests.write
    send = test_service_team.TeamServiceTests.send
    working = test_service_team.TeamServiceTests.working
    deliver = test_service_team.TeamServiceTests.deliver
    restart = test_service_team.TeamServiceTests.restart
    test_mentions_address_only_selected_bots_once = test_service_team.TeamServiceTests.test_mentions_address_only_selected_bots_once
    def test_card_text_reply_stays_visible_when_assignment_returns_to_its_bot(self):
        root = self.working(self.send(self.a))
        child = self.working({'id': self.deliver(root, self.b)['task_id']})
        with self.assertRaises(YieldForDecision):
            self.service.emit(child['id'], 'native_question', dict(title='Publish the patch?',
                options=[dict(id='publish', label='Publish'), dict(id='pause', label='Pause')]))
        decision = self.service._all('decisions')[-1]
        with self.service.transaction():
            withdrawn = self.service._message(self.b['id'], 'The patch corrects tax calculations.', task_id=child['id'])
            withdrawn['_hidden'] = True
            self.service._put('messages', withdrawn)
        self.write('POST', f"/v1/decisions/{decision['id']}/answer",
                   dict(choice='reply', correction='Explain this patch.', revision=1))
        def reply(task, bot, history, decision, emit):
            self.assertEqual(decision['status'], 'replied')
            emit('text_delta', dict(text='The patch corrects tax calculations.'))
            emit('completed', dict(summary='Complete.'))
        self.provider.resume_task = reply
        self.service._run_task(child['id'])
        self.service._drain_returns()
        for restart in (False, True):
            if restart:
                self.restart()
            answers = [m for m in self.service.snapshot()['messages']
                       if m['text'] == 'The patch corrects tax calculations.' and m['kind'] == 'text']
            self.assertEqual(len(answers), 1)
            self.assertEqual(answers[0]['bot_id'], self.b['id'])
            self.assertFalse(answers[0].get('exchange_recipient_bot_ids'))
            self.assertFalse(answers[0]['recipient_bot_ids'])
            self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'replied')
            returned = [m for m in self.service.snapshot()['messages']
                        if m['text'] == 'The patch corrects tax calculations.' and m['kind'] == 'bot_exchange']
            self.assertEqual(len(returned), 1)
            self.assertEqual(returned[0]['recipient_bot_ids'], [self.a['id']])
        deliveries = [d for d in self.service._all('deliveries') if d['source_task_id'] == child['id']]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]['target_bot_id'], self.a['id'])
        with self.service.transaction():
            stored = self.service._get('tasks', child['id'])
            self.service._advance_input(stored)
            self.service._put('tasks', stored)
            update = self.service._message(self.b['id'], 'The remaining work is complete.', task_id=child['id'])
        self.assertEqual(update['exchange_recipient_bot_ids'], [self.a['id']])

    def test_text_reply_to_a_delegated_proposal_uses_the_card_bot(self):
        root = self.working(self.send(self.a))
        child = self.working({'id': self.deliver(root, self.b)['task_id']})
        self.service.emit(child['id'], 'native_proposal', dict(title='Inspect the patch?',
            key='inspect', options=[dict(id='inspect', label='Inspect')]))
        decision = self.service._all('decisions')[-1]
        result = self.write('POST', f"/v1/decisions/{decision['id']}/answer",
                            dict(choice='reply', correction='Explain the patch.', revision=1))
        task_id = self.service._get('decisions', result['id'])['_execution_task_id']
        def reply(task, bot, history, decision, emit):
            emit('text_delta', dict(text='The patch corrects tax calculations.'))
            emit('completed', dict(summary='Complete.'))
        self.provider.resume_task = reply
        self.service._run_task(task_id)
        with self.service.transaction():
            update = self.service._message(self.b['id'], 'Continuing the original task.', task_id=child['id'])
        self.assertEqual(update['exchange_recipient_bot_ids'], [self.a['id']])
        answer = next(m for m in self.service.snapshot()['messages'] if m['text'] == 'The patch corrects tax calculations.')
        self.assertEqual(answer['bot_id'], self.b['id'])
        self.assertFalse(answer.get('exchange_recipient_bot_ids'))
        self.restart()
        self.assertEqual(self.service._get('messages', answer['id'])['bot_id'], self.b['id'])
        self.assertEqual(self.service._get('messages', update['id'])['exchange_recipient_bot_ids'], [self.a['id']])

    def test_restart_preserves_a_repaired_reply_without_reclassifying_later_updates(self):
        root = self.working(self.send(self.a))
        child = self.working({'id': self.deliver(root, self.b)['task_id']})
        with self.service.transaction():
            source = self.service._message(self.b['id'], 'Explain this.', task_id=child['id'],
                                           role='user', decision_id='old-card')
            source['_human_device_id'] = 'test-phone'
            self.service._put('messages', source)
            withdrawn = self.service._message(self.b['id'], 'Unfinished answer', task_id=child['id'])
            withdrawn['_hidden'] = True
            self.service._put('messages', withdrawn)
            self.service._message(self.b['id'], 'Saved report', task_id=child['id'], kind='artifact', artifact_id='report')
            answer = self.service._message(self.b['id'], 'This corrects tax calculations.', task_id=child['id'])
            update = self.service._message(self.b['id'], 'The remaining tests need access.', task_id=child['id'])
        self.assertTrue(answer.get('exchange_recipient_bot_ids'))
        with self.service.transaction():
            answer['_direct_reply_message_id'] = source['id']
            self.service._put('messages', answer)
        self.restart()
        restored = self.service._get('messages', answer['id'])
        self.assertFalse(restored.get('exchange_recipient_bot_ids'))
        self.assertEqual(self.service._get('messages', update['id'])['exchange_recipient_bot_ids'], [self.a['id']])
        self.restart()
        self.assertEqual(self.service._get('messages', answer['id']), restored)

    def test_context_answer_keeps_its_audience_when_the_same_result_returns(self):
        root = self.working(self.send(self.a))
        child = self.working({'id': self.deliver(root, self.b)['task_id']})
        with self.assertRaises(YieldForDecision):
            self.service.emit(child['id'], 'native_question', dict(title='Inspect the patch?',
                options=[dict(id='inspect', label='Inspect')]))
        decision = self.service._all('decisions')[-1]
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Explain this.', revision=1))
        context = self.service._all('contexts')[-1]
        self.provider.answer_context = lambda task, bot, history, question, emit: emit('completed', dict(summary='The patch corrects tax calculations.'))
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        answer = next(m for m in self.service.snapshot()['messages'] if m['text']=='The patch corrects tax calculations.')
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(option_id='inspect', revision=1))
        self.provider.resume_task = lambda task, bot, history, decision, emit: emit('completed', dict(summary='Complete.'))
        self.service._run_task(child['id'])
        self.service._drain_returns()
        self.restart()
        restored = self.service._get('messages', answer['id'])
        self.assertEqual(restored['bot_id'], self.b['id'])
        self.assertFalse(restored.get('exchange_recipient_bot_ids'))

    def test_text_reply_to_a_legacy_recovery_card_keeps_its_recipient(self):
        root = self.working(self.send(self.a))
        child = self.working({'id': self.deliver(root, self.b)['task_id']})
        item = self.service._register_work(child['id'], dict(key='report', title='Check the report',
            status='completed', activity='Report checked', completion_evidence='Saved report verified.'))
        with self.service.transaction():
            task = self.service._get('tasks', child['id'])
            task.update(status='failed', _self_recovery=dict(unknown_tools=[]))
            self.service._put('tasks', task)
        with self.assertRaises(YieldForDecision):
            self.service.native_event(child['id'], 'native_question', dict(title='Retry recovery?',
                options=[dict(id='retry', label='Retry'), dict(id='stop', label='Stop')]), recovery_work_ids=[item['id']])
        decision = self.service._all('decisions')[-1]
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(choice='reply', correction='Explain this.', revision=1))
        def reply(task, bot, history, decision, emit):
            emit('text_delta', dict(text='The report is already complete.'))
            emit('completed', dict(summary='Complete.'))
        self.provider.resume_task = reply
        self.service._run_task(child['id'])
        answer = next(m for m in self.service.snapshot()['messages'] if m['text']=='The report is already complete.')
        self.assertEqual(answer['bot_id'], self.b['id'])
        self.assertFalse(answer.get('exchange_recipient_bot_ids'))

    def test_card_replies_to_a_mentioned_bot_keep_the_original_conversation(self):
        child = self.working(self.send(self.a, recipient_bot_ids=[self.b['id']]))
        with self.assertRaises(YieldForDecision):
            self.service.emit(child['id'], 'native_question', dict(title='Inspect the patch?',
                options=[dict(id='inspect', label='Inspect')]))
        decision = self.service._all('decisions')[-1]
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text='Explain this.', revision=1))
        context = self.service._all('contexts')[-1]
        self.provider.answer_context = lambda task, bot, history, question, emit: emit('completed', dict(summary='This is the tax patch.'))
        self.service._start_context(context['id'])
        self.service.context_threads[context['id']].join(timeout=5)
        self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(choice='reply', correction='Explain more.', revision=1))
        def reply(task, bot, history, decision, emit):
            emit('text_delta', dict(text='It corrects tax calculations.'))
            emit('completed', dict(summary='Complete.'))
        self.provider.resume_task = reply
        self.service._run_task(child['id'])
        self.restart()
        answers = [m for m in self.service.snapshot()['messages'] if m['text'] in ('This is the tax patch.', 'It corrects tax calculations.')]
        self.assertEqual(len(answers), 2)
        self.assertTrue(all(m['bot_id']==self.a['id'] and not m.get('exchange_recipient_bot_ids') for m in answers))

    def test_delegated_reply_is_an_exchange_before_the_first_stream_event(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        with self.service.transaction():
            reply = self.service._message(self.b['id'], '', task_id=delivery['task_id'], streaming=True)
        self.assertEqual(reply.get('exchange_recipient_bot_ids'), [self.a['id']])
        self.assertEqual(reply['sender_bot_id'], self.b['id'])
        with self.service.transaction():
            stored = self.service._get('messages', reply['id'])
            stored.pop('exchange_recipient_bot_ids')
            self.service._put('messages', stored)
        self.restart()
        restored = next(m for m in self.service.snapshot()['messages'] if m['id'] == reply['id'])
        self.assertEqual(restored['exchange_recipient_bot_ids'], [self.a['id']])

    def test_decision_context_stays_in_the_bot_conversation(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        with self.service.transaction():
            reply = self.service._message(self.b['id'], 'This needs your choice.',
                task_id=delivery['task_id'], decision_id='pending-decision')
        self.assertNotIn('exchange_recipient_bot_ids', reply)
        self.restart()
        restored = next(m for m in self.service.snapshot()['messages'] if m['id'] == reply['id'])
        self.assertNotIn('exchange_recipient_bot_ids', restored)

    def test_return_to_origin_keeps_its_user_summary_as_a_normal_message(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        child = self.working({'id': delivery['task_id']})
        returned = self.service._deliver_bot_message(child['id'],
            {'target_bot_id': self.a['id'], 'text': 'Report checked'}, returning=True)
        with self.service.transaction():
            reply = self.service._message(self.a['id'], 'The report is ready.', task_id=returned['task_id'])
        self.assertNotIn('exchange_recipient_bot_ids', reply)
        self.assertEqual(reply['kind'], 'text')

    def test_worker_updates_stay_with_worker_and_exchange_has_both_directions(self):
        root = self.working(self.send(self.a))
        delivery = self.deliver(root, self.b, 'Check the report')
        self.service._run_task(delivery['task_id'])
        self.service._drain_returns()
        messages = self.service.snapshot()['messages']
        updates = [m for m in messages if m['task_id'] == delivery['task_id'] and m['kind'] == 'text']
        self.assertTrue(updates)
        self.assertTrue(all(m['bot_id'] == self.b['id'] for m in updates))
        exchange = [m for m in messages if m['kind'] == 'bot_exchange' or m.get('exchange_recipient_bot_ids')]
        self.assertEqual(len(exchange), 2)
        self.assertEqual(exchange[0]['sender_bot_id'], self.a['id'])
        self.assertEqual(exchange[1]['sender_bot_id'], self.b['id'])
        self.assertEqual(exchange[1]['exchange_recipient_bot_ids'], [self.a['id']])
        identities = [m['id'] for m in exchange]
        self.restart()
        self.service._drain_returns()
        self.assertEqual([m['id'] for m in self.service.snapshot()['messages'] if m['kind'] == 'bot_exchange' or m.get('exchange_recipient_bot_ids')], identities)


if __name__ == '__main__':
    unittest.main()
