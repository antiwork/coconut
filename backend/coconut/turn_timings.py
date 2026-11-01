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
        """End a hold and return its summary. The sampler updates the counts under the same guard."""
        with self.guard:
            return _sample_summary(self.holds.pop(token))

    def _run(self):
        while True:
            time.sleep(SAMPLE_EVERY)
            self.sample()

    def sample(self):
        """Take one sample for each hold that passed SAMPLE_AFTER."""
        now = time.perf_counter()
        with self.guard:
            due = [token for token, hold in self.holds.items()
                   if now - hold['started'] >= SAMPLE_AFTER and hold['samples'] < SAMPLE_LIMIT]
        if not due:
            return
        frames = sys._current_frames()
        try:
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            me = threading.get_ident()
            stacks = {ident: (os.path.basename(frame.f_code.co_filename), frame.f_code.co_name, _stack_text(frame))
                      for ident, frame in frames.items() if ident != me}
        finally:
            # Frames keep the locals of running calls. Drop them before the sleep.
            del frames
        with self.guard:
            for token in due:
                hold = self.holds.get(token)
                if hold is None:
                    continue
                hold['samples'] += 1
                for ident, (file, function, stack) in stacks.items():
                    if ident == hold['ident']:
                        hold['holder'][stack] += 1
                    elif (file, function) not in IDLE_FRAMES:
                        hold['others'][names.get(ident, str(ident)) + ': ' + stack] += 1


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
            token = _diagnose(_sampler.start, operation)
            try:
                yield
            finally:
                cpu_ms = round((time.thread_time() - cpu) * 1000, 1)
                held = time.perf_counter()
                if token is not None:
                    hold = _diagnose(_sampler.stop, token)
    finally:
        ended = time.perf_counter()
        if ended - started >= LOG_AFTER:
            _diagnose(_log_hold, operation, started, acquired, held if cpu_ms is not None else ended, cpu_ms, hold)


def _diagnose(function, *args):
    """Run a diagnostic step. Its error must never fail the operation that holds the lock: the operation may have
    committed already."""
    try:
        return function(*args)
    except Exception:
        logging.getLogger(__name__).exception('A lock hold diagnostic failed.')
        return None


def _log_hold(operation, started, acquired, held, cpu_ms, hold):
    data = dict(operation=operation, thread=threading.current_thread().name,
                wait_ms=round((acquired - started) * 1000, 1), hold_ms=round((held - acquired) * 1000, 1), cpu_ms=cpu_ms)
    if hold:
        data.update(hold)
    logging.getLogger(__name__).warning('coconut.transaction_timing %s', json.dumps(data))


class TurnTrace:
    """Correlate execution stages without recording conversation content."""
    def __init__(self, task, operation):
        self.task_id = task['id']
        self.turn_id = task.get('_native_turn_id')
        self.operation = operation
        self.started = time.perf_counter()
        self.emitted = set()

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
        logging.getLogger(__name__).warning('coconut.turn_trace %s', json.dumps(data))
