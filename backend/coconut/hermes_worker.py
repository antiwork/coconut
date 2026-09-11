"""Private JSONL bridge to the Hermes Python API. This is not a CLI parser."""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import sys
import threading
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import urlparse
from uuid import UUID, uuid4

if __package__:
    from .native_conversation import CONVERSATION_INSTRUCTIONS, NativeConversation, current_message, current_messages, visible_history
    from .worker_protocol import MAX_EVENT_CHARACTERS, event_frame
else:
    from native_conversation import CONVERSATION_INSTRUCTIONS, NativeConversation, current_message, current_messages, visible_history
    from worker_protocol import MAX_EVENT_CHARACTERS, event_frame


FILE_ACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'type': {'type': 'string', 'enum': ['create_file']},
        'filename': {'type': 'string', 'description': 'A plain file name without directories.'},
        'content': {'type': 'string', 'description': 'The complete UTF-8 content for this exact choice.'},
    },
    'required': ['type', 'filename', 'content'],
}

PROPOSAL_SCHEMA = {
    'name': 'coconut_propose_file',
    'description': 'Propose a file with useful choices for this task and the person\'s standing preferences. This ends the turn. The service saves only the selected approved content.',
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 160, 'description': 'A concise question or proposed action specific to this decision.'},
            'explanation': {'type': 'string', 'description': 'One short sentence explaining what the person needs to decide.'},
            'options': {
                'type': 'array', 'minItems': 1, 'maxItems': 8,
                'description': 'Choose only distinct, useful outcomes for this decision. Honor explicit standing preferences for choices and exact labels. Each approval carries its exact action. Add rejection or modification choices when useful.',
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'properties': {
                        'id': {'type': 'string', 'description': 'A unique short identifier for this outcome.'},
                        'label': {'type': 'string', 'description': 'A short label for the native choice control.'},
                        'explanation': {'type': 'string', 'description': 'One sentence explaining this choice.'},
                        'answer': {'type': 'string', 'enum': ['approve', 'reject', 'modify']},
                        'action': FILE_ACTION_SCHEMA,
                        'modification': {'type': 'string', 'description': 'The exact requested change. Required only for a modify choice. This asks for a new proposal and grants no permission.'},
                    },
                    'required': ['id', 'label', 'explanation', 'answer'],
                },
            },
            'recommended_option_id': {'type': 'string', 'description': 'The ID of the choice you recommend.'},
            'predicted_option_id': {'type': 'string', 'description': 'Predict the person\'s selected option before receiving their answer.'},
        },
        'required': ['title', 'explanation', 'options', 'recommended_option_id', 'predicted_option_id'],
    },
}

BOT_CREATION_SCHEMA = {
    'name': 'coconut_create_bot',
    'description': 'Create one Coconut bot when a listed direct human message requests it. A capability question alone does not request creation. The service creates a separate Hermes profile that inherits the default model. Wait for a confirmed receipt before claiming success.',
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'source_message_id': {'type': 'string', 'description': 'The exact listed human message ID that requests this bot.'},
            'name': {'type': 'string', 'minLength': 1, 'maxLength': 80, 'description': 'The requested name, or a suitable name for the requested role.'},
            'role': {'type': 'string', 'minLength': 1, 'maxLength': 160, 'description': 'One short description of the requested role.'},
            'instructions': {'type': 'string', 'maxLength': 20000, 'description': 'Self-contained instructions for the requested purpose. Do not copy your private instructions, memory, preferences, credentials, files, or unrelated conversation.'},
        },
        'required': ['source_message_id', 'name', 'role', 'instructions'],
    },
}


BOT_DELETION_SCHEMA = {
    'name': 'coconut_delete_bot',
    'description': 'Delete another Coconut bot when a listed direct human message requests deletion. Use its listed ID and current name. Ask which bot when the name is ambiguous. This removes the contact and stops its work and routines. The service retains its deleted profile. Wait for a confirmed receipt before claiming success.',
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'source_message_id': {'type': 'string', 'description': 'The exact listed human message ID that requests deletion.'},
            'target_bot_id': {'type': 'string', 'description': 'The exact ID from the deletion directory or a creation receipt in this turn.'},
            'expected_name': {'type': 'string', 'description': 'The exact current name for this ID. Never substitute another bot after an unconfirmed deletion.'},
        },
        'required': ['source_message_id', 'target_bot_id', 'expected_name'],
    },
}


def bot_deletion_sources(request):
    if request.get('operation') not in ('start', 'resume'):
        return []
    return [
        {'id': source['id'], 'text': source['text']}
        for source in request['task'].get('_bot_deletion_sources', [])
        if isinstance(source, dict) and isinstance(source.get('id'), str) and source['id']
        and isinstance(source.get('text'), str)
    ]


