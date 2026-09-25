"""Adapt native Hermes relay envelopes to Coconut's durable delivery receipts."""
import contextlib
import fcntl
import json
import os
import copy
from pathlib import Path


def message_tool_schema(native_schema):
    schema = copy.deepcopy(native_schema)
    schema['name'] = 'coconut_message_bot'
    schema['description'] += (
        ' To continue a failed task in this conversation, supply continuation_task_id from its saved task record. '
        'This preserves its execution session and worktree. It does not approve actions or change workspace ownership. '
        'Do not start another assignment for the same unfinished work.')
    schema['parameters']['properties']['continuation_task_id'] = {
        'type': 'string', 'minLength': 1, 'maxLength': 100,
        'description': 'The failed task owned by the target bot in this conversation.'}
    return schema


class NativeRelay:
    def __init__(self, request, receipt):
        self.request, self.receipt = request, receipt
        self.home = Path(os.environ['HERMES_HOME'])
        self.peers = {bot['id']: bot for bot in request['task'].get('_available_bots', [])}

    def install(self, conversation):
        import yaml
        from utils import atomic_yaml_write
        from tools.bot_mode_probe import BOT_CHAT_TITLE
        if __package__:
            from .native_conversation import resume_session, session_root
        else:
            from native_conversation import resume_session, session_root
        path = self.home / 'profile.yaml'
        meta = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        ui = dict(meta.get('ui_meta') or {})
        ui['hermes-bots'] = dict(ui.get('hermes-bots') or {}, title=self.request['bot'].get('name', self.request['bot']['id']))
        meta['ui_meta'] = ui
        atomic_yaml_write(self.home / 'profile.yaml', meta, sort_keys=False)
        conversation.db.create_session(conversation.session_id, source='coconut', model=self.request['model'])
        # Hermes reserves one Bot Chat per profile. Restarts preserve that conversation.
        # A prune of older ended sessions can remove the titled one. The recorded chat tip keeps the anchor.
        titled = conversation.db.resolve_session_by_title(BOT_CHAT_TITLE)
        canonical = (conversation.db.resolve_resume_session_id(titled) if titled
                     else resume_session(conversation.db, self.request['bot']['session_id']))
        conversation.db.create_session(canonical, source='coconut', model=self.request['model'])
        conversation.db.set_session_title(canonical, BOT_CHAT_TITLE)
        if conversation.session_id != canonical:
            execution = self.request['task'].get('_execution_session_id')
            root = session_root(self.request['bot'], execution=execution,
                                epoch=self.request['task'].get('_conversation_epoch'))
            conversation.db.set_session_title(conversation.session_id, ('Work ' if execution else 'Chat ') + root)
        self.sync_roster()

    def enqueue(self, arguments):
        from tools.bot_relay import enqueue_envelope, read_remote_roster, resolve_remote_target
        target = resolve_remote_target(arguments['target'], read_remote_roster(self.home))
        if not isinstance(target, dict) or target['connection_id'] not in self.peers:
            return json.dumps({'error': 'Choose a bot from the current contact list.'})
        text = arguments.get('message', '')
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            return json.dumps({'error': 'Write a message between 1 and 16000 characters.'})
        enqueue_envelope(self.home, target=target, message=text,
                         sender_profile=self.request['bot']['id'], sender_handle=self.request['bot']['id'])
        return json.dumps({'status': 'queued'})

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
        if __package__:
            from .native_turn_scope import stop_unconfirmed
        else:
            from native_turn_scope import stop_unconfirmed
        from tools.bot_relay import claim_pending_envelopes
        target = str(arguments.get('target', '')).lstrip('@').split('@')[0]
        if self.request['operation'] == 'context' or target not in self.peers:
            return json.dumps({'error': 'Choose the exact bot ID from the current contact list.'})
        with relay_lock(self.home):
            intent = dict(call_id=call_id, task_id=self.request['task']['id'], target_bot_id=target,
                          text=arguments['message'], previous_ids=[e['id'] for e in stored_envelopes(self.home)])
            if 'continuation_task_id' in arguments:
                intent['continuation_task_id'] = arguments['continuation_task_id']
            save_intent(self.home, intent)
            result = next_call(arguments)
            envelopes = claim_pending_envelopes(self.home)
            matching = [e for e in envelopes if e['id'] not in intent['previous_ids'] and e['target_connection'] == target]
            if len(matching) != 1:
                value = json.loads(result)
                if not matching and (value.get('error') or value.get('status') not in ('sent', 'queued')):
                    return result
                # Do not let native middleware turn an uncertain delivery into a success.
                stop_unconfirmed()
            intent['envelope'] = matching[0]
            save_intent(self.home, intent)
            confirmed = self.receipt('native_bot_message', delivery_payload(intent))
            if confirmed.get('status') not in ('queued', 'duplicate', 'paused', 'rejected'):
                stop_unconfirmed()
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
                target_bot_id=intent['target_bot_id'], text=intent['text'],
                **({key: intent[key] for key in ('continuation_task_id',) if key in intent}))


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
