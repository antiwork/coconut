"""Deliver chat input through Hermes while retaining unapplied input for the next turn."""
from threading import RLock


class LiveMessages:
    def __init__(self, agent, consumed):
        self.agent = agent
        self.consumed = consumed
        self.lock = RLock()
        self.pending = {}

    def deliver(self, messages):
        with self.lock:
            for message in messages:
                identity = message['id']
                if identity in self.pending:
                    continue
                content = (f'[New chat message {identity}; it may concern another topic. '
                           'Respond to the latest intent. Keep unrelated work separate.]\n'
                           + message['text'])
                if not self.agent.redirect(content):
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
