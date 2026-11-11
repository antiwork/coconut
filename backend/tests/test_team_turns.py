"""A bot of a team-only Mac answers in the team chat (coconut/team_turns.py).

Ada's Mac is a full Mac (`first`). The team-only Mac (`only`) runs the bot Reporter with its chat connected. The Macs
share the FakeRelay and the FakeHelper of team_fakes.py. FakeGateway plays the `coconut` adapter of the team mode: it
polls the bridge of the team-only Mac for the home of the bot and reports a script of events for each turn. No model
runs. test_team_chat_gateway.py runs the adapter of the plugin, and test_native_team_chat.py a real Hermes gateway.
"""
import logging
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from coconut import team_store, team_turns
from coconut.team_relay import TeamRelayError
from coconut.team_turns import ASKED, DENIED, UNAVAILABLE, UNFINISHED
from test_team_only import TeamOnlyMacs


class FakeGateway(threading.Thread):
    """Poll the bridge as the adapter of the team mode does, and report the events of `script(turn)` for each turn.
    A `question` event waits for its answer, which the next poll brings."""

    def __init__(self, bridge, home, script):
        super().__init__(daemon=True)
        self.bridge, self.home, self.script = bridge, str(home), script
        self.turns, self.answers = [], []
        self.done = threading.Event()
        bridge.poll({'home': self.home, 'wait': 0})

    def run(self):
        while not self.done.is_set():
            answer = self.bridge.poll({'home': self.home, 'wait': 0.2,
                                       'active': [turn['id'] for turn in self.turns]})
            self.answers += answer.get('answers', [])
            for turn in answer['turns']:
                self.turns.append(turn)
                for event in self.script(turn):
                    if event['kind'] == 'question':
                        self.bridge.event(dict(event, turn_id=turn['id']))
                        deadline = time.monotonic() + 5
                        while not any(item['question_id'] == event['question_id'] for item in self.answers):
                            self.answers += self.bridge.poll({'home': self.home, 'wait': 0.2,
                                                              'active': [turn['id']]}).get('answers', [])
                            if time.monotonic() > deadline:
                                return
                        continue
                    self.bridge.event(dict(event, turn_id=turn['id']))

    def stop(self):
        self.done.set()
        self.join(5)


def reply(text):
    return lambda turn: [{'kind': 'send', 'message_id': f'{turn["id"]}:1', 'text': text},
                         {'kind': 'done', 'outcome': 'success'}]


class TeamTurnMacs(TeamOnlyMacs):
    def setUp(self):
        super().setUp()
        self.room = self.team()
        self.bot = self.declare()
        self.share(self.room, self.bot)
        self.connect()
        self.sync()
        (seen,) = [bot for bot in self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['bots']]
        self.mention = seen['id']
        self.entry = self.mention[-32:]
        self.gateway = None

    def connect(self, chat=True, bot=None):
        return self.call('PATCH', f'/v1/team/bots/{(bot or self.bot)["id"]}', {'chat': chat})['bot']

    def send(self, text, service=None, **fields):
        return self.call('POST', f'/v1/team/rooms/{self.room}/messages', dict(fields, text=text),
                         service=service or self.first)['message']

    def triggers(self):
        with self.only.lock:
            return [(row['entry'], row['started']) for row in self.only.team_store._rows(
                team_store.TRIGGER_FIELDS, 'SELECT * FROM team_triggers ORDER BY number')]

    def serve(self, script):
        self.gateway = FakeGateway(self.only.gateway_bridge, self.home, script)
        self.gateway.start()
        self.addCleanup(self.gateway.stop)
        return self.gateway

    def run_turns(self):
        """Run the waiting turns of the team-only Mac in this thread, then let Ada's Mac read the chat."""
        with self.only.lock:
            waiting = self.only.team_store.waiting()
        for room, bot in waiting:
            self.only._turns_run(room, bot)
        self.sync()

    def chat(self, service=None):
        """The chat as Ada's Mac shows it, oldest first: (writer, text, reply_to)."""
        rows = self.call('GET', f'/v1/team/rooms/{self.room}/messages', service=service or self.first)['messages']
        return [(row['bot_name'] or row['member_name'], row['text'], row['reply_to']) for row in reversed(rows)]


