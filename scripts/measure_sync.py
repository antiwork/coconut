"""Measure the reads that a paired app repeats, with stored data of the size of a busy Mac.

The fixture has synthetic records: five bots with 14,991, 10,275, 2,209, 203, and 141 messages, as the fixture of
scripts/test_app_performance.py. The script starts no Hermes and no worker. Run it with
`PYTHONPATH=backend python3 scripts/measure_sync.py`.
"""
import json
import logging
import statistics
import tempfile
import threading
import time
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service

HISTORY = [14991, 10275, 2209, 203, 141]
TEXT = ('The check retries each request once and records the receipt before it sends the next one, so a restart '
        'cannot repeat a charge while the queue drains. ') * 6


def median_ms(work, runs=15):
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        work()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def fill(service):
    bots = [service.mutate('POST', '/v1/bots', {'name': f'Agent {index + 1}'}, str(uuid4())) for index in range(len(HISTORY))]
    for bot, count in zip(bots, HISTORY):
        with service.transaction():
            for index in range(count):
                user = index % 2 == 0
                message = service._message(bot['id'], f'Question {index}' if user else TEXT, role='user' if user else 'assistant')
                if user:
                    message['client_request_id'] = str(uuid4())
                service._put('messages', message)
    return bots


def delivery_ms(service, bot, runs=30):
    """The time from the commit of a message to the answer of a waiting GET /v1/updates."""
    samples = []
    for index in range(runs):
        after = service._cursor()
        done = []
        waiter = threading.Thread(target=lambda: done.append((service.updates(after, 5, collection_deltas=True, native_events=True),
                                                              time.perf_counter())))
        waiter.start()
        # The waiter is in its wait at a different point of the poll interval on each run.
        time.sleep(.15 + index % 10 * .011)
        with service.transaction():
            service._message(bot['id'], 'A streamed word', streaming=True)
        committed = time.perf_counter()
        waiter.join()
        samples.append((done[0][1] - committed) * 1000)
    return dict(median=round(statistics.median(samples), 2), worst=round(max(samples), 2))


def main():
    logging.disable(logging.WARNING)
    with tempfile.TemporaryDirectory() as root:
        service = Service(root, FixtureProvider(root), start_workers=False)
        try:
            bots = fill(service)
            code = __import__('coconut.server', fromlist=['create_pairing_code']).create_pairing_code(root)
            token = service.pair({'code': code, 'device_name': 'Measure'})['token']
            result = {'messages': sum(HISTORY)}
            result['authenticate_ms'] = median_ms(lambda: service.authenticate(token))
            cursor = service._cursor()
            result['updates_idle_ms'] = median_ms(lambda: service.updates(cursor, 0, collection_deltas=True, native_events=True))
            with service.transaction():
                service._message(bots[0]['id'], 'A streamed word', streaming=True)
            result['updates_streamed_word_ms'] = median_ms(
                lambda: service.updates(cursor, 0, collection_deltas=True, native_events=True))
            cursor = service._cursor()
            with service.transaction():
                message = service._message(bots[0]['id'], 'A sent message', role='user')
                message['client_request_id'] = str(uuid4())
                service._put('messages', message)
            result['updates_sent_message_ms'] = median_ms(
                lambda: service.updates(cursor, 0, collection_deltas=True, native_events=True))
            result['recent_state_ms'] = median_ms(
                lambda: service.snapshot(content_only=True, compact_history=True, recent_work=True), runs=7)
            result['history_page_ms'] = median_ms(lambda: service.message_page(bots[0]['id']))
            result['search_ms'] = median_ms(lambda: service.search_conversations('receipt'), runs=5)
            result['search_without_match_ms'] = median_ms(lambda: service.search_conversations('no such text'), runs=5)
            result['update_delivery_ms'] = delivery_ms(service, bots[1])
            print(json.dumps(result, indent=2))
        finally:
            service.close()


if __name__ == '__main__':
    main()