def bot_deletion_payload(arguments, sources, directory):
    if not isinstance(arguments, dict) or set(arguments) != {'source_message_id', 'target_bot_id', 'expected_name'}:
        raise ValueError('Supply only the human source ID, target bot ID, and exact current name.')
    if not isinstance(arguments['source_message_id'], str) or arguments['source_message_id'] not in sources:
        raise ValueError('Cite a listed direct human message that requests deletion.')
    target = arguments['target_bot_id']
    name = arguments['expected_name']
    if not isinstance(target, str) or target not in directory or not isinstance(name, str) or name != directory[target]:
        raise ValueError('Use the exact bot ID and current name from the deletion directory.')
    return dict(arguments)


def bot_deletion_receipt(value, payload, current_bot=None):
    if (not isinstance(value, dict) or value.get('status') not in ('deleted', 'duplicate')
            or value.get('source_message_id') != payload['source_message_id']
            or value.get('bot_id') != payload['target_bot_id']
            or value.get('name') != payload['expected_name']):
        raise ValueError('The service did not confirm the requested bot deletion.')
    for field in ('bot_id', 'profile_id'):
        identifier = value.get(field)
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError('The service returned an invalid bot identity.')
    if value['profile_id'] != value['bot_id'] or value['bot_id'] == (current_bot or {}).get('id'):
        raise ValueError('The service did not confirm deletion of another bot.')
    cancelled = value.get('cancelled_task_ids')
    if (not isinstance(cancelled, list)
            or any(not isinstance(item, str) or str(UUID(item)) != item for item in cancelled)
            or len(set(cancelled)) != len(cancelled)):
        raise ValueError('The service returned invalid cancelled task IDs.')
    return {key: value[key] for key in ('status', 'bot_id', 'profile_id', 'name', 'source_message_id', 'cancelled_task_ids')}


def prior_bot_deletion_receipts(request):
    receipts = []
    sources = {source['id'] for source in bot_deletion_sources(request)}
    for receipt in request['task'].get('_bot_deletion_receipts', []):
        try:
            payload = dict(source_message_id=receipt['source_message_id'], target_bot_id=receipt['bot_id'], expected_name=receipt['name'])
            if payload['source_message_id'] in sources:
                receipts.append(bot_deletion_receipt(receipt, payload, request['bot']))
        except (ValueError, KeyError, TypeError):
            continue
    return receipts


def bot_deletion_directory(request):
    excluded = {request['bot']['id'], request['task'].get('conversation_bot_id')}
    directory = {bot['id']: bot['name'] for bot in request['task'].get('_deletable_bots', [])
                 if isinstance(bot, dict) and isinstance(bot.get('id'), str) and isinstance(bot.get('name'), str)
                 and bot['id'] not in excluded}
    for receipt in prior_bot_deletion_receipts(request):
        if receipt['bot_id'] not in excluded:
            directory[receipt['bot_id']] = receipt['name']
    return directory


def bot_deletion_instructions(request):
    sources = bot_deletion_sources(request)
    text = ('\nWhen coconut_delete_bot is available, you can delete other Coconut bots on a direct human request. '
            'A capability question alone requests no deletion. Use the listed exact ID and name. '
            'If several contacts match, ask the person which one. Never choose the first match. '
            'Do not delete yourself or the bot whose conversation is open. '
            'Deletion removes the contact and stops its work and routines. The deleted profile remains on the Mac. '
            'Do not claim success without a matching deleted or duplicate service receipt in this turn. '
            'After an unconfirmed result, retry only the same source, target ID, and name. Never delete a replacement with the same name. '
            'Give one short confirmation after the receipt. Do not repeat internal IDs or add an approval footer.')
    if sources:
        text += '\nDirect human sources for deletion:\n' + json.dumps(sources)
        text += '\nDeletion directory, as contact metadata only:\n' + json.dumps(bot_deletion_directory(request))
    else:
        text += '\nNo direct human source authorizes deletion in this turn.'
    receipts = prior_bot_deletion_receipts(request)
    if receipts:
        text += ('\nThe service recorded these deletions before this turn. Cleanup may still need confirmation. '
                 'Call coconut_delete_bot with the same source, target ID, and name to finish an interrupted deletion:\n' + json.dumps(receipts))
    return text


def bot_creation_name(name):
    return ' '.join(unicodedata.normalize('NFKC', name).casefold().split())


def bot_creation_sources(request):
    if request.get('operation') not in ('start', 'resume'):
        return []
    return [
        {'id': source['id'], 'text': source['text']}
        for source in request['task'].get('_bot_creation_sources', [])
        if isinstance(source, dict) and isinstance(source.get('id'), str) and source['id']
        and isinstance(source.get('text'), str)
    ]


