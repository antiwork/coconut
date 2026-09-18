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
        from datetime import datetime, timezone
        data = dict(task_id=self.task_id, turn_id=self.turn_id, operation=self.operation,
                    stage=stage, at=datetime.now(timezone.utc).isoformat(),
                    elapsed_ms=round((time.perf_counter() - self.started) * 1000, 1), **measurements)
        if self.emit:
            self.emit('turn_trace', data)
        else:
            logging.getLogger(__name__).warning('coconut.turn_trace %s', json.dumps(data))
