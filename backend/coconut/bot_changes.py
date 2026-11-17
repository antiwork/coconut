"""The bot changes that a turn may make: the context of a turn for coconut_delete_bot.

The service gives a turn the person's direct messages that may ask for a change, the bots that the turn may delete, and
the deletions that the service already recorded (Service._bot_deletion_input). The tool and its rules are in
card_tools. The service checks each request (_delete_bot_from_chat). Nobody creates a bot: Coconut has two projects.
"""
from __future__ import annotations

import json
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
        raise ValueError('The service did not confirm the requested agent deletion.')
    for field in ('bot_id', 'profile_id'):
        identifier = value.get(field)
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError('The service returned an invalid agent identity.')
    if value['profile_id'] != value['bot_id'] or value['bot_id'] == (current_bot or {}).get('id'):
        raise ValueError('The service did not confirm deletion of another agent.')
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