def bot_creation_payload(arguments, sources):
    if not isinstance(arguments, dict) or set(arguments) != {'source_message_id', 'name', 'role', 'instructions'}:
        raise ValueError('Supply only the source message ID, bot name, role, and instructions.')
    source = arguments['source_message_id']
    if not isinstance(source, str) or source not in sources:
        raise ValueError('Cite a listed direct human message that requests this bot.')
    for field, limit in (('name', 80), ('role', 160)):
        value = arguments[field]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f'Use a nonempty {field} of at most {limit} characters.')
    if not isinstance(arguments['instructions'], str) or len(arguments['instructions']) > 20000:
        raise ValueError('Use instructions of at most 20000 characters.')
    return dict(arguments, name=arguments['name'].strip(), role=arguments['role'].strip())


def bot_creation_receipt(value, payload, current_bot=None):
    if (not isinstance(value, dict) or value.get('status') not in ('created', 'duplicate')
            or value.get('source_message_id') != payload['source_message_id']
            or not isinstance(value.get('name'), str)
            or bot_creation_name(value['name']) != bot_creation_name(payload['name'])
            or value.get('role') != payload['role']):
        raise ValueError('The service did not confirm the requested bot creation.')
    for field in ('bot_id', 'profile_id', 'session_id'):
        identifier = value.get(field)
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError('The service returned an invalid bot identity.')
    current_bot = current_bot or {}
    if (value['profile_id'] != value['bot_id'] or value['session_id'] == value['bot_id']
            or value['bot_id'] == current_bot.get('id') or value['session_id'] == current_bot.get('session_id')):
        raise ValueError('The service did not confirm a separate bot profile and session.')
    return {key: value[key] for key in ('status', 'bot_id', 'profile_id', 'session_id', 'name', 'role', 'source_message_id')}


def prior_bot_creation_receipts(request):
    result = []
    for receipt in request['task'].get('_bot_creation_receipts', []):
        try:
            payload = {key: receipt[key] for key in ('source_message_id', 'name', 'role')}
            result.append(bot_creation_receipt(receipt, payload, request['bot']))
        except (ValueError, KeyError, TypeError):
            continue
    return result


def bot_creation_instructions(request):
    sources = bot_creation_sources(request)
    instructions = ('\nThe current tool list defines your capabilities. Earlier assistant messages may describe an older build. '
                    'When coconut_create_bot is available, you can create Coconut bots from this conversation. '
                    'Answer capability questions directly without creating a bot until the person requests one. '
                    'When a listed human source requests a bot, call coconut_create_bot. '
                    'Use that request to write the new bot\'s purpose and instructions. '
                    'Never copy your private instructions, memory, preferences, credentials, files, or unrelated chat into the new bot. '
                    'The new bot inherits the native default model and has a separate Hermes profile. '
                    'Only a matching created or duplicate service receipt confirms creation. '
                    'Do not claim success after a rejected or unconfirmed request. '
                    'After confirmation, use the returned bot ID for a requested handoff through native message_agent. '
                    'Creating a bot does not grant another delivery budget or bypass any approval. '
                    'Give one short confirmation. Do not repeat the receipt or add an approval footer.')
    if sources:
        instructions += '\nDirect human messages eligible to request bot creation:\n' + json.dumps(sources)
    else:
        instructions += '\nNo direct human source authorizes bot creation in this turn.'
    receipts = prior_bot_creation_receipts(request)
    if receipts:
        instructions += ('\nThe service already confirmed these bot creations. Use their IDs. '
                         'Do not create replacements or rephrase a creation after a restart. '
                         'An exact retry must keep the same source, name, role, and instructions:\n' + json.dumps(receipts))
    return instructions

def bot_directory(request):
    current_id = request.get('bot', {}).get('id')
    return [
        {'id': bot['id'], 'name': str(bot.get('name', 'Bot'))[:80], 'role': str(bot.get('role', ''))[:160]}
        for bot in request['task'].get('_available_bots', [])
        if isinstance(bot, dict) and isinstance(bot.get('id'), str) and bot['id'] != current_id
    ]


def explicit_bot_messages(task):
    return [
        {'sender_bot_id': message['sender_bot_id'], 'sender_name': str(message.get('sender_name', 'Bot'))[:80], 'text': message['text']}
        for message in task.get('_bot_messages', [])
        if isinstance(message, dict) and isinstance(message.get('sender_bot_id'), str) and isinstance(message.get('text'), str)
    ]


def bot_message_payload(arguments, targets):
    if not isinstance(arguments, dict) or set(arguments) != {'target_bot_id', 'text'}:
        raise ValueError('Supply only the target bot ID and message text.')
    target, text = arguments['target_bot_id'], arguments['text']
    if not isinstance(target, str) or target not in targets:
        raise ValueError('Choose a bot from the available bots list.')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('Send a nonempty message of at most 4000 characters.')
    return {'target_bot_id': target, 'text': text}


