"""Measure real service dispatch with a fixed maintenance delay and a fixture provider."""
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/tests'))
from test_native_kanban import Provider
from coconut.service import Service

logging.disable(logging.WARNING)
samples = []
for _ in range(5):
    with tempfile.TemporaryDirectory() as root:
        provider = Provider(root)
        provider.supports_native_outcomes = True
        service = Service(root, provider, start_workers=False)
        entered = threading.Event()
        def maintenance():
            entered.set()
            time.sleep(2)
        try:
            bot = service.mutate('POST', '/v1/bots', {'name': 'Latency fixture'}, str(uuid4()))
            with patch.object(service, 'tick_routines', side_effect=maintenance):
                service.start()
                assert entered.wait(3)
                started = time.perf_counter()
                task = service.mutate('POST', '/v1/bots/' + bot['id'] + '/messages', {'text': 'What is 17 times 19?'}, str(uuid4()))
                while task['id'] not in provider.started:
                    if time.perf_counter() - started > 10:
                        raise TimeoutError('The service did not dispatch.')
                    time.sleep(.001)
                samples.append(round((time.perf_counter() - started) * 1000, 2))
        finally:
            service.close()
print(json.dumps(dict(case='direct_message_during_2000ms_maintenance', model='fixture', samples_ms=samples,
                      median_ms=statistics.median(samples)), indent=2))
