#!/usr/bin/env python3
"""Exercise the real iOS chat against an isolated API with varied message heights."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xctestrun', type=Path, required=True)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--google-connection', action='store_true')
    parser.add_argument('--message-interactions', action='store_true')
    parser.add_argument('--inbox-choice', action='store_true')
    parser.add_argument('--decision-assessment', action='store_true')
    parser.add_argument('--inbox-stack', action='store_true')
    parser.add_argument('--inbox-link', action='store_true')
    parser.add_argument('--inbox-undo', action='store_true')
    parser.add_argument('--inbox-undo-order', action='store_true')
    parser.add_argument('--inbox-refresh', action='store_true')
    parser.add_argument('--inbox-uncertain', action='store_true')
    parser.add_argument('--inbox-stale', action='store_true')
    parser.add_argument('--inbox-revisit', action='store_true')
    parser.add_argument('--navigation', action='store_true')
    parser.add_argument('--footer-scroll', action='store_true')
    parser.add_argument('--exchange', action='store_true')
    parser.add_argument('--bot-references', action='store_true')
    parser.add_argument('--instant-send', action='store_true')
    parser.add_argument('--send-from-history', action='store_true')
    parser.add_argument('--keyboard-recovery', action='store_true')
    parser.add_argument('--keyboard-anchor', action='store_true')
    parser.add_argument('--latest-button', action='store_true')
    parser.add_argument('--decision-receipt', action='store_true')
    parser.add_argument('--transcript', type=Path, default=ROOT/'ios/CoconutUITests/Fixtures/ConversationLayoutMessages.json')
    args = parser.parse_args()
    if args.send_from_history:
        args.instant_send = True
        args.keyboard_recovery = True
    if args.decision_assessment or args.inbox_choice or args.inbox_link or args.inbox_undo or args.inbox_undo_order or args.inbox_refresh or args.inbox_uncertain or args.inbox_stale or args.inbox_revisit: args.inbox_stack = True
    google_fixture = tempfile.TemporaryDirectory(prefix='coconut-google-ui-')
    google_status = dict(connected=False, email=None, needs_reconnect=False, services=['gmail', 'calendar'])
    google_authorization = None
    state = json.loads((ROOT/'ios/Coconut/Fixtures/PreviewState.json').read_text())
    peer = dict(state['bots'][0], pinned=False, status='idle', unread_count=0)
    bot = state['bots'][1]
    bot.update(status='idle', unread_count=0, pinned=False)
    state.update(bots=[bot], messages=[], decisions=[], artifacts=[], tasks=[], routines=[], cursor=50)
    task = dict(id=str(uuid4()), bot_id=bot['id'], session_id=bot['session_id'], title='Conversation layout',
                status='completed', summary='', created_at=state['server_time'], updated_at=state['server_time'])
    task['thread_id'] = task['id']
    if args.instant_send: task['status'] = 'working'
    state['tasks'].append(task)
    def message(role, text):
        return dict(id=str(uuid4()), bot_id=bot['id'], task_id=task['id'], thread_id=task['id'], role=role,
                    kind='text', text=text, created_at=state['server_time'], streaming=False)
    paragraph = 'The plan keeps your instructions and prior decisions together. Each update continues the same conversation. '
    rows = json.loads(args.transcript.read_text())
    if args.keyboard_recovery:
        rows = [rows[index % len(rows)] for index in range(1000)]
    if args.keyboard_anchor or args.latest_button:
        rows = [dict(role='assistant', text=f'Message {index}. Keep this reading position.') for index in range(40)]
    state['messages'] = [message(row['role'], row['text']) for row in rows]
    latest = state['messages'][-1]
    latest['text'] = 'Latest response stays visible.\n\n' + latest['text']
    latest['streaming'] = True
    if args.message_interactions:
        state['messages'] = [message('user', 'Compare the two folders.'),
                             message('assistant', 'The blue folder has the complete report.'),
                             message('assistant', 'The green folder contains an older draft.')]
    if args.bot_references:
        state['bots'].append(peer)
        state['messages'] = [message('assistant', 'Ready for your message.')]
    if args.exchange:
        state['bots'].append(peer)
        own = message('assistant', 'My own work stays here.')
        outward = message('assistant', '**Check the report.**\n\n- Keep the useful findings.\n- Return a short answer.')
        outward.update(kind='bot_exchange', sender_bot_id=bot['id'], recipient_bot_ids=[peer['id']], created_at='2026-09-10T10:00:00Z')
        inward = message('assistant', '## Result\n\nThe check **passed**.\n\n```swift\nlet answer = 42\n```\n\n[Open reference](https://example.com)')
        inward.update(kind='text', bot_id=peer['id'], sender_bot_id=peer['id'], recipient_bot_ids=[], exchange_recipient_bot_ids=[bot['id']], created_at='2026-09-10T10:00:01Z')
        silent = message('assistant', '[SILENT]')
        silent_peer = dict(inward, id=str(uuid4()), text='[SILENT]')
        state['messages'] = [own, silent, outward, inward, silent_peer]
        bot['last_message'] = '[SILENT]'
    if args.decision_receipt:
        fixture = json.loads((ROOT/'ios/Coconut/Fixtures/PreviewState.json').read_text())
        decision = dict(fixture['decisions'][0], bot_id=bot['id'], task_id=task['id'],
                        title='Allow this action?', explanation='Choose how to continue.')
        task['status'] = 'needs_you'
        state['decisions'] = [decision]
        state['messages'] = []
    if args.footer_scroll:
        state['bots'] += [dict(bot, id=str(uuid4()), name=f'Bot {index:02}', pinned=False) for index in range(30)]
        state['messages'] = [message('assistant', f'History {index}. ' + paragraph * 3) for index in range(40)]
        latest = state['messages'][-1]
        latest['text'] = 'Latest response stays visible.'
        fixture = json.loads((ROOT/'ios/Coconut/Fixtures/PreviewState.json').read_text())
        state['decisions'] = [dict(fixture['decisions'][0], bot_id=bot['id'], task_id=task['id'],
                                  context=paragraph * 20)]
    if args.navigation:
        state.update(bots=[dict(bot, id=str(uuid4()), name=f'Bot {index:02}', pinned=False) for index in range(30)],
                     messages=[], decisions=[], tasks=[])
    if args.inbox_stack:
        fixture = json.loads((ROOT/'ios/Coconut/Fixtures/PreviewState.json').read_text())
        titles = ['Publish the release notes?', 'Keep the retry fix?', 'Review the onboarding copy?']
        state.update(messages=[], decisions=[dict(fixture['decisions'][0],
            id=f'00000000-0000-4000-8000-00000000004{index}', bot_id=bot['id'], task_id=task['id'],
            title=title, context=[
                'The release notes describe the new Inbox. The draft is ready for review.',
                'The retry fix passes all checks. It is ready to save.',
                'The setup instructions still describe the old Inbox. A copy review could find gaps.'
            ][index],
            options=[dict(id=f'option-{index}-{number}', label=label, answer='approve', explanation=effect)
                     for number, (label, effect) in enumerate([
                         [('Publish', 'Publish these notes on the public release page.'),
                          ('Review draft', 'Open the draft for review before publishing.'),
                          ('Revise wording', 'Prepare a shorter draft and ask for review.'),
                          ('Do nothing', 'Keep the draft private and leave the release page unchanged.')],
                         [('Keep fix', 'Save the checked retry fix.'), ('Discard fix', 'Leave the current retry behavior unchanged.')],
                         [('Review copy', 'Check the setup text and propose edits.'), ('Not now', 'Leave the setup text unchanged.')]
                     ][index])],
            recommended_option_id=f'option-{index}-0' if index == 1 else None,
            explanation='', resource='', consequences='', proposal_preview=None,
            evidence='The draft matches the shipped changes.' if index == 0 else '',
            risk='Publishing makes these notes visible to everyone.' if index == 0 else '',
            inbox_kind='proposal' if index == 2 else 'decision', status='pending')
            for index, title in enumerate(titles)])
        original = message('user', 'Prepare release notes for the Inbox update.')
        state['messages'] = [original]
        for decision in state['decisions']:
            decision.update(request_bot_id=bot['id'], request_message_id=original['id'])
        if args.inbox_link:
            # Bots often keep writing after a card. A long message below the selected card fills the screen.
            state['messages'] += [message('assistant', paragraph * 12), message('assistant', 'Latest note after the cards.')]
    if args.decision_assessment:
        state['decisions'] = state['decisions'][:1]
        decision = state['decisions'][0]
        decision.update(title='Publish the release notes?', context='This publishes the checked notes on the release page.',
            risk_level='medium', risk='Customers can read an incorrect date.',
            uncertainty='The release time remains undecided.')
        decision['options'] = decision['options'][:2]
        decision['options'][1].update(risk_level='low', risk='Only the private draft changes.')
    inbox_actions = []
    inbox_keys = []
    inbox_receipts = {}
    answer_calls = []
    dismissal_failed = False
    decision_attempts = 0
    changed = threading.Condition()
    events = []
    sends = 0
    streaming = threading.Event()
    release_send = threading.Event()
    release_refresh = threading.Event()
    stale_requested = threading.Event()
    stale_started = threading.Event()
    release_stale = threading.Event()
    send_keys = []
    send_modes = []
    sent_requests = []
    rejected_keys = set()
    responses = {}

    def publish(kind, row):
        state['cursor'] += 1
        events.append(dict(seq=state['cursor'], type=kind, bot_id=bot['id'], task_id=task['id'],
                           created_at=state['server_time'], data=dict(row)))
        changed.notify_all()

    def reply(number):
        if streaming.wait(0.4): return
        with changed:
            row = message('assistant', '')
            row['streaming'] = True
            state['messages'].append(row)
            publish('message.created', row)
        for chunk in ['Reply ', 'after ', 'message ', f'{number}.']:
            if streaming.wait(0.3): return
            with changed:
                row['text'] += chunk
                publish('message.updated', row)
        with changed:
            row['streaming'] = False
            task['status'] = 'completed'
            publish('message.updated', row)
            publish('task.updated', task)

    def stream():
        for index in range(36):
            if streaming.wait(0.15): return
            with changed:
                latest['text'] += '\n\n' + paragraph if index % 6 == 0 else ' More detail.'
                state['cursor'] += 1
                events.append(dict(seq=state['cursor'], type='message.updated', bot_id=bot['id'],
                                   task_id=task['id'], created_at=latest['created_at'], data=dict(latest)))
                changed.notify_all()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_POST(self):
            nonlocal sends, decision_attempts, dismissal_failed, google_authorization
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/v1/pair':
                self.respond(dict(token='layout-fixture-only', device_id=str(uuid4())))
            elif args.google_connection and self.path == '/v1/settings/google/start':
                sys.path.insert(0, str(ROOT / 'backend'))
                from coconut.google_connection import GoogleConnections, GoogleProfile
                manager = GoogleConnections(google_fixture.name, profile_factory=lambda home: GoogleProfile(home, executable='/usr/bin/true'))
                google_authorization = manager.start('fixture-phone', str(uuid4()))
                self.respond(google_authorization)
            elif args.google_connection and self.path == '/v1/settings/google/finish':
                if body.get('state') != google_authorization['state'] or body.get('code') != 'fixture-code':
                    self.respond(dict(error='Invalid fixture callback'), status=400)
                    return
                google_status.update(connected=True, email='fixture@example.com')
                self.respond(google_status)
            elif args.google_connection and self.path == '/v1/settings/google/disconnect':
                google_status.update(connected=False, email=None)
                self.respond(google_status)
            elif args.exchange and self.path == '/test/exchange-history':
                with changed:
                    for index in range(35):
                        row = message('assistant', f'Older exchange {index:02}. ' + 'This earlier result remains available for context. ' * 5)
                        row.update(kind='bot_exchange', sender_bot_id=bot['id'], recipient_bot_ids=[peer['id']],
                                   created_at=f'2026-09-09T10:{index:02}:00Z')
                        state['messages'].append(row)
                        publish('message.created', row)
                self.respond(dict(ok=True))
            elif args.exchange and self.path == '/test/exchange':
                with changed:
                    row = message('assistant', 'A new peer reply.')
                    row.update(bot_id=peer['id'], sender_bot_id=peer['id'], recipient_bot_ids=[],
                               exchange_recipient_bot_ids=[bot['id']], created_at='2026-09-10T10:00:02Z', streaming=True)
                    state['messages'].append(row)
                    publish('message.created', row)
                    row['streaming'] = False
                    publish('message.updated', row)
                    publish('task.updated', task)
                self.respond(dict(ok=True))
            elif args.decision_assessment and self.path == '/test/assessment-stale':
                with changed:
                    state['decisions'][0]['assessment_stale'] = True
                    publish('decision.updated', state['decisions'][0])
                self.respond(dict(ok=True))
            elif self.path == '/test/start-stale':
                stale_requested.set()
                with changed: publish('task.updated', task)
                self.respond(dict(ok=True))
            elif self.path == '/test/release-stale':
                release_stale.set()
                self.respond(dict(ok=True))
            elif self.path == '/test/release-send':
                release_send.set()
                self.respond(dict(ok=True))
            elif args.inbox_stack and self.path.endswith('/inbox'):
                decision = next(d for d in state['decisions'] if d['id'] == self.path.split('/')[-2])
                action = body['action']
                inbox_actions.append(action)
                key = self.headers.get('Idempotency-Key')
                inbox_keys.append(key)
                if args.inbox_uncertain and key in inbox_receipts:
                    self.respond(inbox_receipts[key])
                    return
                if len(inbox_actions) == 1 and not args.inbox_refresh and not args.inbox_stale: release_send.wait(15)
                if action == 'dismiss' and not dismissal_failed and not args.inbox_uncertain:
                    dismissal_failed = True
                    self.respond(dict(error='Could not dismiss. Try again.'), status=503)
                    return
                with changed:
                    if action == 'restore':
                        release_refresh.set()
                        if not body.get('swipe_id') or body['swipe_id'] != decision.get('inbox_swipe_id'):
                            self.respond(dict(error='The swipe changed.'), status=409)
                            return
                        decision.update(status='pending', inbox_deferred_at=None, inbox_undo_until=None, inbox_swipe_id=None)
                    else:
                        decision['inbox_swipe_id'] = str(uuid4())
                        decision['inbox_undo_until'] = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
                        if action == 'dismiss': decision['status'] = 'dismissed'
                        else: decision['inbox_deferred_at'] = state['server_time']
                    publish('decision.updated', decision)
                if args.inbox_uncertain:
                    inbox_receipts[key] = copy.deepcopy(decision)
                    if len(inbox_actions) == 1:
                        self.respond({'incomplete': True})
                        return
                self.respond(decision)
            elif args.message_interactions and self.path.endswith('/reactions'):
                row = next(m for m in state['messages'] if m['id'] == self.path.split('/')[-2])
                values = row.setdefault('reactions', [])
                if body['active'] and body['emoji'] not in values: values.append(body['emoji'])
                if not body['active'] and body['emoji'] in values: values.remove(body['emoji'])
                with changed: publish('message.updated', row)
                self.respond(row)
            elif args.inbox_choice and self.path.endswith('/answer'):
                answer_calls.append(body)
                release_send.wait(20)
                decision = next(d for d in state['decisions'] if d['id'] == self.path.split('/')[-2])
                if decision['id'].endswith('41'):
                    self.respond(dict(error='The decision changed. Review it again.'), status=409)
                    return
                with changed:
                    option = next(o for o in decision['options'] if o['id'] == body['option_id'])
                    decision.update(status='approved', selected_option_id=option['id'], selected_option_label=option['label'])
                    publish('decision.updated', decision)
                self.respond(decision)
            elif args.inbox_stack and self.path.endswith('/answer'):
                answer_calls.append(body)
                self.respond(dict(error='The swipe must not answer.'), status=409)
            elif args.decision_receipt and self.path.endswith('/answer'):
                decision_attempts += 1
                if decision_attempts == 1:
                    self.respond(dict(error='Temporary test failure.'), status=503)
                    return
                release_send.wait(30)
                with changed:
                    option = next(o for o in decision['options'] if o['id'] == body['option_id'])
                    decision.update(status='approved', selected_option_id=option['id'], selected_option_label=option['label'],
                                    decided_by='fixture', outcome='Answer recorded. Hermes will continue.')
                    publish('decision.updated', decision)
                self.respond(decision)
            elif args.message_interactions and self.path.endswith('/messages'):
                target = next(m for m in state['messages'] if m['id'] == body['reply_to_message_id'])
                row = message('user', body['text'])
                row.update(client_request_id=self.headers.get('Idempotency-Key'),
                           reply_to=dict(message_id=target['id'], text=target['text'], author=bot['name'], role=target['role']))
                with changed: state['messages'].append(row); publish('message.created', row)
                self.respond(task)
            elif self.path.endswith('/messages'):
                sent_requests.append(dict(path=self.path, body=body))
                key = self.headers.get('Idempotency-Key')
                if args.instant_send:
                    send_keys.append(key)
                    send_modes.append(body.get("mode"))
                    if args.send_from_history or body['text'] == 'Immediate message.':
                        release_send.wait(20)
                    if body['text'] == 'Retry this message.' and key not in rejected_keys:
                        rejected_keys.add(key)
                        self.respond(dict(error='Temporary test failure.'), status=503)
                        return
                    if body['text'] == 'Rejected message.':
                        self.respond(dict(error='The selected task has finished.'), status=409)
                        return
                    if key in responses:
                        self.respond(responses[key])
                        return
                with changed:
                    sends += 1
                    task['status'] = 'working'
                    publish('task.updated', task)
                    row = message('user', body['text'])
                    row['client_request_id'] = key
                    responses[key] = dict(task)
                    state['messages'].append(row)
                    publish('message.created', row)
                threading.Thread(target=reply, args=(sends,), daemon=True).start()
                self.respond(task)
            else: self.respond(dict(ok=True))

        def do_PUT(self): self.respond(dict(registered=True, configured=False))
        def do_DELETE(self): self.respond(dict(registered=False, configured=False))
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == '/test/stale-ready':
                self.respond(dict(ready=stale_started.wait(3)))
                return
            if args.inbox_stale and url.path == '/v1/state' and stale_requested.is_set():
                if not stale_started.is_set():
                    with changed: stale_value = copy.deepcopy(state)
                    stale_started.set()
                    release_stale.wait(15)
                    self.respond(stale_value)
                    return
                release_refresh.wait(15)
            if url.path == '/test/inbox-status':
                if args.inbox_refresh: release_refresh.wait(3)
                self.respond(dict(actions=inbox_actions, keys=inbox_keys, answers=answer_calls))
                return
            if url.path == '/test/send-status':
                self.respond(dict(keys=send_keys, sends=sends, modes=send_modes, requests=sent_requests))
                return
            if args.inbox_refresh and inbox_actions and url.path == '/v1/state':
                release_refresh.wait(15)
            with changed:
                if url.path == '/v1/events':
                    after = int(parse_qs(url.query).get('after', ['0'])[0])
                    if state['cursor'] <= after: changed.wait(1)
                    value = dict(events=[e for e in events if e['seq'] > after], cursor=state['cursor'], server_time=state['server_time'])
                elif args.google_connection and url.path == '/v1/settings/google': value = dict(google_status)
                elif url.path == '/v1/state': value = copy.deepcopy(state)
                else: value = dict(ok=True)
            self.respond(value)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = args.xctestrun.resolve()
        def absolute(value):
            if isinstance(value, str): return value.replace('__TESTROOT__', str(source.parent))
            if isinstance(value, list): return [absolute(v) for v in value]
            if isinstance(value, dict): return {k: absolute(v) for k, v in value.items()}
            return value
        document = absolute(plistlib.loads(source.read_bytes()))
        targets = ([t for c in document['TestConfigurations'] for t in c['TestTargets']]
                   if 'TestConfigurations' in document else
                   [v for v in document.values() if isinstance(v, dict)])
        for target in targets:
            if target.get('BlueprintName') == 'CoconutUITests':
                target.setdefault('EnvironmentVariables', {})['COCONUT_LAYOUT_TEST_URL'] = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory(prefix='coconut-layout-test-') as folder:
            runfile = Path(folder)/'Layout.xctestrun'
            runfile.write_bytes(plistlib.dumps(document))
            command = ['xcodebuild', 'test-without-building', '-xctestrun', str(runfile),
                            '-destination', 'platform=iOS Simulator,id='+args.simulator,
                            '-only-testing:CoconutUITests/' + ('DecisionInboxTests/testAssessmentInInboxAndChatUpdatesWhenSourcesChange' if args.decision_assessment else 'FooterScrollTests' if args.footer_scroll else 'SendFromHistoryTests/testSendingFromHistoryShowsMessageBeforeServerAcceptance' if args.send_from_history else 'BotMentionsFlowTests/testMentionKeepsOwnerOnWire' if args.bot_references else 'GoogleConnectionFlowTests/testSystemSignInReturnsToCoconut' if args.google_connection else 'MessageInteractionFlowTests/testRepliesReactionsAndCopy' if args.message_interactions else 'DecisionInboxTests/testChoicesAdvanceBeforeConfirmationAndRecoverFailure' if args.inbox_choice else 'DecisionInboxTests/testReopeningFromPendingListEndsUndoBeforeDelayedReceipt' if args.inbox_revisit else 'DecisionInboxTests/testOldSnapshotCannotRemoveConfirmedUndo' if args.inbox_stale else 'DecisionInboxTests/testUndoRecoversAnUncertainSwipeWithTheSameKey' if args.inbox_uncertain else 'DecisionInboxTests/testUndoSendsRestoreWhileSnapshotRefreshIsBlocked' if args.inbox_refresh else 'DecisionInboxTests/testUndoTargetsLatestSwipeWhenEarlierWriteFinishesLast' if args.inbox_undo_order else 'DecisionInboxTests/testUndoDuringPendingSwipeKeepsCardAndHeaderStable' if args.inbox_undo else 'DecisionInboxTests/testOpenChatShowsSelectedCard' if args.inbox_link else 'DecisionInboxTests/testSwipeStackKeepsDecisionsAndSupportsUndo' if args.inbox_stack else 'DecisionInboxTests/testNativeNavigation' if args.navigation else 'NativeDecisionFlowTests/testChoiceReceiptStaysStableDuringServerConfirmation' if args.decision_receipt else 'BotExchangeFlowTests' if args.exchange else 'InstantSendFlowTests' if args.instant_send else 'ConversationSendRecoveryTests' if args.keyboard_recovery else 'ConversationLayoutFlowTests/testKeyboardKeepsBottomPinnedAndPreservesHistory' if args.keyboard_anchor else 'ConversationLayoutFlowTests/testLatestButtonReturnsToLastMessage' if args.latest_button else 'ConversationLayoutFlowTests'), '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', str(args.result)]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    if 'COCONUT_GOOGLE_STAGE=callback' in line and google_authorization:
                        callback = google_authorization['callback_scheme'] + ':/oauthredirect?state=' + google_authorization['state'] + '&code=fixture-code'
                        subprocess.run(['xcrun', 'simctl', 'openurl', args.simulator, callback], check=True)
                    if 'COCONUT_LAYOUT_STAGE=stream' in line:
                        threading.Thread(target=stream, daemon=True).start()
                result = process.wait()
            if result: raise SystemExit(result)
    finally:
        google_fixture.cleanup()
        streaming.set()
        release_send.set()
        server.shutdown()
        server.server_close()


if __name__ == '__main__': main()
