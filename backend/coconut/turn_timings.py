"""Measure preparation without recording prompts, model settings, or credentials."""
import json
import logging
import time


class TurnTimings:
    def __init__(self, task_id, component):
        self.task_id, self.component = task_id, component
        self.previous = time.perf_counter()
        self.stages = {}

    def mark(self, stage):
        current = time.perf_counter()
        self.stages[stage] = round((current - self.previous) * 1000, 1)
        self.previous = current

    def payload(self):
        return dict(task_id=self.task_id, component=self.component, stages_ms=self.stages)

    def report(self):
        logging.getLogger(__name__).warning('coconut.turn_timing %s', json.dumps(self.payload()))


from contextlib import contextmanager
import threading


@contextmanager
def timed_lock(lock, operation):
    started = time.perf_counter()
    acquired = started
    try:
        with lock:
            acquired = time.perf_counter()
            yield
    finally:
        ended = time.perf_counter()
        if ended - started >= 1:
            logging.getLogger(__name__).warning('coconut.transaction_timing %s', json.dumps(dict(
                operation=operation, thread=threading.current_thread().name,
                wait_ms=round((acquired - started) * 1000, 1), hold_ms=round((ended - acquired) * 1000, 1))))


class TurnTrace:
    """Correlate execution stages without recording conversation content."""
    def __init__(self, task, operation, emit=None):
        self.task_id = task['id']
        self.turn_id = task.get('_native_turn_id')
        self.operation = operation
        self.started = time.perf_counter()
        self.emitted = set()
        self.emit = emit

    def mark(self, stage, **measurements):
        if stage in self.emitted:
            return
        self.emitted.add(stage)
        self.record(stage, **measurements)

    def record(self, stage, **measurements):
        from datetime import datetime, timezone
        data = dict(task_id=self.task_id, turn_id=self.turn_id, operation=self.operation,
                    stage=stage, at=datetime.now(timezone.utc).isoformat(),
                    elapsed_ms=round((time.perf_counter() - self.started) * 1000, 1), **measurements)
        if self.emit:
            self.emit('turn_trace', data)
        else:
            logging.getLogger(__name__).warning('coconut.turn_trace %s', json.dumps(data))


@contextmanager
def native_request_timings(trace, agent):
    """Observe native requests without retaining prompts, tools, or credentials."""
    from contextlib import ExitStack
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    context = PluginContext(PluginManifest(name='coconut-request-timings', version='1'), get_plugin_manager())

    def observe(stage, payload):
        if payload.get('task_id') != trace.task_id or payload.get('session_id') != agent.session_id:
            return
        values = {}
        for key in ('api_call_count', 'retry_count', 'message_count', 'tool_count',
                    'approx_input_tokens', 'assistant_content_chars', 'assistant_tool_call_count'):
            value = payload.get(key)
            if type(value) is int and value >= 0:
                values[key] = value
        duration = payload.get('api_duration')
        if type(duration) in (int, float) and duration >= 0:
            values['duration_ms'] = round(duration * 1000, 1)
        started, first = payload.get('started_at'), payload.get('first_chunk_at')
        if type(started) in (int, float) and type(first) in (int, float) and first >= started:
            values['first_chunk_ms'] = round((first - started) * 1000, 1)
        usage = payload.get('usage')
        if isinstance(usage, dict):
            for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'):
                if type(usage.get(key)) is int and usage[key] >= 0:
                    values[key] = usage[key]
        trace.record(stage, **values)

    with ExitStack() as cleanup:
        for hook, stage in (('pre_api_request', 'api_request_start'), ('post_api_request', 'api_request_end'),
                            ('api_request_error', 'api_request_error')):
            handle = context.register_hook(hook, lambda _stage=stage, **payload: observe(_stage, payload))
            cleanup.callback(handle.dispose)
        yield
