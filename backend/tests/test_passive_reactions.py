"""Emoji feedback must not create work or consume a model turn by itself."""
import unittest
import test_message_interactions as interactions
from turn_text import turn_input
from coconut.service import YieldForDecision


class PassiveReactionTests(unittest.TestCase):
    setUp = interactions.MessageInteractionTests.setUp
    tearDown = interactions.MessageInteractionTests.tearDown
    write = interactions.MessageInteractionTests.write
    send = interactions.MessageInteractionTests.send
    react = interactions.MessageInteractionTests.react

    def test_heart_after_completion_does_not_start_another_turn(self):
        tasks = self.service.snapshot()['tasks']
        starts = len(self.provider.starts)
        self.react(emoji='❤️')
        self.assertEqual(self.service.snapshot()['tasks'], tasks)
        self.assertEqual(len(self.provider.starts), starts)
        self.assertEqual(self.service._get('bots', self.bot['id'])['status'], 'idle')

    def test_next_request_gets_feedback_once_with_its_original_context(self):
        self.react(emoji='❤️')
        next_task = self.send('Continue with the other report.')
        self.service._run_task(next_task['id'])
        task, bot, history = self.provider.starts[-1]
        prompt, _ = turn_input(dict(operation='start', task=task, bot=bot, history=history))
        self.assertIn('❤️', prompt)
        self.assertIn(self.original['text'], prompt)
        self.assertIn('Continue with the other report.', prompt)
        self.assertIn('Do not send an acknowledgement', prompt)
        last_task = self.send('What is next?')
        self.service._run_task(last_task['id'])
        self.assertFalse(self.provider.starts[-1][0].get('_reaction_feedback'))

    def test_add_remove_before_next_turn_coalesces_to_current_reactions(self):
        self.react(emoji='❤️')
        self.react(False, emoji='❤️')
        task = self.send('Continue.')
        self.service._run_task(task['id'])
        feedback = self.provider.starts[-1][0]['_reaction_feedback']
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0]['reactions'], [])

    def test_feedback_during_turn_cannot_force_a_continuation(self):
        task = self.send('Continue work')
        stored = self.service._get('tasks', task['id'])
        stored['status'] = 'working'
        self.service._put('tasks', stored)
        self.react(emoji='❤️')
        updated = self.service._get('tasks', task['id'])
        self.assertFalse(updated.get('_steering'))
        self.assertEqual(updated['status'], 'working')

    def test_feedback_survives_restart(self):
        self.react(emoji='❤️')
        self.service.close()
        self.service = interactions.Service(self.temp.name, self.provider, start_workers=False)
        task = self.send('Continue after restart.')
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.starts[-1][0]['_reaction_feedback'][0]['reactions'], ['❤️'])

    def test_failed_provider_keeps_feedback_for_next_request(self):
        self.react(emoji='❤️')
        start = self.provider.start_task
        def fail(*args):
            raise RuntimeError('Provider unavailable')
        self.provider.start_task = fail
        task = self.send('Continue.')
        self.service._run_task(task['id'])
        self.assertTrue(self.service._get('bots', self.bot['id'])['_pending_reactions'])
        self.provider.start_task = start
        task = self.send('Try again.')
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.starts[-1][0]['_reaction_feedback'][0]['reactions'], ['❤️'])

    def test_feedback_changed_during_turn_survives_earlier_acknowledgement(self):
        self.react(emoji='❤️')
        start = self.provider.start_task
        def change(task, bot, history, emit):
            self.react(False, emoji='❤️')
            start(task, bot, history, emit)
        self.provider.start_task = change
        task = self.send('Continue.')
        self.service._run_task(task['id'])
        pending = self.service._get('bots', self.bot['id'])['_pending_reactions']
        self.assertEqual(pending[self.original['id']]['reactions'], [])
        self.provider.start_task = start
        task = self.send('Continue again.')
        self.service._run_task(task['id'])
        self.assertEqual(self.provider.starts[-1][0]['_reaction_feedback'][0]['reactions'], [])

    def test_passive_events_stay_out_of_native_history_and_prompt(self):
        self.react(emoji='❤️')
        task = self.send('Continue.')
        self.service._run_task(task['id'])
        task, bot, history = self.provider.starts[-1]
        request = dict(operation='start', task=task, bot=bot, history=history)
        self.assertNotIn('Reaction added:', interactions.turn_input(request)[0])
        self.assertEqual(interactions.gateway_turn_own(request), 'Continue.')

    def test_decision_resume_uses_feedback_changed_while_waiting(self):
        for initial in (False, True):
            with self.subTest(initial_reaction=initial):
                if initial:
                    self.react(emoji='❤️')
                def ask(task, bot, history, emit):
                    emit('native_question', dict(title='Which report?', options=[dict(id='short', label='Short'), dict(id='long', label='Long')]))
                self.provider.start_task = ask
                task = self.send('Prepare another report.')
                self.service._run_task(task['id'])
                decision = self.service.snapshot()['decisions'][-1]
                self.react(not initial, emoji='❤️')
                self.write(f'/v1/decisions/{decision["id"]}/answer', dict(option_id='short', revision=decision['revision']))
                resumed = []
                def resume(task, bot, history, decision, emit):
                    resumed.extend(task['_reaction_feedback'])
                    emit('completed', dict(summary='Done.'))
                self.provider.resume_task = resume
                self.service._run_task(task['id'])
                self.assertEqual(len(resumed), 1)
                self.assertEqual(resumed[0]['reactions'], [] if initial else ['❤️'])
                self.react(False, emoji='❤️')

    def test_shutdown_does_not_acknowledge_interrupted_feedback(self):
        for output_after_shutdown in (False, True):
            with self.subTest(output_after_shutdown=output_after_shutdown):
                self.react(emoji='❤️')
                def stop(task, bot, history, emit):
                    self.service.stopping.set()
                    if output_after_shutdown:
                        emit('text_delta', dict(text='Interrupted output'))
                self.provider.start_task = stop
                task = self.send('Continue before shutdown.')
                try:
                    self.service._run_task(task['id'])
                    self.assertTrue(self.service._get('bots', self.bot['id'])['_pending_reactions'])
                    self.assertTrue(self.service._get('tasks', task['id'])['_reaction_feedback'])
                finally:
                    self.service.stopping.clear()
                    stored = self.service._get('tasks', task['id'])
                    stored['status'] = 'cancelled'
                    self.service._put('tasks', stored)

    def test_thumbs_request_contextual_judgment_but_removals_are_passive(self):
        for emoji in ('👍', '👎'):
            before = len(self.service.snapshot()['tasks'])
            self.react(emoji=emoji)
            self.assertEqual(len(self.service.snapshot()['tasks']), before + 1)
            task = self.service.snapshot()['tasks'][-1]
            self.service._run_task(task['id'])
            task, bot, history = self.provider.starts[-1]
            prompt, _ = turn_input(dict(operation='start', task=task, bot=bot, history=history))
            self.assertIn('[SILENT]', prompt)
            self.assertIn('does not approve', prompt)
            self.react(False, emoji=emoji)
            self.assertEqual(len(self.service.snapshot()['tasks']), before + 1)

    def test_early_decision_answer_refreshes_feedback_on_resume(self):
        def ask(task, bot, history, emit):
            try:
                emit('native_question', dict(title='Which report?', options=[dict(id='short', label='Short'), dict(id='long', label='Long')]))
            except YieldForDecision:
                decision = self.service.snapshot()['decisions'][-1]
                self.react(emoji='❤️')
                self.write(f'/v1/decisions/{decision["id"]}/answer', dict(option_id='short', revision=decision['revision']))
                raise
        self.provider.start_task = ask
        task = self.send('Prepare another report.')
        self.service._run_task(task['id'])
        resumed = []
        def resume(task, bot, history, decision, emit):
            resumed.extend(task['_reaction_feedback'])
            emit('completed', dict(summary='Done.'))
        self.provider.resume_task = resume
        self.service._run_task(task['id'])
        self.assertEqual(resumed[0]['reactions'], ['❤️'])

    def test_readding_thumb_replaces_pending_removal(self):
        self.react(emoji='👍')
        task = self.service.snapshot()['tasks'][-1]
        self.service._run_task(task['id'])
        self.react(False, emoji='👍')
        removed = self.service._get('bots', self.bot['id'])['_pending_reactions'][self.original['id']]
        self.react(emoji='👍')
        task = self.service.snapshot()['tasks'][-1]
        self.service._run_task(task['id'])
        feedback = self.provider.starts[-1][0]['_reaction_feedback'][0]
        self.assertEqual(feedback['reactions'], ['👍'])
        self.assertNotEqual(feedback['event_id'], removed['event_id'])

    def test_silent_thumb_completion_creates_no_message_or_unread_marker(self):
        self.react(emoji='👍')
        task = self.service.snapshot()['tasks'][-1]
        messages = self.service.snapshot()['messages']
        unread = self.service._get('bots', self.bot['id'])['unread_count']
        def silent(task, bot, history, emit):
            emit('completed', dict(summary='Complete.', reaction_silent=True))
        self.provider.start_task = silent
        self.service._run_task(task['id'])
        self.assertEqual(self.service.snapshot()['messages'], messages)
        self.assertEqual(self.service._get('bots', self.bot['id'])['unread_count'], unread)

    def test_silence_flag_cannot_hide_an_ordinary_completion(self):
        task = self.send('Explain the result.')
        messages = self.service.snapshot()['messages']
        def silent(task, bot, history, emit):
            emit('completed', dict(summary='Complete.', reaction_silent=True))
        self.provider.start_task = silent
        self.service._run_task(task['id'])
        self.assertGreater(len(self.service.snapshot()['messages']), len(messages))
