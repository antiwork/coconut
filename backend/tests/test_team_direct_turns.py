"""A bot of a Mac for a team only answers a person in a direct chat (coconut/team_turns.py, docs/team-log.md, "Direct
chat with an agent", "The Mac for a team only").

Ada's Mac (`first`) and Grace's Mac (`second`) are full Macs whose apps know direct messages. The Mac for a team only
(`only`) runs the bot Reporter with its chat connected. FakeGateway of test_team_turns.py plays the adapter of the team
mode. No model runs.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import unittest
from secrets import token_hex
from unittest import mock

from coconut import team_store, team_turns, team_typing
from coconut.team_turns import (ASKED_DIRECT, DENIED_DIRECT, OUTDATED, PAGE_GONE, PAGE_LONGER, RULES_DIRECT,
                                UNAVAILABLE, UNFINISHED)
from team_fakes import DEVICE
from test_push import RecordingTransport, pair_phone
from test_team_push import flush, register
from test_team_turns import FakeGateway, TeamTurnMacs, reply


class DirectTurnMacs(TeamTurnMacs):
    """The room of TeamTurnMacs, with Grace in it, and the mark of direct messages on Ada's and Grace's Macs."""

    def setUp(self):
        super().setUp()
        self.join(self.second, self.invite({'room': self.room})['link'], 'Grace')
        for service in (self.first, self.second):
            self.ask(service)
        self.passes()
        self.ada, self.grace, self.mac = (service.team_store.room(self.room)['member']
                                          for service in (self.first, self.second, self.only))
        self.bot_id = self.mention

    def passes(self, count: int = 2):
        """Passes on each Mac: the first writes, the second reads what the others wrote."""
        for _ in range(count):
            for service in (self.only, self.first, self.second):
                service.team_sync_pass()

    def ask(self, service) -> dict:
        """The read of the rooms by an app that knows direct messages. The room of the test."""
        return service.team_request('GET', '/v1/team/rooms', {}, '', DEVICE, True)['rooms'][0]

    def path(self, rest: str = 'messages', chat: str | None = None) -> str:
        return f'/v1/team/rooms/{self.room}/directs/{chat or self.bot_id}/{rest}'

    def dm(self, text: str, service=None, **fields) -> dict:
        """A direct message of a person to the bot, then a pass of the Mac for a team only."""
        sent = self.phone(service or self.first)('POST', self.path(), dict(fields, text=text))['message']
        self.only.team_sync_pass()
        return sent

    def bot_chat(self, service=None) -> list[tuple[str, bool]]:
        """The chat of a person with the bot, oldest first, as text and `this_mac`."""
        page = self.phone(service or self.first)('GET', self.path(), {'limit': '100'})
        return [(message['text'], message['this_mac']) for message in reversed(page['messages'])]

    def bot_replies(self, service=None) -> list[str]:
        (service or self.first).team_sync_pass()
        return [text for text, mine in self.bot_chat(service) if not mine]

    def box(self, service, entry: str, to: str, plaintext: dict) -> None:
        """Write a `direct` entry of a Mac with a box for `to`, as a Mac of another version could."""
        room = service.team_store.room(self.room)
        secrets = json.loads(self.key_file(service, self.room).read_text())
        agreement = service.team_store.member(self.room, to)['agreement']
        box = service.team_helper.seal_direct(self.room, entry, secrets['agreement_key'], room['member'], to,
                                              agreement, json.dumps(plaintext).encode())
        with service._team_room_lock(self.room):
            service._team_write(room, entry, False, {'kind': 'direct', 'box': box})

    def plaintext(self, service, entry: str) -> dict:
        """The plaintext of the box of a `direct` entry in the store of a Mac (the box of FakeHelper)."""
        box = json.loads(service.team_store.entry(self.room, entry)['payload'])['box']
        sealed = json.loads(base64.urlsafe_b64decode(box + '=' * (-len(box) % 4)))
        return json.loads(base64.b64decode(sealed['plaintext']))

    def stored(self, service=None) -> list[tuple]:
        """The rows of team_directs on a Mac: writer, member, bot, text."""
        service = service or self.only
        with service.lock:
            return service.db.execute('SELECT writer, member, bot, text FROM team_directs WHERE room=? ORDER BY first',
                                      (self.room,)).fetchall()

    def direct_chat_id(self, member: str | None = None) -> str:
        return team_turns.chat_id(self.room, self.bot['id'], direct=member or self.ada)


class TheMarkOfABot(DirectTurnMacs, unittest.TestCase):
    def bot_payload(self) -> dict:
        entry = self.only.team_store.shares(self.room)[self.bot['id']]
        stored = self.only.team_store.entry(self.room, entry)
        return dict(json.loads(stored['payload']), rev=stored['rev'])

    def test_the_mark_follows_chat_and_is_written_once(self):
        payload = self.bot_payload()
        self.assertEqual((payload['open'], payload['direct']), (True, True))
        self.passes(3)
        self.assertEqual(self.bot_payload()['rev'], payload['rev'])
        self.assertEqual([bot['direct'] for bot in self.ask(self.first)['bots']], [True])
        self.connect(chat=False)
        self.passes()
        payload = self.bot_payload()
        self.assertNotIn('direct', payload)
        self.assertNotIn('open', payload)
        self.assertEqual([bot['direct'] for bot in self.ask(self.first)['bots']], [False])

    def test_a_bot_entry_without_the_mark_gets_it_at_the_next_pass(self):
        # The `bot` entry of an older version: `open` without `direct`.
        entry = self.only.team_store.shares(self.room)[self.bot['id']]
        room = self.only.team_store.room(self.room)
        with self.only._team_room_lock(self.room):
            self.only._team_write(room, entry, True, {'kind': 'bot', 'name': 'Reporter', 'avatar': '', 'color': '',
                                                      'open': True})
        rev = self.bot_payload()['rev']
        self.only.team_sync_pass()
        self.assertEqual((self.bot_payload()['rev'], self.bot_payload()['direct']), (rev + 1, True))
        self.only.team_sync_pass()
        self.assertEqual(self.bot_payload()['rev'], rev + 1)

    def test_a_persons_mac_never_writes_the_mark(self):
        from uuid import uuid4
        own = self.first.mutate('POST', '/v1/bots', {'name': 'Own'}, str(uuid4()), 'phone')
        with self.first.transaction():
            self.first._put('bots', dict(self.first._get('bots', own['id']), chat=True))
        self.share_page(self.first, self.room, own)
        self.passes()
        (entry,) = self.first.team_store.shares(self.room).values()
        self.assertNotIn('direct', json.loads(self.first.team_store.entry(self.room, entry)['payload']))


class ReadingOnTheMacForATeamOnly(DirectTurnMacs, unittest.TestCase):
    def test_a_message_to_the_bot_goes_into_the_chat_of_its_writer(self):
        sent = self.dm('Hello Reporter')
        self.assertEqual(self.stored(), [(self.ada, self.ada, self.entry, 'Hello Reporter')])
        self.assertEqual(self.triggers(), [(sent['id'], 0)])
        # It keeps no read position and has no Team chat message.
        for table in ('team_bot_direct_reads', 'team_direct_reads', 'team_messages'):
            with self.subTest(table=table):
                self.assertEqual(self.only.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_a_box_without_bot_and_a_box_for_another_member_show_nothing(self):
        self.box(self.first, token_hex(16), self.mac, {'to': self.mac, 'text': 'Hello Mac'})
        self.phone(self.first)('POST', self.path(chat=self.grace), {'text': 'Hello Grace'})
        self.box(self.first, token_hex(16), self.grace, {'to': self.grace, 'text': 'For Grace', 'bot': self.entry})
        self.only.team_sync_pass()
        self.assertEqual((self.stored(), self.triggers()), ([], []))

    def test_a_message_before_the_member_entry_that_it_needs_shows_after_the_read(self):
        self.dm('Before the rename')
        self.phone(self.first)('PATCH', f'/v1/team/rooms/{self.room}', {'member_name': 'Ada L.'})
        with self.only.transaction():
            for table in ('team_entries', 'team_members', 'team_directs', 'team_triggers'):
                self.only.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            self.only.team_store.update_room(self.room, seq=0)
        self.only.team_sync_pass()
        self.assertEqual(self.stored(), [(self.ada, self.ada, self.entry, 'Before the rename')])
        # The message waited for its key, so it is history: it starts no turn.
        self.assertEqual(self.triggers(), [])

    def test_an_own_message_that_the_store_does_not_have(self):
        self.dm('Hello Reporter')
        self.serve(reply('Hello Ada'))
        self.run_turns()
        with self.only.transaction():
            for table in ('team_entries', 'team_members', 'team_directs'):
                self.only.db.execute(f'DELETE FROM {table} WHERE room=?', (self.room,))
            self.only.team_store.update_room(self.room, seq=0)
        self.only.team_sync_pass()
        self.assertEqual([row[0] for row in self.stored()], [self.ada, self.mac])
        self.assertEqual(self.stored()[1], (self.mac, self.ada, self.entry, 'Hello Ada'))
        self.assertEqual(self.stored()[0][3], 'Hello Reporter')


class TriggerTests(DirectTurnMacs, unittest.TestCase):
    def test_an_edit_and_a_read_again_start_nothing(self):
        sent = self.phone(self.first)('POST', self.path(), {'text': 'Draft'})['message']
        self.phone(self.first)('PATCH', self.path(f'messages/{sent["id"]}'), {'text': 'Changed'})
        self.only.team_sync_pass()
        self.assertEqual(self.triggers(), [])
        named = self.dm('Hello')
        self.assertEqual(self.triggers(), [(named['id'], 0)])
        self.serve(reply('Hi'))
        self.run_turns()
        with self.only.transaction():
            self.only.team_store.update_room(self.room, seq=0)
        self.only.team_sync_pass()
        self.assertEqual(self.triggers(), [])

    def test_an_old_message_starts_nothing(self):
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        self.dm('Old')
        self.assertEqual(self.triggers(), [])

    def test_a_message_for_a_bot_that_does_not_answer_starts_nothing(self):
        quiet = self.declare('Quiet', home=self.home.parent / 'quiet')
        self.share(self.room, quiet)
        self.passes()
        entry = self.only.team_store.shares(self.room)[quiet['id']]
        self.box(self.first, token_hex(16), self.mac, {'to': self.mac, 'text': 'Hello Quiet', 'bot': entry})
        # A bot entry of another member names no bot of this Mac.
        self.box(self.first, token_hex(16), self.mac, {'to': self.mac, 'text': 'Hello', 'bot': token_hex(16)})
        self.only.team_sync_pass()
        self.assertEqual(len(self.stored()), 2)
        self.assertEqual(self.triggers(), [])

    def test_the_direct_chats_and_the_team_chat_share_the_queue_and_its_bound(self):
        named = self.send('@Reporter in the team', mentions=[self.mention])
        sent = [self.phone(self.first)('POST', self.path(), {'text': f'Direct {number}'})['message']['id']
                for number in range(9)]
        with self.assertLogs('coconut.team_store', logging.WARNING) as logs:
            self.sync()
        self.assertEqual([entry for entry, _ in self.triggers()], [named['id'], *sent[:team_store.MAX_WAITING - 1]])
        self.assertNotIn('Direct', '\n'.join(logs.output))
        order = []
        self.serve(lambda turn: (order.append(turn['text']), reply('ok')(turn))[1])
        self.run_turns()
        self.assertEqual(order, ['@Reporter in the team', *(f'Direct {n}' for n in range(team_store.MAX_WAITING - 1))])

    def test_a_trigger_whose_member_left_before_its_turn_goes(self):
        self.dm('From Grace', service=self.second)
        self.leave(self.second, {'room': self.room})
        self.only.team_sync_pass()
        gateway = self.serve(reply('never'))
        self.run_turns()
        self.assertEqual((gateway.turns, self.triggers()), ([], []))


class TurnTests(DirectTurnMacs, unittest.TestCase):
    def capture(self, text='ok'):
        turns = []
        self.serve(lambda turn: (turns.append(turn), reply(text)(turn))[1])
        return turns

    def test_one_session_for_each_person_and_bot(self):
        turns = self.capture()
        self.dm('From Ada')
        self.dm('From Grace', service=self.second)
        self.send('@Reporter team', mentions=[self.mention])
        self.sync()
        self.run_turns()
        chats = {turn['text']: (turn['chat_id'], turn['chat_name'], turn['team'].get('direct')) for turn in turns}
        self.assertEqual(chats, {
            'From Ada': (self.direct_chat_id(self.ada), 'Ada · Example Team', True),
            'From Grace': (self.direct_chat_id(self.grace), 'Grace · Example Team', True),
            '@Reporter team': (team_turns.chat_id(self.room, self.bot['id']), 'Example Team', None)})
        self.assertEqual(self.direct_chat_id(self.ada), f'{self.bot["id"]}:team:{self.room}:direct:{self.ada}')
        (ada,) = [turn for turn in turns if turn['text'] == 'From Ada']
        self.assertEqual((ada['team']['member'], ada['team']['member_name']), (self.ada, 'Ada'))
        self.assertEqual(ada['team']['rules'], RULES_DIRECT.format(name='Ada', team='Example Team'))
        self.assertIn('Only Ada reads your reply.', ada['team']['rules'])

    def test_the_context_has_the_chat_since_the_last_message_of_the_bot(self):
        turns = self.capture('Noted.')
        # An old message is history: it starts no turn, and it is context.
        self.phone(self.first)('POST', self.path(), {'text': 'Plan: ship the export.'})
        self.send('A message of the Team chat.')
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        self.only.team_sync_pass()
        self.only.team_store.clock = time.time
        self.dm('What is open?')
        self.run_turns()
        self.assertEqual(len(turns), 1)
        context = turns[0]['team']['context']
        self.assertTrue(context.startswith(team_turns.CONTEXT))
        self.assertIn('Ada wrote:\n> Plan: ship the export.', context)
        self.assertNotIn('Team chat', context)
        self.assertNotIn('What is open', context)
        self.dm('And now?')
        self.run_turns()
        self.assertIsNone(turns[1]['team']['context'])

    def test_a_page_a_long_page_and_a_page_that_is_gone(self):
        turns = self.capture()
        self.save()
        self.save(key='long', title='Long page', content='y' * (team_store.MAX_TEXT + 10))
        self.passes()
        pages = {page['title']: page['id'] for page in self.first.documents()['documents'] if 'team' in page}
        self.dm('About the report', about_document_id=pages['Weekly report'])
        self.run_turns()
        context = turns[0]['team']['context']
        self.assertIn('Weekly report', context)
        self.assertIn('weekly-report', context)
        self.assertIn('> 3 tasks are open.', context)
        self.assertNotIn(PAGE_LONGER, context)
        self.dm('About the long page', about_document_id=pages['Long page'])
        self.run_turns()
        context = turns[1]['team']['context']
        self.assertIn(PAGE_LONGER, context)
        self.assertIn('y' * team_store.MAX_TEXT, context)
        self.assertNotIn('y' * (team_store.MAX_TEXT + 1), context)
        # The page goes from this Mac after the person wrote.
        self.phone(self.first)('POST', self.path(), {'text': 'About the report again',
                                                      'about_document_id': pages['Weekly report']})
        self.only.gateway_document_delete({'home': str(self.home), 'key': 'weekly-report'})
        self.only.team_sync_pass()
        self.run_turns()
        self.assertIn(PAGE_GONE.format(name='Ada'), turns[2]['team']['context'])
        self.assertNotIn('3 tasks are open', turns[2]['team']['context'])

    def test_each_reply_is_a_direct_message_for_the_person_only(self):
        long = 'x' * (team_store.MAX_TEXT + 5)

        def script(turn):
            return [{'kind': 'send', 'message_id': 'c', 'text': 'I look at it.', 'interim': True},
                    {'kind': 'send', 'message_id': 'a', 'text': long},
                    {'kind': 'done', 'outcome': 'success'}]
        self.serve(script)
        named = self.dm('Status?')
        self.run_turns()
        self.assertEqual([len(text) if len(text) > 100 else text for text in self.bot_replies()],
                         ['I look at it.', team_store.MAX_TEXT, 'xxxxx'])
        replies = [row for row in self.stored() if row[0] == self.mac]
        self.assertEqual(len(replies), 3)
        for entry in self.only.db.execute('SELECT entry FROM team_directs WHERE writer=?', (self.mac,)).fetchall():
            self.assertEqual(set(self.plaintext(self.only, entry[0])), {'to', 'text', 'bot'})
        # Nothing reaches the Team chat or Grace.
        self.second.team_sync_pass()
        self.assertEqual((self.stored(self.second), self.chat()), ([], []))
        self.assertEqual(self.ask(self.second)['bot_directs'], [])
        # A second run of the same turn adds no message.
        turn = {'room': self.room, 'bot': self.bot['id'], 'entry': named['id'], 'chat': None, 'direct': self.ada,
                'member_name': 'Ada'}
        team_turns.Reply(self.only, turn).say('I look at it.')
        self.assertEqual(len(self.bot_replies()), 3)

    def test_an_approval_is_denied_and_a_clarify_question_goes_into_the_chat(self):
        def script(turn):
            return [{'kind': 'question', 'question_id': 'q1', 'question_kind': 'approval', 'text': 'Run rm?',
                     'choices': [{'id': 'once', 'label': 'Allow once'}, {'id': 'deny', 'label': 'Deny'}]},
                    {'kind': 'question', 'question_id': 'q2', 'question_kind': 'clarify', 'text': 'Which team?',
                     'choices': [{'id': 'choice-0', 'label': 'Web'}, {'id': 'choice-1', 'label': 'iOS'}]},
                    {'kind': 'done', 'outcome': 'success'}]
        gateway = self.serve(script)
        self.dm('Clean up.')
        self.run_turns()
        self.assertEqual({item['question_id']: (item['choice'], item['text']) for item in gateway.answers},
                         {'q1': ('deny', DENIED_DIRECT.format(name='Ada')), 'q2': ('', ASKED_DIRECT.format(name='Ada'))})
        self.assertEqual(self.bot_replies(), ['Which team?\n- Web\n- iOS'])

    def test_a_turn_logs_no_text_and_no_name(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug('start')
            self.serve(reply('secret answer'))
            self.dm('secret plans')
            self.run_turns()
        self.assertEqual(self.bot_replies(), ['secret answer'])
        text = '\n'.join(logs.output)
        for word in ('secret', 'Ada', 'Example Team'):
            self.assertNotIn(word, text)


class OutsideTests(DirectTurnMacs, unittest.TestCase):
    def message(self, **fields):
        data = {'home': str(self.home), 'chat_id': self.direct_chat_id(), 'kind': 'send', 'message_id': 'outside:1',
                'text': 'The background job finished.', **fields}
        return self.only.gateway_message(data)

    def test_a_send_outside_a_turn_is_a_direct_message_once(self):
        self.assertEqual(self.message(), {'recorded': True})
        self.assertEqual(self.message(), {'recorded': True})
        self.assertEqual(self.bot_replies(), ['The background job finished.'])
        self.assertEqual(self.chat(), [])
        self.assertFalse(self.message(kind='edit', text='Changed.')['recorded'])

    def test_a_send_for_a_member_that_left_or_a_bot_without_its_chat_writes_nothing(self):
        self.leave(self.second, {'room': self.room})
        self.only.team_sync_pass()
        self.assertFalse(self.message(chat_id=self.direct_chat_id(self.grace))['recorded'])
        self.assertFalse(self.message(chat_id=self.direct_chat_id('f' * 64))['recorded'])
        self.connect(chat=False)
        self.assertFalse(self.message()['recorded'])
        self.assertEqual(self.bot_replies(), [])


class FailureTests(DirectTurnMacs, unittest.TestCase):
    def test_no_adapter_and_an_old_hermes_give_a_notice_in_the_chat(self):
        self.dm('Status?')
        with mock.patch.object(team_turns, 'ADAPTER_SECONDS', 0.2):
            self.run_turns()
        self.assertEqual(self.bot_replies(), [UNAVAILABLE])
        self.serve(reply('never'), lacking='trusted_inbound')
        self.dm('Status?')
        with self.assertLogs('coconut.team_turns', logging.WARNING):
            self.run_turns()
        self.assertEqual(self.bot_replies(), [UNAVAILABLE, OUTDATED])
        self.assertEqual((self.chat(), self.triggers()), ([], []))

    def test_a_failed_turn_an_empty_turn_a_late_trigger_and_a_fault_give_the_notice(self):
        gateway = self.serve(lambda turn: [{'kind': 'done', 'outcome': 'failure'}])
        self.dm('One')
        self.run_turns()
        gateway.script = lambda turn: [{'kind': 'done', 'outcome': 'success'}]
        self.dm('Two')
        self.run_turns()
        self.dm('Three')
        self.only.team_store.clock = lambda: time.time() + team_store.TRIGGER_SECONDS + 1
        self.run_turns()
        self.only.team_store.clock = time.time
        self.dm('Four')
        with mock.patch.object(self.only, '_team_turn', side_effect=ValueError('boom')), \
                self.assertLogs('coconut.team_turns', logging.ERROR):
            self.run_turns()
        self.assertEqual(self.bot_replies(), [UNFINISHED] * 4)
        self.assertEqual(self.chat(), [])

    def test_a_stop_in_a_turn_posts_the_notice_in_the_direct_chat_at_the_next_start(self):
        self.serve(lambda turn: [{'kind': 'typing'}])
        sent = self.dm('Status?')
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.only.stopping.set()
        self.only.gateway_bridge.close()
        runner.join(5)
        self.assertEqual(self.triggers(), [(sent['id'], 1)])
        self.gateway.stop()
        self.only.close()
        restarted = self.team_only_mac('only')
        restarted._turns_recover()
        self.assertEqual(self.bot_replies(), [UNFINISHED])
        self.assertEqual(self.chat(), [])

    def stopped(self, stop):
        """Start a turn that waits, run `stop`, and return the replies that the chat shows after it."""
        released = threading.Event()
        self.serve(lambda turn: (released.wait(5), reply('Late answer.')(turn))[1])
        runner = threading.Thread(target=self.only._turns_run, args=(self.room, self.bot['id']), daemon=True)
        with self.only._turns_lock:
            self.only._turns_running[(self.room, self.bot['id'])] = runner
        runner.start()
        deadline = time.monotonic() + 5
        while not self.gateway.turns and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(self.gateway.turns)
        stop()
        self.only.team_sync_pass()
        self.only._turns_check()
        self.assertEqual(self.only.gateway_bridge.turns[self.gateway.turns[0]['id']].state, 'stopping')
        released.set()
        runner.join(5)
        self.gateway.stop()

    def test_each_stop_ends_the_turn_without_a_word(self):
        # The bot that leaves gets a new `bot` entry when it comes back, so that case is the last one.
        cases = {
            'removed': lambda sent: self.phone(self.first)('DELETE', self.path(f'messages/{sent["id"]}'), {}),
            'chat off': lambda sent: self.connect(chat=False),
            'member leaves': lambda sent: self.leave(self.second, {'room': self.room}),
            'bot leaves': lambda sent: self.call('DELETE', f'/v1/team/rooms/{self.room}/bots/{self.bot["id"]}'),
        }
        for case, stop in cases.items():
            with self.subTest(case=case):
                service = self.second if case == 'member leaves' else self.first
                sent = self.dm(f'Please, {case}', service=service)
                self.stopped(lambda: stop(sent))
                self.assertEqual([row for row in self.stored() if row[0] == self.mac], [])
                self.connect(chat=True)
                self.passes()

    def test_a_leave_of_this_mac_stops_the_turn(self):
        self.dm('Please')
        self.stopped(lambda: self.leave(self.only, {'room': self.room}))
        self.assertEqual(self.bot_replies(), [])


class TypingTests(DirectTurnMacs, unittest.TestCase):
    def typing(self, service=None) -> int | None:
        service = service or self.first
        service.team_sync_pass()
        (chat,) = self.ask(service)['bot_directs']
        return chat['typing']

    def writer(self) -> team_typing.BotTyping:
        return team_typing.BotTyping(self.only, self.room, self.bot['id'], lambda: True, direct=self.ada)

    def test_the_sign_starts_again_and_ends_on_the_persons_mac_only(self):
        self.dm('Hello')
        writer = self.writer()
        cursor = self.first._cursor()
        self.assertTrue(writer.write(True))
        self.assertEqual(self.typing(), team_store.TYPING_BOT_SECONDS)
        self.assertTrue(self.first.updates(cursor, 0, collection_deltas=True)['team_typing_changed'])
        # The Team chat shows nothing, and Grace's Mac shows nothing.
        room = self.ask(self.first)
        self.assertEqual((room['typing'], [chat['typing'] for chat in room['chats']]), ([], []))
        self.second.team_sync_pass()
        self.assertEqual(self.ask(self.second)['bot_directs'], [])
        # One entry for the person and the bot: the sign replaces it, and no `typing` entry goes to the log.
        self.assertTrue(writer.write(True))
        token = json.loads(self.key_file(self.only, self.room).read_text())['token']
        entry = team_typing.direct_typing_entry(token, self.room, self.entry, self.ada)
        self.assertEqual(self.only.team_store.entry(self.room, entry)['rev'], 2)
        self.assertEqual(self.plaintext(self.only, entry), {'to': self.ada, 'bot': self.entry, 'on': True})
        with self.only.lock:
            kinds = {row[0] for row in self.only.db.execute('SELECT kind FROM team_entries WHERE member=?',
                                                            (self.mac,))}
        self.assertNotIn('typing', kinds)
        # A message of the bot does not end it, and `on` false does.
        self.only.gateway_message({'home': str(self.home), 'chat_id': self.direct_chat_id(), 'kind': 'send',
                                   'message_id': 'outside:1', 'text': 'A note.'})
        self.assertEqual(self.typing(), team_store.TYPING_BOT_SECONDS)
        self.assertTrue(writer.write(False))
        self.assertIsNone(self.typing())
        self.assertEqual(self.bot_replies(), ['A note.'])

    def test_an_old_sign_shows_nothing_and_a_sign_ends_by_time(self):
        self.dm('Hello')
        self.first.team_store.clock = lambda: time.time() + team_store.TYPING_FRESH_SECONDS + 1
        self.writer().write(True)
        self.assertIsNone(self.typing())
        self.first.team_store.clock = time.time
        self.writer().write(True)
        self.assertEqual(self.typing(), team_store.TYPING_BOT_SECONDS)
        self.first.team_store.clock = lambda: time.time() + team_store.TYPING_BOT_SECONDS
        self.assertIsNone(self.typing())

    def test_a_turn_writes_the_sign_while_the_adapter_shows_it(self):
        seen = []

        def script(turn):
            # An event of the turn shows it to the bridge, so the sign starts. The script then waits for the sign.
            yield {'kind': 'typing'}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.typing() is not None:
                    seen.append(True)
                    break
                time.sleep(.05)
            yield from reply('Done.')(turn)
        self.serve(script)
        self.dm('Hello')
        self.run_turns()
        self.assertEqual(seen, [True])
        deadline = time.monotonic() + 5
        while self.typing() is not None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNone(self.typing())
        with self.only.lock:
            kinds = {row[0] for row in self.only.db.execute('SELECT kind FROM team_entries WHERE member=?',
                                                            (self.mac,))}
        self.assertNotIn('typing', kinds)


class FromEndToEnd(DirectTurnMacs, unittest.TestCase):
    def test_ada_writes_the_bot_answers_and_grace_sees_nothing(self):
        phone = pair_phone(self.first)
        transport = RecordingTransport()
        self.first.push.transport = transport
        register(self.first, phone, level='grouped')
        self.serve(reply('3 tasks are open.'))
        self.dm('What is open?')
        self.run_turns()
        self.first.team_sync_pass()
        self.assertEqual(self.bot_chat(), [('What is open?', True), ('3 tasks are open.', False)])
        (chat,) = self.ask(self.first)['bot_directs']
        self.assertEqual((chat['bot'], chat['bot_name'], chat['unread'], chat['last_message']['text'], chat['typing']),
                         (self.bot_id, 'Reporter', 1, '3 tasks are open.', None))
        items = self.first.db.execute('SELECT kind, team, status FROM push_items').fetchall()
        self.assertEqual(items, [('direct', self.room, 'waiting')])
        flush(self.first)
        (call,) = transport.calls
        self.assertEqual(call[2]['coconut']['direct'], {'team': self.room, 'bot': self.bot_id})
        # Grace's Mac stores the entries and shows nothing.
        self.second.team_sync_pass()
        room = self.ask(self.second)
        self.assertEqual((room['bot_directs'], room['directs'], room['last_message']), ([], [], None))
        self.assertEqual(self.stored(self.second), [])


if __name__ == '__main__':
    unittest.main()