def file_proposal(arguments):
    """Keep each choice tied to its complete proposed outcome."""
    if not isinstance(arguments, dict) or set(arguments) - {'title', 'explanation', 'options', 'recommended_option_id', 'predicted_option_id'}:
        raise ValueError('Use the native file proposal schema.')
    title = arguments.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError('Give this decision a concise title of at most 160 characters.')
    options = arguments.get('options')
    if not isinstance(options, list) or not 1 <= len(options) <= 8:
        raise ValueError('Provide one to eight useful choices.')
    ids, outcomes = set(), set()
    actions = []
    for option in options:
        if not isinstance(option, dict) or set(option) - {'id', 'label', 'explanation', 'answer', 'action', 'modification'}:
            raise ValueError('Each choice must use the native option schema.')
        identifier = option.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', identifier) or identifier in ids:
            raise ValueError('Each choice needs a unique short ID.')
        ids.add(identifier)
        for field, maximum in (('label', 80), ('explanation', 500)):
            if not isinstance(option.get(field), str) or not option[field].strip() or len(option[field]) > maximum:
                raise ValueError(f'Each choice needs a short {field}.')
        answer = option.get('answer')
        if answer == 'approve':
            action = option.get('action')
            if not isinstance(action, dict) or set(action) != {'type', 'filename', 'content'} or action['type'] != 'create_file':
                raise ValueError('An approval choice needs an exact create_file action.')
            filename, content = action['filename'], action['content']
            if not isinstance(filename, str) or not filename or len(filename) > 160 or PurePosixPath(filename).name != filename or '\\' in filename or '\x00' in filename or filename in ('.', '..'):
                raise ValueError('Use a plain file name for each approval choice.')
            if not isinstance(content, str) or len(content.encode()) > 1_000_000:
                raise ValueError('Each approval choice needs complete file content within the size limit.')
            if 'modification' in option:
                raise ValueError('An approval choice cannot also request a modification.')
            actions.append(action)
            outcome = json.dumps(action, sort_keys=True)
        elif answer in ('reject', 'modify'):
            if 'action' in option:
                raise ValueError('A rejection or modification cannot carry an executable action.')
            if answer == 'modify':
                correction = option.get('modification')
                if not isinstance(correction, str) or not correction.strip() or len(correction) > 2000:
                    raise ValueError('A modification choice needs the exact requested change.')
                outcome = 'modify:' + correction
            else:
                if 'modification' in option:
                    raise ValueError('A rejection cannot also request a modification.')
                outcome = 'reject'
        else:
            raise ValueError('Each choice must approve, reject, or request a modification.')
        if outcome in outcomes:
            raise ValueError('Each choice needs a different outcome.')
        outcomes.add(outcome)
    if not actions:
        raise ValueError('A file proposal needs at least one exact file choice.')
    recommended_id, predicted_id = arguments.get('recommended_option_id'), arguments.get('predicted_option_id')
    if recommended_id not in ids or predicted_id not in ids:
        raise ValueError('Recommendations and predictions must refer to listed choices.')
    explanation = arguments.get('explanation')
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 2000:
        raise ValueError('Explain the decision in one short paragraph.')
    recommended = next(option for option in options if option['id'] == recommended_id)
    predicted = next(option for option in options if option['id'] == predicted_id)
    action = recommended.get('action', actions[0])
    proposal = {
        'title': title, 'explanation': explanation,
        'action_type': 'create_file', 'resource': ', '.join(dict.fromkeys(item['filename'] for item in actions)),
        'consequences': 'An approved choice creates its exact file in Coconut storage. Reject saves nothing. Modify requests a new proposal.',
        'recommended': recommended['answer'], 'predicted_choice': predicted['answer'],
        'recommended_option_id': recommended_id, 'predicted_option_id': predicted_id,
        'options': options, 'action': action,
    }
    if len(event_frame('decision', proposal)) > MAX_EVENT_CHARACTERS:
        raise ValueError('The combined proposal is too large. Reduce the file content or the number of choices.')
    return proposal


