"""Adapt native Hermes relay envelopes to Coconut's durable delivery receipts."""
import contextlib
import fcntl
import json
import os
from pathlib import Path


class NativeRelay:
    def __init__(self, request, receipt):
        self.request, self.receipt = request, receipt
        self.home = Path(os.environ['HERMES_HOME'])
        self.peers = {bot['id']: bot for bot in request['task'].get('_available_bots', [])}
        budget = request['task'].get('_handoff_budget_remaining', 0)
        self.remaining = max(0, min(8, budget)) if isinstance(budget, int) else 0

    def install(self, conversation):
        import yaml
        from utils import atomic_yaml_write
        from tools.bot_mode_probe import BOT_CHAT_TITLE
        path = self.home / 'profile.yaml'
        meta = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        ui = dict(meta.get('ui_meta') or {})
        ui['hermes-bots'] = dict(ui.get('hermes-bots') or {}, title=self.request['bot'].get('name', self.request['bot']['id']))
        meta['ui_meta'] = ui
        atomic_yaml_write(self.home / 'profile.yaml', meta, sort_keys=False)
        conversation.db.create_session(conversation.session_id, source='coconut', model=self.request['model'])
        conversation.db.set_session_title(conversation.session_id, BOT_CHAT_TITLE)
        self.sync_roster()

    def sync_roster(self):
        from tools.bot_relay import write_remote_roster
        write_remote_roster(self.home, [dict(connection_id=bot['id'], connection_label=bot['name'],
                                           profile='default', handle=bot['id'], title=bot['name'],
                                           description=bot.get('role', ''), online=True)
                                       for bot in self.peers.values()])

    def add_peer(self, bot):
        self.peers[bot['bot_id']] = dict(id=bot['bot_id'], name=bot['name'])
        self.sync_roster()

    def remove_peer(self, identity):
        self.peers.pop(identity, None)
        self.sync_roster()

    def deliver(self, arguments, next_call, call_id):
        from tools.bot_relay import claim_pending_envelopes
        target = str(arguments.get('target', '')).lstrip('@').split('@')[0]
        if self.request['operation'] == 'context' or target not in self.peers:
            return json.dumps({'error': 'Choose the exact bot ID from the current contact list.'})
        if self.remaining <= 0:
            return json.dumps({'status': 'limit_reached', 'error': 'This exchange has no remaining automatic deliveries.'})
        with relay_lock(self.home):
            intent = dict(call_id=call_id, task_id=self.request['task']['id'], target_bot_id=target,
                          text=arguments['message'], previous_ids=[e['id'] for e in stored_envelopes(self.home)])
            save_intent(self.home, intent)
            result = next_call(arguments)
            envelopes = claim_pending_envelopes(self.home)
            matching = [e for e in envelopes if e['id'] not in intent['previous_ids'] and e['target_connection'] == target]
            if len(matching) != 1:
                value = json.loads(result)
                if not matching and (value.get('error') or value.get('status') not in ('sent', 'queued')):
                    return result
                # Do not let native middleware turn an uncertain delivery into a success.
                os._exit(74)
            intent['envelope'] = matching[0]
            save_intent(self.home, intent)
            confirmed = self.receipt('native_bot_message', delivery_payload(intent))
            if confirmed.get('status') not in ('queued', 'duplicate', 'paused', 'limit_reached'):
                os._exit(74)
            if confirmed['status'] == 'limit_reached':
                self.remaining = 0
            elif confirmed['status'] != 'duplicate':
                self.remaining -= 1
            return json.dumps(confirmed, sort_keys=True)


@contextlib.contextmanager
def relay_lock(home):
    directory = Path(home) / '.coconut-relay'
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / 'transport.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_intent(home, intent):
    from profiles import _write
    directory = Path(home) / '.coconut-relay'
    _write(directory / (intent['call_id'] + '.json'), json.dumps(intent), Path(home))
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stored_envelopes(home):
    # Hermes defines these files as the public transport interface for relay clients.
    result = []
    for folder in ('outbox', 'claimed'):
        for path in (Path(home) / 'bot_relay' / folder).glob('*.json'):
            result.append(json.loads(path.read_text()))
    return result


def delivery_payload(intent):
    envelope = intent['envelope']
    return dict(native_call_id=intent['call_id'], native_envelope_id=envelope['id'],
                target_bot_id=intent['target_bot_id'], text=intent['text'])


def recover_delivery(home, call_id):
    with relay_lock(home):
        path = Path(home) / '.coconut-relay' / (call_id + '.json')
        if not path.exists():
            return None
        intent = json.loads(path.read_text())
        if 'envelope' not in intent:
            matches = [e for e in stored_envelopes(home) if e['id'] not in intent['previous_ids']
                       and e['target_connection'] == intent['target_bot_id']]
            if len(matches) != 1:
                return None
            intent['envelope'] = matches[0]
            save_intent(home, intent)
        return delivery_payload(intent)


def write_native_reply(home, envelope_id, text, failed=False):
    from tools.bot_relay import write_reply
    write_reply(home, envelope_id, reply='' if failed else text, error=text if failed else '')
