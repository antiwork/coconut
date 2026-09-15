"""Route concurrent turns through one native runtime without mixing receipts."""
import json
import queue
import threading
import subprocess
from uuid import uuid4


class WorkerTransport:
    def __init__(self, process, maximum):
        self.process, self.maximum = process, maximum
        self.lock = threading.Lock()
        self.channels = {}
        self.closed = False
        threading.Thread(target=self._read, daemon=True, name='hermes-events').start()

    def open(self, request):
        identity = str(uuid4())
        channel = queue.Queue()
        with self.lock:
            if self.closed:
                raise RuntimeError('The Hermes runtime stopped.')
            self.channels[identity] = channel
        return identity, channel, dict(request, turn_id=identity)

    def release(self, identity):
        with self.lock:
            self.channels.pop(identity, None)

    def _read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(self.maximum + 1), ''):
                if len(line) > self.maximum:
                    raise RuntimeError('Hermes returned an oversized event.')
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise RuntimeError('Hermes returned an invalid event.')
                with self.lock:
                    identity = event.get('turn_id')
                    # A serial fixture can omit routing. Never guess between concurrent turns.
                    if identity is None and len(self.channels) > 1:
                        raise RuntimeError('Hermes returned an event without its work session.')
                    if identity is None and len(self.channels) == 1:
                        identity = next(iter(self.channels))
                    channel = self.channels.get(identity)
                    if channel is not None:
                        channel.put(event)
        except Exception as error:
            failure = error
        else:
            try:
                code = self.process.wait(timeout=5)
                failure = RuntimeError(f'Hermes worker stopped with exit code {code}.')
            except subprocess.TimeoutExpired:
                failure = RuntimeError('The Hermes event stream closed before the runtime stopped.')
        with self.lock:
            self.closed = True
            for channel in self.channels.values():
                channel.put(failure)


def events(channel):
    while True:
        event = channel.get()
        if isinstance(event, BaseException):
            raise event
        yield event