def model_configuration(model, base_url, provider=None):
    """Select a credential only for its matching endpoint."""
    parsed = urlparse(base_url) if base_url else None
    hostname = parsed.hostname if parsed else None
    local_hosts = ('localhost', '127.0.0.1', '::1')
    if parsed and (not hostname or parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('Use a model endpoint without credentials, a query, or a fragment.')
    if parsed and hostname not in local_hosts and parsed.scheme != 'https':
        raise ValueError('Remote model endpoints require HTTPS.')
    if provider is None:
        if hostname == 'api.anthropic.com' or (not base_url and model.startswith('claude-')):
            provider = 'anthropic'
        elif hostname == 'openrouter.ai' or (not base_url and '/' in model):
            provider = 'openrouter'
        elif hostname == 'api.openai.com' or not base_url:
            provider = 'openai'
        elif hostname in local_hosts:
            provider = 'custom'
    if provider == 'custom' and hostname in local_hosts:
        return {'provider': 'custom', 'api_key': 'local', 'base_url': base_url}
    routes = {
        'anthropic': ('api.anthropic.com', 'ANTHROPIC_API_KEY', 'https://api.anthropic.com'),
        'openai': ('api.openai.com', 'OPENAI_API_KEY', 'https://api.openai.com/v1'),
        'openrouter': ('openrouter.ai', 'OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1'),
    }
    if isinstance(provider, str) and provider in routes:
        expected_host, credential, default_url = routes[provider]
        if hostname is None or hostname == expected_host:
            return {'provider': provider, 'api_key': os.environ.get(credential), 'base_url': base_url or default_url}
    raise ValueError('This endpoint needs an explicit credential binding. No existing key was sent.')


def reasoning_configuration(effort):
    """Translate the service's validated effort into Hermes' native config."""
    if effort is None:
        return None
    if not isinstance(effort, str) or not effort or effort != effort.strip():
        raise ValueError('Use the reasoning effort resolved by the service.')
    return {'enabled': False} if effort == 'none' else {'enabled': True, 'effort': effort}


def execution_configuration(model, base_url, *, provider=None, runtime_config=None):
    """Use private native routing without changing the worker's isolated home."""
    if runtime_config is None:
        return model_configuration(model, base_url, provider)
    scalar_keys = {'provider', 'api_mode', 'base_url', 'api_key', 'requested_provider', 'acp_command', 'command'}
    list_keys = {'acp_args', 'args'}
    if not isinstance(runtime_config, dict) or set(runtime_config) - scalar_keys - list_keys:
        raise ValueError('The native model configuration contains an unsupported field.')
    if not isinstance(runtime_config.get('provider'), str) or not runtime_config['provider']:
        raise ValueError('The native model configuration has no provider.')
    for key, value in runtime_config.items():
        if key in scalar_keys and value is not None and not isinstance(value, str):
            raise ValueError('The native model configuration contains an invalid value.')
        if key in list_keys and value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise ValueError('The native model configuration contains invalid arguments.')
    return dict(runtime_config)


def conversation_input(request):
    rows = visible_history(request.get('history', []))
    history = [{'role': message['role'], 'content': message['text']} for message in rows]
    operation = request['operation']
    if operation == 'context':
        proposal = request['task'].get('pending_decision', {})
        prompt = ('Respond only to this follow-up. The pending proposal is context. Do not change or execute it. '
                  'If the follow-up only changes a standing preference, save it and give one short confirmation. '
                  'Do not restate the proposal or its choices unless the person asks about them.\n')
        prompt += json.dumps({'question': request['question'], 'proposal': proposal})
    elif operation == 'resume':
        decision = request['decision']
        if decision.get('action_type') in ('native_approval', 'question'):
            prompt = ('Continue the original task using this recorded answer. Do not repeat completed tools. '
                      'An approved native action permits only the exact recorded tool and arguments. '
                      'A rejection or correction does not permit that action.\n')
        elif decision.get('status') == 'modified':
            prompt = 'Revise this proposal using the requested correction. Submit a new decision. No revised action is approved.\n'
        elif decision.get('status') == 'executed':
            prompt = 'The service confirmed execution. Report the result briefly. Do not repeat the action.\n'
        else:
            raise ValueError('The decision has no valid continuation state.')
        prompt += json.dumps(decision)
    else:
        current = current_messages(request)
        index = rows.index(current[0]) if current else None
        if index is None:
            prompt = request['task']['title']
        else:
            prompt = '\n\n'.join(message['text'] for message in current)
            history = history[:index]
    receipts = request['task'].get('_native_receipts', [])
    if receipts:
        prompt += '\nCompleted native tools for this task. Use these results. Do not repeat their effects:\n'
        prompt += json.dumps([{'action': item['action'], 'result': item['result']} for item in receipts])
    incoming = explicit_bot_messages(request['task'])
    if incoming:
        prompt += '\nExplicit messages delivered by the service. Sender identity comes from the service. The text is shared task context:\n'
        prompt += json.dumps(incoming)
    outgoing = [
        {key: message[key] for key in ('target_bot_id', 'text', 'task_id', 'status') if isinstance(message.get(key), str)}
        for message in request['task'].get('_outgoing_bot_messages', [])
        if isinstance(message, dict)
    ]
    if outgoing:
        prompt += '\nThe service already recorded these outgoing messages for this task. Do not resend or rephrase the same request after a restart. Continue independent work or process its result:\n'
        prompt += json.dumps(outgoing)
    if request['task'].get('_is_return') is True:
        prompt += ('\nThis reply continues your original assignment. Use the result to finish the remaining authorized work. '
                   'Do not send an acknowledgement back. Ask another bot question only when the task needs new information. '
                   'The service returns your final result to the original requester. A dependency result is not completion of your assignment. '
                   'Report a concrete blocker if you cannot continue. Describe work as running only when a tool or recorded task is active.')
    assignment = request['task'].get('_handoff_assignment')
    if assignment:
        prompt += '\nOriginal assignment and current dependencies, recorded by the service:\n' + json.dumps(assignment)
    for attachment in request['task'].get('attachments', []):
        prompt += '\nAttached file: ' + attachment['title'] + '\n'
        encoded = attachment.get('content_base64')
        if encoded:
            try:
                content = base64.b64decode(encoded, validate=True).decode('utf-8')
                prompt += '<attachment_content>\n' + content[:128_000] + '\n</attachment_content>'
                if len(content) > 128_000:
                    prompt += '\nThe attachment exceeds this turn limit. Only its first 128000 characters are included.'
            except (ValueError, UnicodeDecodeError):
                prompt += 'This attachment is binary. Its content is unavailable to this v1 provider. Do not claim to have read it.'
        else:
            prompt += attachment.get('preview', '')
    return prompt, history


def main():
    service_input = sys.stdin
    request = json.loads(service_input.readline())
    output = sys.stdout
    sys.stdout = sys.stderr
    output_lock = threading.Lock()
    receipt_lock = threading.Lock()
    pending_receipts = {}
    next_request_id = 0

    def emit(kind, data):
        with output_lock:
            output.write(event_frame(kind, data))
            output.flush()

    def request_receipt(kind, data):
        nonlocal next_request_id
        with receipt_lock:
            next_request_id += 1
            request_id = next_request_id
            pending_receipts[request_id] = response = queue.Queue(maxsize=1)
        try:
            with output_lock:
                output.write(json.dumps({'type': kind, 'data': data, 'request_id': request_id}, ensure_ascii=False) + '\n')
                output.flush()
            try:
                return response.get(timeout=30)
            except queue.Empty:
                return {'status': 'unconfirmed', 'error': 'The service has not confirmed this operation. Retry only the exact same request.'}
        finally:
            with receipt_lock:
                pending_receipts.pop(request_id, None)

    def watch_parent():
        for line in service_input:
            try:
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    continue
                with receipt_lock:
                    response = pending_receipts.get(frame.get('request_id'))
                if response and isinstance(frame.get('receipt'), dict):
                    response.put_nowait(frame['receipt'])
            except (ValueError, TypeError, queue.Full):
                continue
        # EOF means the service died or closed its pipe. End even a blocked model call.
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True, name='service-liveness').start()
    conversation = None
    agent = None
    try:
        from profiles import merge_runtime_config
        merge_runtime_config(os.environ['HERMES_HOME'])
        from terminal_setup import configure_terminal
        configure_terminal(os.environ['HERMES_HOME'])
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)
        from run_agent import AIAgent
        from hermes_state import SessionDB
        from tools.registry import registry
        from toolsets import create_custom_toolset

        operation = request['operation']
        proposed = False
        response_chunks = []
        allowed = set()
        directory = bot_directory(request)
        remaining = request['task'].get('_handoff_budget_remaining', 0)
        remaining = max(0, min(8, remaining)) if isinstance(remaining, int) else 0
        directory_lock = threading.Lock()
        creation_sources = {source['id']: source for source in bot_creation_sources(request)}
        creation_lock = threading.Lock()
        creation_errors = {}
        deletion_sources = {source['id']: source for source in bot_deletion_sources(request)}
        deletion_targets = bot_deletion_directory(request)
        deletion_lock = threading.Lock()
        deletion_errors = {}

        def propose_file(arguments, **kwargs):
            nonlocal proposed
            if operation == 'context':
                return json.dumps({'error': 'Context requests cannot change a decision.'})
            if proposed:
                return json.dumps({'error': 'One proposal is already pending.'})
            if creation_errors:
                return json.dumps({'error': 'A bot creation remains unconfirmed. Resolve it before ending this turn with a proposal.'})
            if deletion_errors:
                return json.dumps({'error': 'A bot deletion remains unconfirmed. Resolve it before ending this turn with a proposal.'})
            try:
                proposal = file_proposal(arguments)
            except ValueError as error:
                return json.dumps({'error': str(error)})
            proposed = True
            emit('decision', proposal)
            # Parent persists the decision and terminates this process. The wait prevents
            # Hermes from claiming approval or executing another step before that commit.
            threading.Event().wait()
            return json.dumps({'status': 'pending'})

        if operation != 'context' and not (operation == 'resume' and request['decision'].get('status') == 'executed'):
            schema = PROPOSAL_SCHEMA
            registry.register(name=schema['name'], toolset='coconut', schema=schema, handler=propose_file)
            allowed.add(schema['name'])

        def create_bot(arguments, **kwargs):
            try:
                payload = bot_creation_payload(arguments, creation_sources)
            except ValueError as error:
                creation_errors['invalid_request'] = 'invalid_request'
                return json.dumps({'status': 'validation_failed', 'error_code': 'bot_creation_invalid_request', 'error': str(error)})
            key = (payload['source_message_id'], bot_creation_name(payload['name']))
            with creation_lock:
                receipt = request_receipt('bot_create', payload)
                try:
                    confirmed = bot_creation_receipt(receipt, payload, request['bot'])
                except ValueError:
                    category = 'rejected' if receipt.get('status') == 'validation_failed' else 'receipt_unconfirmed'
                    creation_errors[key] = category
                    return json.dumps({'status': 'validation_failed' if category == 'rejected' else 'unconfirmed', 'error_code': 'bot_creation_' + category,
                                       'error': 'The service has not confirmed this bot creation. Do not claim success. Retry only the exact same request.'})
                creation_errors.pop(key, None)
                creation_errors.pop('invalid_request', None)
                with directory_lock:
                    deletion_targets[confirmed['bot_id']] = confirmed['name']
                relay.add_peer(confirmed)
                return json.dumps(confirmed)

        if creation_sources:
            registry.register(name=BOT_CREATION_SCHEMA['name'], toolset='coconut', schema=BOT_CREATION_SCHEMA, handler=create_bot)
            allowed.add(BOT_CREATION_SCHEMA['name'])

        def delete_bot(arguments, **kwargs):
            try:
                with directory_lock:
                    payload = bot_deletion_payload(arguments, deletion_sources, deletion_targets)
            except ValueError as error:
                deletion_errors['invalid_request'] = 'invalid_request'
                return json.dumps({'status': 'validation_failed', 'error_code': 'bot_deletion_invalid_request', 'error': str(error)})
            key = (payload['source_message_id'], payload['target_bot_id'])
            with deletion_lock:
                # Prevent a same-turn message while deletion is unconfirmed.
                with directory_lock:
                    relay.remove_peer(payload['target_bot_id'])
                receipt = request_receipt('bot_delete', payload)
                try:
                    confirmed = bot_deletion_receipt(receipt, payload, request['bot'])
                except ValueError:
                    category = 'rejected' if receipt.get('status') == 'validation_failed' else 'receipt_unconfirmed'
                    deletion_errors[key] = category
                    return json.dumps({'status': 'validation_failed' if category == 'rejected' else 'unconfirmed', 'error_code': 'bot_deletion_' + category,
                                       'error': 'The service has not confirmed this deletion. Do not claim success. Retry only the exact same source, bot ID, and name.'})
                deletion_errors.pop(key, None)
                deletion_errors.pop('invalid_request', None)
                relay.remove_peer(confirmed['bot_id'])
                return json.dumps(confirmed)

        if deletion_sources:
            registry.register(name=BOT_DELETION_SCHEMA['name'], toolset='coconut', schema=BOT_DELETION_SCHEMA, handler=delete_bot)
            allowed.add(BOT_DELETION_SCHEMA['name'])

        from native_bridge import NativeBridge, QUESTION_SCHEMA
        bridge = NativeBridge(request, emit, request_receipt)
        if operation != 'context':
            from service_restart import RESTART_SCHEMA
            registry.register(name=RESTART_SCHEMA['name'], toolset='coconut', schema=RESTART_SCHEMA,
                              handler=lambda arguments, **kwargs: json.dumps(request_receipt('service_restart', arguments)))
            allowed.add(RESTART_SCHEMA['name'])
        registry.register(name=QUESTION_SCHEMA['name'], toolset='coconut', schema=QUESTION_SCHEMA, handler=bridge.ask)
        allowed.add(QUESTION_SCHEMA['name'])
        create_custom_toolset('coconut', 'Native Coconut actions', tools=sorted(allowed))

        def stream_delta(text):
            if isinstance(text, str) and text and not creation_errors and not deletion_errors:
                response_chunks.append(text)

        instructions = ('You work in Coconut, a concise messaging app. Give short, useful replies. '
                        'Use native Hermes tools and profile memory. Save standing preferences with the native memory tool. '
                        'Treat new human messages as one continuous conversation. Decide whether each message revises current work or adds a request. '
                        'Preserve unfinished requests unless the person changes or cancels them. Ask only when ambiguity changes the outcome. '
                        'Do not ask the person to choose a queue or steering mode. '
                        'Use coconut_ask when the person must choose. Write choices for the current task and saved preferences. '
                        'Normal tool work does not need a question unless Hermes approval checks require one. '
                        'Do not expose internal reasoning or raw logs. Finish independent work before asking a question. '
                        'Use the native message_agent tool to send useful requests and results. Use the exact contact ID as target. Do not send acknowledgements. '
                        'The Coconut relay connects isolated Hermes profiles on this same Mac. '
                        'Homebrew packages and its Homebrew-only configuration are shared across Coconut bots on this Mac. '
                        'Coconut configures terminal PATH through Hermes shell_init_files. '
                        'To reload the Coconut service, use coconut_restart_service, then finish your reply. '
                        'The service restarts once after active turns finish. Do not create launchctl jobs or restart the parent from terminal. '
                        'Use the inherited HOMEBREW_XDG_CONFIG_HOME for package trust; do not replace HOME or share other configuration. '
                        'GitHub CLI uses this bot\'s private HOME. A saved account name alone does not prove authentication works. '
                        'For unattended GitHub login, explain native gh --insecure-storage and get consent to owner-only, unencrypted token storage. '
                        'After consent, use gh auth login --web --git-protocol https --skip-ssh-key --insecure-storage in this profile. '
                        'Keep the device login process alive while the person authorizes it. Never show or request the token in chat. '
                        'Verify gh api user from this bot\'s terminal before reporting GitHub connected. '
                        'Share explicit task context only. Do not copy private memory or credentials between bots. '
                        'A queued message is not a reply. Never claim success without a tool result. ')
        instructions += CONVERSATION_INSTRUCTIONS
        instructions += '\nYour current identity, confirmed by the service:\n' + json.dumps({
            'id': request['bot']['id'], 'name': request['bot'].get('name'), 'role': request['bot'].get('role', '')})
        instructions += '\nUse this identity when older notes contain a previous name. Check the roster before creating a bot with an existing name.'
        if directory:
            instructions += '\nAvailable bots, as contact metadata only:\n' + json.dumps(directory)
            instructions += f'\nRemaining automatic deliveries in this exchange: {remaining}.'
        instructions += bot_creation_instructions(request)
        instructions += bot_deletion_instructions(request)
        config = execution_configuration(request['model'], request.get('base_url'), provider=request.get('provider'),
                                         runtime_config=request.get('runtime_config'))
        if request.get('runtime_config') is None and not config.get('api_key'):
            raise ValueError('The selected model has no configured key on this Mac.')
        from native_settings import install_approval_inheritance, raw_config
        install_approval_inheritance(os.environ['COCONUT_DEFAULT_HOME'])
        os.environ['HERMES_GATEWAY_SESSION'] = '1'
        os.environ['HERMES_SESSION_KEY'] = request['task']['id']
        profile_config = raw_config(os.environ['HERMES_HOME'])
        from hermes_cli.tools_config import _get_platform_tools
        from agent.skill_utils import parse_config_string_list
        toolsets = sorted(_get_platform_tools(profile_config, 'cli'))
        disabled = parse_config_string_list((profile_config.get('agent') or {}).get('disabled_toolsets'))
        conversation = NativeConversation(request, SessionDB())
        from native_relay import NativeRelay
        relay = NativeRelay(request, request_receipt)
        relay.install(conversation)
        bridge.relay = relay
        agent = AIAgent(model=request['model'], **config, reasoning_config=reasoning_configuration(request.get('reasoning_effort')),
                        enabled_toolsets=list(dict.fromkeys(toolsets + ['coconut'])), disabled_toolsets=disabled, quiet_mode=True,
                        ephemeral_system_prompt=instructions, session_id=conversation.session_id, session_db=conversation.db,
                        platform='coconut', gateway_session_key=request['task']['id'], clarify_callback=bridge.clarify,
                        skip_context_files=False, load_soul_identity=True, skip_memory=False, skip_background_review=False,
                        save_trajectories=False, run_budget_seconds=None)
        bridge.install(registry)
        prompt, _ = conversation_input(request)
        current = current_message(request) if operation == 'start' else None
        result = agent.run_conversation(prompt, conversation_history=conversation.history, task_id=request['task']['id'],
                                        stream_callback=stream_delta,
                                        persist_user_message=current['text'] if current else request.get('question'),
                                        persist_user_platform_id='coconut:' + current['id'] if current else None)
        agent.close()
        agent = None
        if result.get('completed') is not True or result.get('failed') or result.get('interrupted'):
            raise RuntimeError('Hermes could not finish this turn.')
        if creation_errors:
            category = next(iter(creation_errors.values()))
            emit('failed', {'summary': 'The Mac did not confirm the last bot creation request. Check the bot list before retrying.',
                            'error_code': 'bot_creation_' + category})
            return 1
        if deletion_errors:
            category = next(iter(deletion_errors.values()))
            emit('failed', {'summary': 'The Mac did not confirm the last bot deletion. Retry the exact request to finish it.',
                            'error_code': 'bot_deletion_' + category})
            return 1
        final = result.get('final_response') or ''.join(response_chunks)
        if final:
            emit('text_delta', {'text': final})
        if operation != 'context':
            emit('completed', {'summary': 'Complete.'})
    except Exception as error:
        # Model libraries can include request bodies or keys in exception strings.
        emit('failed', {'summary': f'Hermes could not finish ({type(error).__name__}). Check the model configuration on the Mac.'})
        return 1
    finally:
        if agent is not None:
            agent.close()
        if conversation is not None:
            conversation.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