class TriggerTests(TeamTurnMacs, unittest.TestCase):
    def test_a_mention_and_a_reply_to_the_bot_start_a_turn(self):
        named = self.send('@Reporter what is open?', mentions=[self.mention])
        self.sync()
        self.assertEqual(self.triggers(), [(named['id'], 0)])
        self.serve(reply('3 tasks are open.'))
        self.run_turns()
        (_, answer) = self.chat()[-2:]
        self.assertEqual(answer, ('Reporter', '3 tasks are open.', named['id']))
        self.assertEqual(self.triggers(), [])
        # A reply to the message of the bot starts a turn without a mention.
        bot_message = self.call('GET', f'/v1/team/rooms/{self.room}/messages', service=self.first)['messages'][0]
        follow = self.send('And tomorrow?', reply_to=bot_message['id'])
        self.sync()
        self.assertEqual(self.triggers(), [(follow['id'], 0)])
        turn_text = []
        self.gateway.script = lambda turn: (turn_text.append(turn), reply('2 tasks.')(turn))[1]
        self.run_turns()
        self.assertEqual(self.chat()[-1], ('Reporter', '2 tasks.', follow['id']))
        # The turn knows that the person answers the bot.
        self.assertEqual(turn_text[0]['team']['reply_to'], {'id': bot_message['id'], 'text': '3 tasks are open.',
                                                            'own': True})

    def test_a_message_that_names_no_bot_of_this_mac_starts_nothing(self):
        ada = self.first.team_store.room(self.room)['member']
        self.send('Hello team')
        self.send('Hello Ada', mentions=[ada])
        # A reply to a message of a person, not of the bot.
        first = self.send('A plain message')
        self.send('A reply to a person', reply_to=first['id'])
        self.sync()
        self.assertEqual(self.triggers(), [])

    def test_a_message_of_a_bot_an_edit_and_an_old_message_start_nothing(self):
        room = self.first.team_store.room(self.room)
        fields = {'kind': 'message', 'text': 'A bot names the bot.', 'mentions': [self.entry], 'bot': '9' * 32}
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, 'a' * 32, False, fields)
        # An edit that adds the mention: the second write of the entry.
        plain = self.send('No mention yet')
        with self.first._team_room_lock(self.room):
            self.first._team_write(room, plain['id'], False, {'kind': 'message', 'text': 'Now @Reporter',
                                                              'mentions': [self.entry]})
        self.sync()
        self.assertEqual(self.triggers(), [])
        # A message over one hour old, for example the history that a Mac reads after a join.
        self.send('@Reporter old', mentions=[self.mention])
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        self.sync()
        self.assertEqual(self.triggers(), [])

    def test_ada_sees_the_bot_as_open_only_while_its_chat_is_connected(self):
        def seen():
            (bot,) = self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['bots']
            return bot['open']
        # TeamMentions.bots(of:) of the app offers a bot after @ only while `open` is true.
        self.assertIs(seen(), True)
        self.connect(chat=False)
        self.sync()
        self.assertIs(seen(), False)
        self.connect(chat=True)
        self.sync()
        self.assertIs(seen(), True)

    def test_a_bot_without_its_chat_or_without_the_team_starts_nothing(self):
        self.connect(chat=False)
        self.send('@Reporter one', mentions=[self.mention])
        self.sync()
        self.assertEqual(self.triggers(), [])
        self.connect(chat=True)
        self.call('DELETE', f'/v1/team/rooms/{self.room}/bots/{self.bot["id"]}')
        self.send('@Reporter two', mentions=[self.mention])
        self.sync()
        self.assertEqual(self.triggers(), [])

    def test_the_queue_keeps_the_order_and_its_bound(self):
        sent = [self.send(f'@Reporter {number}', mentions=[self.mention])['id'] for number in range(10)]
        with self.assertLogs('coconut.team_store', logging.WARNING) as logs:
            self.sync()
        self.assertEqual(self.triggers(), [(entry, 0) for entry in sent[:team_store.MAX_WAITING]])
        self.assertEqual(len(logs.records), 2)
        self.assertNotIn('@Reporter', '\n'.join(logs.output))
        # The turns run in that order, one at a time.
        order = []
        self.serve(lambda turn: (order.append(turn['text']), reply('ok')(turn))[1])
        self.run_turns()
        self.assertEqual(order, [f'@Reporter {number}' for number in range(team_store.MAX_WAITING)])

    def test_a_trigger_that_is_removed_before_its_turn_is_skipped(self):
        named = self.send('@Reporter please', mentions=[self.mention])
        self.sync()
        self.call('DELETE', f'/v1/team/rooms/{self.room}/messages/{named["id"]}', service=self.first)
        self.sync()
        gateway = self.serve(reply('never'))
        self.run_turns()
        self.assertEqual((self.triggers(), gateway.turns), ([], []))

    def test_a_full_service_queues_nothing(self):
        # Grace's Mac is a full Mac. A page of its bot Helper is shared with the team, so Helper has a `bot` entry and a
        # row in team_shares, and its record even has `chat`.
        self.join(self.second, self.invite({'room': self.room})['link'], 'Grace')
        helper = self.second.mutate('POST', '/v1/bots', {'name': 'Helper'}, str(uuid4()), 'phone')
        with self.second.transaction():
            record = self.second._get('bots', helper['id'])
            self.second._put('bots', dict(record, chat=True))
        self.share_page(self.second, self.room, helper)
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        with self.second.lock:
            self.assertEqual(list(self.second.team_store.shares(self.room)), [helper['id']])
        bots = {bot['name']: bot['id'] for bot in self.call('GET', '/v1/team/rooms', service=self.first)['rooms'][0]['bots']}
        self.send('@Helper @Reporter', mentions=[bots['Helper'], bots['Reporter']])
        self.second.team_sync_pass()
        self.first.team_sync_pass()
        self.assertFalse(self.second.team_store.answers)
        self.assertFalse(self.first.team_store.answers)
        for service in (self.first, self.second):
            with service.lock:
                self.assertEqual(service.db.execute('SELECT COUNT(*) FROM team_triggers').fetchone()[0], 0)
            self.assertIsNone(getattr(service, '_turns_thread', None))
        # The team-only Mac queues its own bot only.
        self.only.team_sync_pass()
        self.assertEqual(len(self.triggers()), 1)

