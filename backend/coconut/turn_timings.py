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
import collections
import os
import sys
import threading


# A wait plus hold of at least this long goes to the log.
LOG_AFTER = 1.0
# A hold of at least this long gets stack samples of every thread, so the log shows what the other threads did.
SAMPLE_AFTER = 1.0
SAMPLE_EVERY = 0.2
SAMPLE_LIMIT = 50
# Frames of a thread that waits for input, a lock or a timer. Such a thread does not compete for the GIL.
IDLE_FRAMES = {('threading.py', 'wait'), ('threading.py', '_wait_for_tstate_lock'), ('threading.py', 'join'),
               ('queue.py', 'get'), ('selectors.py', 'select'), ('socket.py', 'readinto'), ('socket.py', 'accept'),
               ('ssl.py', 'read'), ('ssl.py', 'recv_into'), ('subprocess.py', '_communicate'),
               ('subprocess.py', '_try_wait'), ('socketserver.py', 'serve_forever'), ('selectors.py', 'poll'),
               ('threading.py', 'acquire'), ('connection.py', '_recv'), ('connection.py', 'poll')}


def _frame_text(frame):
    return f'{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno} {frame.f_code.co_name}'


def _stack_text(frame, depth=3):
    """The innermost frames of a stack, innermost first, with the innermost Coconut frame when it is deeper."""
    parts, coconut = [], None
    while frame is not None:
        if len(parts) < depth:
            parts.append(_frame_text(frame))
        if coconut is None and os.sep + 'coconut' + os.sep in frame.f_code.co_filename:
            coconut = frame
            if len(parts) >= depth:
                break
        frame = frame.f_back
    if coconut is not None and _frame_text(coconut) not in parts:
        parts.append('.. ' + _frame_text(coconut))
    return ' < '.join(parts)


class HoldSampler:
    """Sample the stacks of all threads while one thread holds the service lock for a long time.

    The sampler thread starts at the first hold and sleeps until a hold passes SAMPLE_AFTER. It then reads
    sys._current_frames every SAMPLE_EVERY seconds, at most SAMPLE_LIMIT times for one hold. It counts the
    innermost frames of the holder and of each busy thread. A thread that waits for input or a lock is idle and
    is not counted.
    """

    def __init__(self):
        self.guard = threading.Lock()
        self.holds = {}
        self.thread = None

    def start(self, operation):
        token = object()
        with self.guard:
            self.holds[token] = dict(ident=threading.get_ident(), operation=operation, started=time.perf_counter(),
                                     samples=0, holder=collections.Counter(), others=collections.Counter())
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='lock-hold-sampler', daemon=True)
                self.thread.start()
        return token

    def stop(self, token):
        with self.guard:
            return self.holds.pop(token)

    def _run(self):
        while True:
            time.sleep(SAMPLE_EVERY)
            now = time.perf_counter()
            with self.guard:
                due = [hold for hold in self.holds.values()
                       if now - hold['started'] >= SAMPLE_AFTER and hold['samples'] < SAMPLE_LIMIT]
            if not due:
                continue
            frames = sys._current_frames()
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            me = threading.get_ident()
            for hold in due:
                hold['samples'] += 1
                for ident, frame in frames.items():
                    if ident == me:
                        continue
                    if ident == hold['ident']:
                        hold['holder'][_stack_text(frame)] += 1
                    elif (os.path.basename(frame.f_code.co_filename), frame.f_code.co_name) not in IDLE_FRAMES:
                        hold['others'][names.get(ident, str(ident)) + ': ' + _stack_text(frame)] += 1


_sampler = HoldSampler()


def _sample_summary(hold):
    if not hold['samples']:
        return {}
    return dict(samples=hold['samples'], holder=hold['holder'].most_common(3), others=hold['others'].most_common(8))


@contextmanager
def timed_lock(lock, operation):
    """Hold the lock and log a slow operation: its wait, its hold, and its own CPU time.

    cpu_ms is the CPU time of the holding thread, SQLite included. A hold much longer than cpu_ms waited: for the
    GIL while other threads ran Python, for disk I/O, or for memory pages. A long hold also logs stack samples.
    """
    started = time.perf_counter()
    acquired = started
    cpu_ms, hold = None, None
    try:
        with lock:
            acquired = time.perf_counter()
            cpu = time.thread_time()
            token = _sampler.start(operation)
            try:
                yield
            finally:
                cpu_ms = round((time.thread_time() - cpu) * 1000, 1)
                hold = _sampler.stop(token)
                held = time.perf_counter()
    finally:
        ended = time.perf_counter()
        if ended - started >= LOG_AFTER:
            data = dict(operation=operation, thread=threading.current_thread().name,
                        wait_ms=round((acquired - started) * 1000, 1),
                        hold_ms=round(((held if hold is not None else ended) - acquired) * 1000, 1), cpu_ms=cpu_ms)
            if hold is not None:
                data.update(_sample_summary(hold))
            logging.getLogger(__name__).warning('coconut.transaction_timing %s', json.dumps(data))


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
