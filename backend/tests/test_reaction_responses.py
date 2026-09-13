"""Contextual thumb reactions use native silence without hiding normal replies."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4
import test_worker_model_settings as fixtures


class ReactionResponseTests(unittest.TestCase):
    setUp = fixtures.WorkerModelProcessTests.setUp
    tearDown = fixtures.WorkerModelProcessTests.tearDown

    def run_response(self, response, *, reaction):
        agent = self.source / 'run_agent.py'
        agent.write_text(agent.read_text().replace("'final_response': 'Ready.'", "'final_response': " + repr(response)))
        home = self.root / 'home'
        home.mkdir()
        task = dict(id=str(uuid4()), title='Reaction', bot_id=self.bot['id'])
        message = dict(id=str(uuid4()), task_id=task['id'], role='user', kind='text', text='Check this.')
        if reaction:
            message.update(text='Reaction added: 👍', reaction_event=dict(emoji='👍', action='added'))
        task['_turn_message_ids'] = [message['id']]
        request = dict(operation='start', task=task, bot=self.bot, history=[message], model='test',
                       runtime_config=self.native, provider='openai-codex', base_url=self.native['base_url'])
        env = dict(PATH=os.environ.get('PATH', ''), HOME=str(home), HERMES_HOME=str(home),
                   COCONUT_DEFAULT_HOME=str(home), PYTHONPATH=str(self.source),
                   WORKER_CAPTURE=str(self.root / 'capture.jsonl'), EXPECTED_NATIVE_KEY=self.native['api_key'])
        worker = Path(__file__).resolve().parents[1] / 'coconut/hermes_worker.py'
        process = subprocess.Popen([sys.executable, str(worker)], text=True, env=env, cwd=self.root,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        events = []
        try:
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            for line in process.stdout:
                event = json.loads(line)
                if event['type'] == 'turn_end':
                    break
                events.append(event)
            process.stdin.close()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
        self.assertNotIn('failed', [event['type'] for event in events], events)
        self.assertIn('completed', [event['type'] for event in events], events)
        if reaction and response == '[SILENT]':
            completion = next(event['data'] for event in events if event['type'] == 'completed')
            self.assertTrue(completion.get('reaction_silent'), completion)
        return [event['data']['text'] for event in events if event['type'] == 'text_delta']

    def test_unneeded_reaction_reply_stays_silent(self):
        self.assertEqual(self.run_response('[SILENT]', reaction=True), [])

    def test_useful_reaction_reply_is_delivered(self):
        self.assertEqual(self.run_response('Which part should I change?', reaction=True), ['Which part should I change?'])

    def test_other_bot_turns_also_honor_the_silence_control(self):
        self.assertEqual(self.run_response('[SILENT]', reaction=False), [])

    def test_normal_message_keeps_an_explanation_of_the_marker(self):
        text = 'The marker is [SILENT].'
        self.assertEqual(self.run_response(text, reaction=False), [text])
