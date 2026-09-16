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
