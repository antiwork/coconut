"""The bot changes that a turn may make: the context of a turn for coconut_create_bot and coconut_delete_bot.

The service gives a turn the person's direct messages that may ask for a change, the bots that the turn may delete, and
the changes that the service already recorded (Service._bot_creation_sources, _bot_deletion_sources). The tools and
their rules are in card_tools. The service checks each request (_create_bot_from_chat, _delete_bot_from_chat).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import unicodedata
from uuid import UUID


def bot_deletion_sources(request):
    if request.get('operation') not in ('start', 'resume'):
        return []
    return [
        {'id': source['id'], 'text': source['text']}
        for source in request['task'].get('_bot_deletion_sources', [])
        if isinstance(source, dict) and isinstance(source.get('id'), str) and source['id']
        and isinstance(source.get('text'), str)
    ]


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




def bot_deletion_context(request):
    sources = bot_deletion_sources(request)
    text = ''
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
    # The profile ID is the key of the profiles table: the new bot must have its own Hermes profile there.
    if os.environ.get('COCONUT_DATA_DIR') and (Path(os.environ['COCONUT_DATA_DIR']) / 'coconut.sqlite3').is_file():
        from . import profile_registry
        row = profile_registry.lookup(os.environ['COCONUT_DATA_DIR'], value['profile_id'])
        if row is None or row['state'] != 'active':
            raise ValueError('The service did not confirm a Hermes profile for the new bot.')
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




def bot_creation_context(request):
    sources = bot_creation_sources(request)
    text = ''
    if sources:
        text += '\nDirect human messages eligible to request bot creation:\n' + json.dumps(sources)
    else:
        text += '\nNo direct human source authorizes bot creation in this turn.'
    receipts = prior_bot_creation_receipts(request)
    if receipts:
        text += ('\nThe service already confirmed these bot creations. Use their IDs. '
                 'Do not create replacements or rephrase a creation after a restart. '
                 'An exact retry must keep the same source, name, role, and instructions:\n' + json.dumps(receipts))
    return text
