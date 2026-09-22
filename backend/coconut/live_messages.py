"""Deliver chat input through Hermes while retaining unapplied input for the next turn."""
from threading import RLock


class LiveMessages:
    def __init__(self, agent, consumed):
        self.agent = agent
        self.consumed = consumed
        self.lock = RLock()
        self.pending = {}
        self.waiting = {}
        self.starting = agent is None
        self.accept = None
        self.deferred = False

    def bind(self, agent):
        with self.lock:
            self.agent = agent

    def run_conversation(self, prompt, **kwargs):
        with self.lock:
            for message in self.waiting.values():
                if self.accept is not None and not self.accept(message):
                    self.deferred = True
                    break
                content = self.content(message)
                # Keep image/file blocks in multimodal startup prompts.
                prompt = (prompt + '\n\n' + content if isinstance(prompt, str)
                          else [*prompt, {'type': 'text', 'text': content}])
                saved = kwargs.get('persist_user_message')
                if saved is not None:
                    kwargs['persist_user_message'] = (saved + '\n\n' + content if isinstance(saved, str)
                                                      else [*saved, {'type': 'text', 'text': content}])
                self.pending[message['id']] = content
            self.waiting.clear()
            self.starting = False
        return self.agent.run_conversation(prompt, **kwargs)

    @staticmethod
    def content(message):
        content = (f'[New chat message {message["id"]}; it may concern another topic. '
                'Respond to the latest intent. Keep unrelated work separate.]\n' + message['text'])
        if message.get('bot_source'):
            content += f'\n[Service-confirmed human source for bot creation or deletion: {message["id"]}.]'
        return content

    def deliver(self, messages):
        with self.lock:
            if self.deferred:
                return
            if self.starting:
                self.waiting.update((message['id'], message) for message in messages)
                return
            for message in messages:
                identity = message['id']
                if identity in self.pending:
                    continue
                if self.accept is not None and not self.accept(message):
                    self.deferred = True
                    break
                content = self.content(message)
                accepted = self.agent.redirect(content)
                if not accepted and hasattr(self.agent, 'steer'):
                    accepted = self.agent.steer(content)
                if not accepted:
                    break
                self.pending[identity] = content

    def finish(self, result):
        if result.get('completed') is not True or result.get('failed') or result.get('interrupted'):
            return
        rows = []
        for message in result.get('messages', []):
            if message.get('role') != 'user':
                continue
            content = message.get('content', '')
            rows.append(content if isinstance(content, str) else '\n'.join(
                item.get('text', '') for item in content if isinstance(item, dict)))
        with self.lock:
            identities = []
            for identity, content in self.pending.items():
                if content in (result.get('pending_steer') or '') or not any(content in row for row in rows):
                    break
                identities.append(identity)
            if identities:
                self.consumed(identities)
