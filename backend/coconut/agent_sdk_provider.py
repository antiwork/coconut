"""Opt-in: each turn runs with Claude's Agent SDK instead of the Hermes gateway (docs/cloud.md).

Nothing uses this provider by default: `--provider agent-sdk` starts the service with it, for example on a Linux host
without Hermes (cloud/Dockerfile). The SDK runs the agent loop of Claude Code in a subprocess, with Claude on Amazon
Bedrock. Coconut's card tool is an in-process MCP tool with the schema of `coconut_ask` (card_tools.QUESTION_SCHEMA). A
card ends the turn: the provider records it through the same path as a card of the gateway (`native_question`), which
pauses the task, and the answer resumes the same SDK session (`resume_task`). Each chat keeps one SDK session.

The agent has read-only tools and the card tool, and no other Coconut tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from .providers import FixtureProvider

LOG = logging.getLogger(__name__)
DEFAULT_MODEL = 'us.anthropic.claude-opus-5-5'
# The fields of coconut_ask that this provider does not support yet (GitHub sources, local images, PR merges).
UNSUPPORTED_FIELDS = ('source_revisions', 'sources', 'previews', 'previous_decision_id', 'merge_pr_url')
# Read-only tools. A tool that writes or runs commands needs an approval card, which this provider does not build yet.
TOOLS = ['Read', 'Glob', 'Grep', 'WebFetch']
SYSTEM = (
    'You are Coconut, the agent of a work platform that the person uses on a Mac and an iPhone. '
    'The person reads your replies in the Coconut app, which shows Markdown. Write short, plain replies for a phone. '
    'Answer the request in chat. ')


def _ask_schema():
    from .card_tools import QUESTION_SCHEMA
    schema = json.loads(json.dumps(QUESTION_SCHEMA['parameters']))
    for field in UNSUPPORTED_FIELDS:
        schema['properties'].pop(field, None)
    return schema


class AgentSDKProvider(FixtureProvider):
    """Runs a turn with the Agent SDK. The service owns the task, its messages and its cards."""

    name = 'agent-sdk'
    ready = True

    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.model = os.environ.get('COCONUT_SDK_MODEL', DEFAULT_MODEL)
        self.home = self.data_dir / 'agent-sdk'
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sessions_path = self.home / 'sessions.json'
        self._lock = threading.Lock()
        self._running: dict[str, tuple] = {}

    # Each chat keeps the ID of its SDK session, so a later turn and a card answer continue it.
    def _session(self, bot_id):
        with self._lock:
            try:
                return json.loads(self.sessions_path.read_text()).get(bot_id)
            except (OSError, ValueError):
                return None

    def _save_session(self, bot_id, session_id):
        with self._lock:
            try:
                sessions = json.loads(self.sessions_path.read_text())
            except (OSError, ValueError):
                sessions = {}
            sessions[bot_id] = session_id
            temporary = self.sessions_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(sessions))
            os.replace(temporary, self.sessions_path)

    def start_task(self, task, bot, history, emit):
        prompt = next((m.get('text', '') for m in reversed(history)
                       if m.get('role') == 'user' and m.get('task_id') == task['id']), task.get('title', ''))
        self._turn(task, bot, prompt, emit)

    def resume_task(self, task, bot, history, decision, emit):
        if decision.get('status') == 'dismissed':
            emit('completed', {'summary': 'The card was dismissed.'})
            return
        title, label, reply = decision.get('title', ''), decision.get('selected_option_label'), decision.get('correction')
        if decision.get('status') == 'rejected':
            text = f'The person declined your card "{title}".'
        elif label:
            text = f'The person chose "{label}" on your card "{title}".'
        else:
            text = f'The person answered your card "{title}".'
        if reply:
            text += f' They wrote: {reply}'
        self._turn(task, bot, text + ' Continue the work with this answer.', emit)

    def answer_context(self, task, bot, history, question, emit):
        emit('text_delta', {'text': 'The cloud spike answers a question about a card in the chat, not here.'})

    def cancel_task(self, task_id):
        running = self._running.get(task_id)
        if running:
            loop, client = running
            asyncio.run_coroutine_threadsafe(client.interrupt(), loop)

    def _options(self, bot, server):
        from claude_agent_sdk import ClaudeAgentOptions
        from .card_tools import DECISION_INSTRUCTIONS
        workspace = self.data_dir / 'bots' / bot['id'] / 'workspace'
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {
            'CLAUDE_CODE_USE_BEDROCK': '1',
            'AWS_REGION': os.environ.get('COCONUT_BEDROCK_REGION', 'us-east-1'),
            # Claude Code also calls a small model for side tasks. It uses the main model unless COCONUT_SDK_SMALL_MODEL
            # names another, because Bedrock serves only the models whose offer the AWS account accepted.
            'ANTHROPIC_DEFAULT_HAIKU_MODEL': os.environ.get('COCONUT_SDK_SMALL_MODEL', self.model),
            # The SDK's own transcripts and settings stay in the service's data folder, never in a person's ~/.claude.
            'CLAUDE_CONFIG_DIR': str(self.home / 'claude'),
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'DISABLE_AUTOUPDATER': '1',
        }
        return ClaudeAgentOptions(
            model=self.model, system_prompt=SYSTEM + DECISION_INSTRUCTIONS, tools=TOOLS,
            allowed_tools=TOOLS + ['mcp__coconut__coconut_ask'], mcp_servers={'coconut': server},
            setting_sources=[], cwd=str(workspace), env=env, max_turns=30, resume=self._session(bot['id']),
            stderr=lambda line: LOG.info('agent sdk: %s', line.rstrip()[:400]))

    def _turn(self, task, bot, prompt, emit):
        from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient, ResultMessage, SystemMessage, TextBlock,
                                      create_sdk_mcp_server, tool)
        from .card_tools import PAUSED, QUESTION_SCHEMA
        card: dict = {}
        texts: list[str] = []
        result: dict = {}

        @tool('coconut_ask', QUESTION_SCHEMA['description'], _ask_schema())
        async def ask(arguments):
            if card:
                return {'content': [{'type': 'text', 'text': 'This turn already has a card. End the turn now.'}],
                        'is_error': True}
            card.update({key: value for key, value in arguments.items() if key not in UNSUPPORTED_FIELDS})
            return {'content': [{'type': 'text', 'text': PAUSED}]}

        server = create_sdk_mcp_server('coconut', '1.0.0', [ask])

        async def run():
            async with ClaudeSDKClient(options=self._options(bot, server)) as client:
                self._running[task['id']] = (asyncio.get_running_loop(), client)
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage) and message.subtype == 'init':
                        session = (message.data or {}).get('session_id')
                        if session:
                            self._save_session(bot['id'], session)
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                # The phone shows the text as the agent writes it. Text after a card waits.
                                if card:
                                    texts.append(block.text)
                                else:
                                    emit('text_delta', {'text': ('\n\n' if result.get('wrote') else '') + block.text})
                                    result['wrote'] = True
                    elif isinstance(message, ResultMessage):
                        result.update(session=message.session_id, error=message.is_error, subtype=message.subtype,
                                      cost=message.total_cost_usd, usage=message.usage)

        try:
            asyncio.run(run())
        finally:
            self._running.pop(task['id'], None)
        if result.get('session'):
            self._save_session(bot['id'], result['session'])
        LOG.info('agent sdk turn: task %s, %s, cost %s, card %s', task['id'], result.get('subtype'), result.get('cost'),
                 bool(card))
        if texts:
            emit('text_delta', {'text': ('\n\n' if result.get('wrote') else '') + '\n\n'.join(texts)})
        if card:
            # The same path as a card of the gateway. It records the card, pauses the task and raises YieldForDecision.
            emit('native_question', card)
        if result.get('error'):
            emit('failed', {'summary': f'The agent stopped: {result.get("subtype")}.'})
        else:
            emit('completed', {'summary': 'Complete.'})