class TurnTests(TeamTurnMacs, unittest.TestCase):
    def trigger(self, text='@Reporter what is open?', **fields):
        sent = self.send(text, mentions=[self.mention], **fields)
        self.sync()
        return sent

    def test_the_turn_gets_the_sender_the_team_and_the_context(self):
        self.send('Plan for today: ship the export.')
        named = self.trigger()
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply('Done.')(turn))[1])
        self.run_turns()
        (turn,) = turns
        ada = self.first.team_store.room(self.room)['member']
        self.assertEqual((turn['chat_id'], turn['chat_name'], turn['text']),
                         (team_turns.chat_id(self.room, self.bot['id']), 'Example Team', '@Reporter what is open?'))
        team = turn['team']
        self.assertEqual((team['member'], team['member_name']), (ada, 'Ada'))
        self.assertIn('Example Team', team['rules'])
        self.assertTrue(team['context'].startswith(team_turns.CONTEXT))
        self.assertIn('Ada wrote:\n> Plan for today: ship the export.', team['context'])
        self.assertNotIn('what is open', team['context'])
        self.assertNotIn('session_root', turn)
        # The next turn has only the messages after the bot's last message.
        self.send('Something new.')
        self.trigger('@Reporter again')
        self.run_turns()
        self.assertEqual(turns[1]['team']['context'], f'{team_turns.CONTEXT}\nAda wrote:\n> Something new.')
        self.assertEqual(named['id'], self.chat()[2][2])

    def test_the_context_has_at_most_twenty_messages(self):
        for number in range(25):
            self.send(f'Message {number}')
        self.trigger()
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply('ok')(turn))[1])
        self.run_turns()
        context = turns[0]['team']['context']
        self.assertEqual(context.count(' wrote:'), team_turns.CONTEXT_MESSAGES)
        self.assertIn('Message 24', context)
        self.assertNotIn('Message 4\n', context + '\n')

    def test_each_segment_is_a_message_and_a_long_text_is_split(self):
        named = self.trigger()
        long = 'x' * (team_store.MAX_TEXT + 5)

        def script(turn):
            return [{'kind': 'send', 'message_id': 'c', 'text': 'I look at it.', 'interim': True},
                    {'kind': 'send', 'message_id': 'a', 'text': 'First part.'},
                    {'kind': 'send', 'message_id': 'b', 'text': 'Draft'},
                    {'kind': 'edit', 'message_id': 'b', 'text': long},
                    {'kind': 'done', 'outcome': 'success'}]
        self.serve(script)
        self.run_turns()
        replies = [row for row in self.chat() if row[0] == 'Reporter']
        self.assertEqual([(text if len(text) < 100 else len(text), to) for _, text, to in replies],
                         [('I look at it.', named['id']), ('First part.', None), (team_store.MAX_TEXT, None),
                          ('xxxxx', None)])
        # A second run of the same turn adds no message: each entry ID comes from the trigger and the part.
        before = len(self.chat())
        turn = {'room': self.room, 'bot': self.bot['id'], 'entry': named['id']}
        team_turns.Reply(self.only, turn).say('I look at it.')
        self.sync()
        self.assertEqual(len(self.chat()), before)

    def test_an_approval_is_denied_and_a_clarify_question_goes_into_the_chat(self):
        named = self.trigger()

        def script(turn):
            return [{'kind': 'question', 'question_id': 'q1', 'question_kind': 'approval', 'text': 'Run rm?',
                     'choices': [{'id': 'once', 'label': 'Allow once'}, {'id': 'deny', 'label': 'Deny'}]},
                    {'kind': 'question', 'question_id': 'q2', 'question_kind': 'clarify', 'text': 'Which team?',
                     'choices': [{'id': 'choice-0', 'label': 'Web'}, {'id': 'choice-1', 'label': 'iOS'}]},
                    {'kind': 'send', 'message_id': 'a', 'text': 'I asked in the chat.'},
                    {'kind': 'done', 'outcome': 'success'}]
        gateway = self.serve(script)
        self.run_turns()
        self.assertEqual({item['question_id']: (item['choice'], item['text']) for item in gateway.answers},
                         {'q1': ('deny', DENIED), 'q2': ('', ASKED)})
        replies = [(text, to) for writer, text, to in self.chat() if writer == 'Reporter']
        self.assertEqual(replies, [('Which team?\n- Web\n- iOS', named['id']), ('I asked in the chat.', None)])

    def test_a_turn_makes_no_task_message_or_card_and_logs_no_text(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug('start')
            self.trigger('@Reporter secret plans')
            self.serve(reply('secret answer'))
            self.run_turns()
        self.assertIn(('Reporter', 'secret answer'), [row[:2] for row in self.chat()])
        text = '\n'.join(logs.output)
        self.assertNotIn('secret', text)
        tables = {row[0] for row in self.only.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables & {'tasks', 'messages', 'decisions'}, set())


class OutsideTests(TeamTurnMacs, unittest.TestCase):
    """A send of Hermes in a team chat outside a turn of Coconut (POST /v1/gateway/messages)."""

    def message(self, **fields):
        data = {'home': str(self.home), 'chat_id': team_turns.chat_id(self.room, self.bot['id']), 'kind': 'send',
                'message_id': 'outside:1', 'text': 'The background job finished.', **fields}
        return self.only.gateway_message(data)

    def test_a_send_outside_a_turn_is_a_message_of_the_bot_once(self):
        self.assertEqual(self.message(), {'recorded': True})
        self.assertEqual(self.message(), {'recorded': True})
        self.sync()
        self.assertEqual([row for row in self.chat() if row[0] == 'Reporter'],
                         [('Reporter', 'The background job finished.', None)])
        # A message of the chat is complete: an edit and a delete change nothing.
        self.assertFalse(self.message(kind='edit', text='Changed.')['recorded'])
        self.assertFalse(self.message(kind='delete')['recorded'])

    def test_a_send_for_another_home_or_chat_is_refused_and_a_bot_without_its_chat_writes_nothing(self):
        other = self.home.parent / 'other'
        self.assertEqual(self.refusal(lambda: self.message(home=str(other)))[0], 404)
        for chat in ('', self.bot['id'], 'nobody:team:' + self.room):
            with self.subTest(chat=chat):
                self.assertEqual(self.refusal(lambda: self.message(chat_id=chat))[0], 400 if ':team:' not in chat else 404)
        self.connect(chat=False)
        self.assertFalse(self.message()['recorded'])
        self.sync()
        self.assertEqual([row for row in self.chat() if row[0] == 'Reporter'], [])


class FailureTests(TeamTurnMacs, unittest.TestCase):
    def trigger(self):
        sent = self.send('@Reporter status?', mentions=[self.mention])
        self.sync()
        return sent

    def replies(self):
        return [(text, to) for writer, text, to in self.chat() if writer == 'Reporter']

    def test_no_adapter_gives_one_notice(self):
        named = self.trigger()
        with mock.patch.object(team_turns, 'ADAPTER_SECONDS', 0.2):
            self.run_turns()
        self.assertEqual(self.replies(), [(UNAVAILABLE, named['id'])])
        self.assertEqual(self.triggers(), [])

    def test_a_parked_profile_gives_the_notice_at_once(self):
        named = self.trigger()
        (self.home / 'gateway.parked').write_text('')
        started = time.monotonic()
        self.run_turns()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.replies(), [(UNAVAILABLE, named['id'])])

    def test_a_gateway_that_stops_in_a_turn_keeps_the_complete_messages_then_a_notice(self):
        named = self.trigger()
        self.serve(lambda turn: [{'kind': 'send', 'message_id': 'a', 'text': 'First finding.'},
                                 {'kind': 'send', 'message_id': 'b', 'text': 'Second finding.'}])
        with mock.patch('coconut.gateway_bridge.SILENT_SECONDS', 0.5):
            self.gateway.done.set()
            self.gateway.join(5)
            self.gateway = FakeGateway(self.only.gateway_bridge, self.home, self.gateway.script)
            self.gateway.start()
            self.addCleanup(self.gateway.stop)
            stopper = threading.Thread(target=lambda: (time.sleep(1), self.gateway.done.set()), daemon=True)
            stopper.start()
            self.run_turns()
        self.assertEqual(self.replies(), [('First finding.', named['id']), ('Second finding.', None),
                                          (UNFINISHED, None)])

    def test_a_failed_turn_and_the_time_cap_give_the_notice(self):
        named = self.trigger()
        self.serve(lambda turn: [{'kind': 'done', 'outcome': 'failure'}])
        self.run_turns()
        self.assertEqual(self.replies(), [(UNFINISHED, named['id'])])
        # A turn that never ends stops at its time.
        second = self.trigger()
        self.gateway.script = lambda turn: [{'kind': 'typing'}]
        with mock.patch.object(team_turns, 'TURN_SECONDS', 0.5):
            self.run_turns()
        self.assertEqual(self.replies()[-1], (UNFINISHED, second['id']))

    def test_a_stop_in_a_turn_loses_the_turn_and_the_next_start_posts_one_notice(self):
        named = self.trigger()
        self.serve(lambda turn: [{'kind': 'typing'}])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.only.stopping.set()
        self.only.gateway_bridge.close()
        runner.join(5)
        self.assertEqual(self.triggers(), [(named['id'], 1)])
        self.gateway.stop()
        self.only.close()
        # The next start of the service.
        restarted = self.team_only_mac('only')
        restarted._turns_recover()
        self.first.team_sync_pass()
        self.assertEqual(self.replies(), [(UNFINISHED, named['id'])])
        with restarted.lock:
            self.assertEqual(restarted.db.execute('SELECT COUNT(*) FROM team_triggers').fetchone()[0], 0)
        self.assertEqual(len(self.gateway.turns), 1, 'The trigger does not run again.')

    def test_a_relay_that_refuses_the_reply_leaves_a_log_line_and_nothing_else(self):
        self.trigger()
        self.serve(reply('An answer.'))
        write = self.relay.write

        def refuse(relay, room, token, entry, kept, body, tries=1):
            if not kept:
                raise TeamRelayError(503, 'The relay answered 503.')
            return write(relay, room, token, entry, kept, body, tries)
        with mock.patch.object(self.relay, 'write', refuse), \
                self.assertLogs('coconut.team_turns', logging.WARNING) as logs:
            self.run_turns()
        self.assertEqual(self.replies(), [])
        self.assertIn('did not reach the relay', '\n'.join(logs.output))
        self.assertEqual(self.triggers(), [])

    def runner(self):
        thread = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        with self.only._turns_lock:
            self.only._turns_running[(self.room, self.bot['id'])] = thread
        thread.start()
        return thread

    def wait_for_turn(self):
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(self.gateway.turns)

    def test_a_disconnect_while_the_turn_waits_for_the_gateway_starts_no_turn(self):
        self.trigger()
        runner = self.runner()
        time.sleep(1.5)
        self.connect(chat=False)
        runner.join(5)
        self.assertFalse(runner.is_alive(), 'The wait ends when the bot no longer answers.')
        # A gateway that connects later gets no turn.
        gateway = self.serve(reply('too late'))
        time.sleep(.5)
        self.assertEqual((gateway.turns, self.replies(), self.triggers()), ([], [], []))

    def test_a_disconnect_between_the_last_check_and_the_registration_still_stops_the_turn(self):
        self.trigger()
        gateway = self.serve(reply('never'))
        checked, stopped = threading.Event(), threading.Event()
        submit = self.only.gateway_bridge.submit_turn

        def slow_submit(*arguments, **options):
            # The check passed. The operator disconnects now, before the bridge has the turn.
            checked.set()
            stopped.wait(1)
            return submit(*arguments, **options)
        with mock.patch.object(self.only.gateway_bridge, 'submit_turn', side_effect=slow_submit):
            runner = self.runner()
            checked.wait(5)
            disconnect = threading.Thread(target=lambda: (self.connect(chat=False), stopped.set()), daemon=True)
            disconnect.start()
            runner.join(10)
            disconnect.join(5)
        self.assertTrue(stopped.is_set())
        self.sync()
        self.assertEqual(self.replies(), [])
        # The stop came while the turn was pending or active: no adapter runs it now.
        self.assertEqual([t for t in self.only.gateway_bridge.turns.values() if t.state in ('pending', 'active')], [])
        self.assertLessEqual(len(gateway.turns), 1)

    def test_a_removed_request_or_a_sender_who_left_stops_the_turn_and_gets_nothing(self):
        for case in ('removed', 'left'):
            with self.subTest(case=case):
                sender = self.first
                if case == 'left':
                    sender = self.second
                    self.join(self.second, self.invite({'room': self.room})['link'], 'Grace')
                    self.second.team_sync_pass()
                    self.only.team_sync_pass()
                named = self.send('@Reporter please', service=sender, mentions=[self.mention])
                self.sync()
                released = threading.Event()
                self.serve(lambda turn: (released.wait(5), reply('Late answer.')(turn))[1])
                runner = self.runner()
                self.wait_for_turn()
                if case == 'removed':
                    self.call('DELETE', f'/v1/team/rooms/{self.room}/messages/{named["id"]}', service=self.first)
                else:
                    self.leave(self.second, {'room': self.room})
                self.sync()
                self.only._turns_check()
                turn_id = self.gateway.turns[0]['id']
                self.assertEqual(self.only.gateway_bridge.turns[turn_id].state, 'stopping')
                released.set()
                runner.join(5)
                self.sync()
                self.assertEqual(self.replies(), [])
                self.gateway.stop()

    def test_the_restart_notice_of_a_removed_request_is_not_posted(self):
        named = self.trigger()
        with self.only.transaction():
            row = self.only.team_store.next_trigger(self.room, self.bot['id'])
            self.only.team_store.start_trigger(row['number'])
        self.call('DELETE', f'/v1/team/rooms/{self.room}/messages/{named["id"]}', service=self.first)
        self.sync()
        self.only._turns_recover()
        self.sync()
        self.assertEqual((self.replies(), self.triggers()), ([], []))

    def test_a_bot_whose_chat_goes_off_or_that_leaves_writes_nothing_more(self):
        self.trigger()
        released = threading.Event()
        self.serve(lambda turn: (released.wait(5), [{'kind': 'send', 'message_id': 'a', 'text': 'Late answer.'},
                                                    {'kind': 'done', 'outcome': 'success'}])[1])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.connect(chat=False)
        released.set()
        runner.join(5)
        self.sync()
        self.assertEqual(self.replies(), [])
        # A removal of a bot whose chat is connected waits for disconnect-chat.
        self.connect(chat=True)
        status, text = self.refusal(self.call, 'DELETE', f'/v1/team/bots/{self.bot["id"]}')
        self.assertEqual(status, 409)
        self.assertIn('coconut team disconnect-chat Reporter', text)

    def test_a_fault_in_a_turn_posts_the_notice_and_a_second_fault_stops_nothing(self):
        named = self.trigger()
        with mock.patch.object(self.only, '_team_turn', side_effect=ValueError('boom')), \
                self.assertLogs('coconut.team_turns', logging.ERROR):
            self.run_turns()
        self.assertEqual(self.replies(), [(UNFINISHED, named['id'])])
        self.assertEqual(self.triggers(), [])
        # A fault of the notice too: the thread goes on with the next trigger.
        self.trigger()
        third = self.trigger()
        self.serve(reply('Here it is.'))
        calls = []

        def first_fails(turn):
            calls.append(turn)
            if len(calls) == 1:
                raise ValueError('boom')
            return team_turns.TeamTurns._team_turn(self.only, turn)
        say = self.only._team_say

        def notice_fails(room, bot, trigger, part, text, reply_to=None):
            if part == 'fault':
                raise RuntimeError('relay down')
            return say(room, bot, trigger, part, text, reply_to)
        with mock.patch.object(self.only, '_team_turn', side_effect=first_fails), \
                mock.patch.object(self.only, '_team_say', side_effect=notice_fails), \
                self.assertLogs('coconut.team_turns', logging.ERROR):
            self.run_turns()
        self.assertEqual(self.replies()[-1], ('Here it is.', third['id']))
        self.assertEqual(self.triggers(), [])

    def test_a_trigger_that_waited_too_long_and_an_empty_reply_get_the_notice(self):
        named = self.trigger()
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        gateway = self.serve(reply('never'))
        self.run_turns()
        self.assertEqual((self.replies(), gateway.turns), ([(UNFINISHED, named['id'])], []))
        self.only.team_store.clock = time.time
        second = self.trigger()
        gateway.script = lambda turn: [{'kind': 'done', 'outcome': 'success'}]
        self.run_turns()
        self.assertEqual(self.replies()[-1], (UNFINISHED, second['id']))

    def test_a_mac_that_leaves_in_a_turn_stops_it_and_writes_nothing(self):
        self.trigger()
        released = threading.Event()
        self.serve(lambda turn: (released.wait(5), reply('Late answer.')(turn))[1])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        with self.only._turns_lock:
            self.only._turns_running[(self.room, self.bot['id'])] = runner
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.leave(self.only, {'room': self.room})
        self.assertEqual(self.only.gateway_bridge.turns.get(self.gateway.turns[0]['id']).state, 'stopping')
        released.set()
        runner.join(5)
        self.first.team_sync_pass()
        self.assertEqual(self.replies(), [])
        with self.only.lock:
            self.assertEqual(self.only.db.execute('SELECT COUNT(*) FROM team_triggers').fetchone()[0], 0)

    def test_a_drain_waits_for_the_running_turn(self):
        self.assertTrue(self.only.drains)
        self.trigger()
        released = threading.Event()
        self.serve(lambda turn: (released.wait(5), reply('ok')(turn))[1])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        runner.start()
        with self.only._turns_lock:
            self.only._turns_running[(self.room, self.bot['id'])] = runner
        (self.only.root / 'drain-request').write_text(str(time.time() + 60))
        self.only._team_poll_drain()
        self.assertTrue(self.only._draining)
        self.assertFalse((self.only.root / 'drained').exists())
        released.set()
        runner.join(5)
        self.only._team_poll_drain()
        self.assertTrue((self.only.root / 'drained').exists())
        # A new trigger waits in the queue while the drain lasts.
        self.trigger()
        self.assertEqual(len(self.triggers()), 1)
        (self.only.root / 'drain-request').write_text(str(time.time() - 1))
        self.only._team_poll_drain()
        self.assertFalse(self.only._draining)
        self.assertFalse((self.only.root / 'drained').exists())


if __name__ == '__main__':
    unittest.main()
