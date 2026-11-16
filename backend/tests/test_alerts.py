"""The alerts feed: the store, its events and its part of the state, the notification of a critical alert, and the
routes (coconut/alerts.py)."""
import base64
import http.client
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from coconut import alerts
from coconut.alerts import AlertError, AlertMissing, Alerts
from coconut.push import ALERT_BODY, ALERT_THREAD
from coconut.push_relay import feed_alert, group_text
from coconut.server import APIServer, TunnelServer
from coconut.service import Service, ServiceError
from test_push import RecordingTransport, pair_phone
from test_push_relay import EncryptedTransport
from test_service import ControlledProvider

ROUTE = {'route_id': 'a' * 64, 'send_token': 'b' * 64, 'public_key': base64.b64encode(b'x' * 32).decode()}
CRITICAL = dict(level='critical', source='cron:daily-ledger', title='daily-ledger failed',
                body='Request 20261116T1200 status=error')


class Clock:
    """The time of a store, which a test moves."""

    def __init__(self):
        self.time = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.time.isoformat().replace('+00:00', 'Z')

    def pass_(self, **age):
        self.time += timedelta(**age)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(alerts.SCHEMA)
        self.clock = Clock()
        self.events = []
        self.store = Alerts(self.db, self.clock, lambda kind, data: self.events.append((kind, dict(data))))

    def post(self, **fields):
        """Post an alert one minute after the one before, so the order of the alerts is the order of the posts."""
        self.clock.pass_(minutes=1)
        return self.store.post(**dict(dict(level='info', source='cron:test', title='A title'), **fields))

    def test_a_post_returns_the_alert_with_each_field(self):
        alert = self.post(level='critical', source=' watchdog:pr-compliance ', title=' Two PRs lack a review ',
                          body='coconut#511 and coconut#516', url='https://github.com/antiwork/coconut/pull/511',
                          dedupe_key='pr-compliance:2026-11-16')
        self.assertEqual({name: alert[name] for name in alert if name != 'id'}, dict(
            level='critical', source='watchdog:pr-compliance', title='Two PRs lack a review',
            body='coconut#511 and coconut#516', url='https://github.com/antiwork/coconut/pull/511',
            dedupe_key='pr-compliance:2026-11-16', created_at='2026-11-16T12:01:00Z', read_at=None, resolved_at=None))
        self.assertEqual(self.store.get(alert['id']), alert)
        self.assertEqual(self.events, [('alert.created', alert)])

    def test_each_level_posts_and_an_unknown_level_fails(self):
        for level in ('info', 'notice', 'critical'):
            self.assertEqual(self.post(level=level)['level'], level)
        for level in ('warning', 'CRITICAL', '', None, 3):
            with self.assertRaisesRegex(AlertError, 'level must be info, notice or critical'):
                self.post(level=level)

    def test_a_post_without_a_title_or_a_source_fails(self):
        for name in ('title', 'source'):
            for value in ('', '   ', None, 7):
                with self.assertRaisesRegex(AlertError, f'^{name} must'):
                    self.post(**{name: value})
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.events, [])

    def test_a_field_at_its_limit_posts_and_a_longer_one_fails(self):
        limits = dict(source=80, title=120, body=2000, dedupe_key=200)
        self.post(**{name: 'x' * limit for name, limit in limits.items()})
        for name, limit in limits.items():
            with self.assertRaisesRegex(AlertError, f'{name} must contain at most {limit} characters'):
                self.post(**{name: 'x' * (limit + 1)})
        self.assertEqual(len(self.store.list()), 1)

    def test_the_body_and_the_url_are_optional(self):
        alert = self.post()
        self.assertEqual((alert['body'], alert['url'], alert['dedupe_key']), ('', None, None))
        self.assertEqual(self.post(body='', url='', dedupe_key='')['url'], None)

    def test_only_an_http_address_is_a_url(self):
        for url in ('https://example.com/run/7', 'http://127.0.0.1:8080/logs?job=ledger'):
            self.assertEqual(self.post(url=url)['url'], url)
        for url in ('javascript:alert(1)', 'file:///etc/passwd', 'coconut://pair', 'ftp://example.com', 'example.com',
                    'https://', 'https://exa mple.com', 'https://[bad', 7):
            with self.assertRaisesRegex(AlertError, 'url must'):
                self.post(url=url)

    def test_a_repeat_of_a_key_inside_a_day_returns_the_first_alert(self):
        first = self.post(level='critical', title='daily-ledger failed', dedupe_key='ledger')
        self.clock.pass_(hours=23, minutes=58)
        again = self.post(level='critical', title='daily-ledger failed, second try', dedupe_key='ledger')
        self.assertEqual(again, first)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual([kind for kind, _ in self.events], ['alert.created'])

    def test_a_repeat_of_a_key_after_a_day_is_a_new_alert(self):
        first = self.post(dedupe_key='ledger')
        self.clock.pass_(hours=24)
        second = self.post(dedupe_key='ledger')
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual([kind for kind, _ in self.events], ['alert.created', 'alert.created'])
        # The window counts from the newest alert of the key.
        self.clock.pass_(hours=23)
        self.assertEqual(self.post(dedupe_key='ledger')['id'], second['id'])

    def test_a_repeat_returns_the_alert_as_it_is_now(self):
        first = self.post(dedupe_key='ledger')
        self.store.resolve(first['id'])
        again = self.post(dedupe_key='ledger')
        self.assertEqual(again['id'], first['id'])
        self.assertIsNotNone(again['resolved_at'])

    def test_alerts_without_a_key_and_with_other_keys_are_separate(self):
        ids = {self.post()['id'], self.post()['id'], self.post(dedupe_key='a')['id'], self.post(dedupe_key='b')['id']}
        self.assertEqual(len(ids), 4)

    def test_the_list_has_the_open_alerts_first_and_each_part_newest_first(self):
        first, second, third, fourth = (self.post(title=f'Alert {number}') for number in range(1, 5))
        self.store.resolve(fourth['id'])
        self.store.resolve(second['id'])
        self.assertEqual([alert['title'] for alert in self.store.list()], ['Alert 3', 'Alert 1', 'Alert 4', 'Alert 2'])
        self.assertEqual([alert['title'] for alert in self.store.list(limit=2)], ['Alert 3', 'Alert 1'])
        self.assertEqual(len(self.store.list(limit=0)), 1)
        self.assertEqual(len(self.store.list(limit=10_000)), 4)

    def test_two_alerts_of_one_moment_keep_the_order_of_their_posts(self):
        first = self.store.post('info', 'cron:test', 'First')
        second = self.store.post('info', 'cron:test', 'Second')
        self.assertEqual([alert['id'] for alert in self.store.list()], [second['id'], first['id']])

    def test_read_marks_one_alert_once(self):
        first, second = self.post(), self.post()
        self.assertEqual(self.store.unread_count(), 2)
        self.events.clear()
        self.clock.pass_(minutes=5)
        read = self.store.read(first['id'])
        self.assertEqual(read['read_at'], '2026-11-16T12:07:00Z')
        self.assertEqual(self.store.unread_count(), 1)
        self.clock.pass_(minutes=5)
        self.assertEqual(self.store.read(first['id']), read)
        self.assertEqual(self.events, [('alert.updated', read)])
        self.assertIsNone(self.store.get(second['id'])['read_at'])

    def test_read_all_marks_each_unread_alert_with_one_event(self):
        first = self.post()
        self.store.read(first['id'])
        self.post(), self.post()
        self.events.clear()
        self.assertEqual(self.store.read_all(), 2)
        self.assertEqual(self.store.unread_count(), 0)
        self.assertEqual(self.store.get(first['id'])['read_at'], '2026-11-16T12:01:00Z')
        self.assertEqual(self.events, [('alert.updated', {'read_all': True, 'read_at': self.clock()})])
        self.assertEqual(self.store.read_all(), 0)
        self.assertEqual(len(self.events), 1)

    def test_resolve_marks_the_alert_resolved_and_read_once(self):
        alert = self.post(level='critical')
        self.events.clear()
        resolved = self.store.resolve(alert['id'])
        self.assertEqual((resolved['resolved_at'], resolved['read_at']), (self.clock(), self.clock()))
        self.assertEqual(self.store.unread_count(), 0)
        self.clock.pass_(minutes=1)
        self.assertEqual(self.store.resolve(alert['id']), resolved)
        self.assertEqual(self.events, [('alert.updated', resolved)])

    def test_resolve_keeps_the_time_of_an_earlier_read(self):
        alert = self.post()
        read = self.store.read(alert['id'])
        self.clock.pass_(hours=1)
        self.assertEqual(self.store.resolve(alert['id'])['read_at'], read['read_at'])

    def test_an_unknown_alert_is_missing(self):
        for change in (self.store.read, self.store.resolve, self.store.get):
            with self.assertRaises(AlertMissing):
                change(str(uuid4()))

    def test_prune_deletes_the_alerts_older_than_thirty_days(self):
        old = self.post(title='Old')
        self.clock.pass_(days=29, hours=23)
        kept = self.post(title='Kept')
        self.assertEqual(len(self.store.list()), 2)
        self.clock.pass_(hours=1)
        self.assertEqual(self.store.prune(), 1)
        self.assertEqual([alert['id'] for alert in self.store.list()], [kept['id']])
        with self.assertRaises(AlertMissing):
            self.store.get(old['id'])

    def test_a_post_prunes_so_the_store_does_not_grow(self):
        self.post(title='Old')
        self.clock.pass_(days=31)
        self.post(title='New')
        self.assertEqual([alert['title'] for alert in self.store.list()], ['New'])

    def test_prune_keeps_five_hundred_rows_and_deletes_the_resolved_ones_first(self):
        oldest = self.post(title='Oldest, open')
        resolved = [self.post(title=f'Resolved {number}') for number in range(3)]
        for alert in resolved:
            self.store.resolve(alert['id'])
        # Without a prune in each post, to reach more rows than the store keeps.
        rows = [dict(self.store.get(oldest['id']), id=str(uuid4()), title=f'Fill {number}') for number in range(498)]
        self.db.executemany('INSERT INTO alerts(id,data) VALUES (?,?)', [(row['id'], json.dumps(row)) for row in rows])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 502)
        self.assertEqual(self.store.prune(), 2)
        kept = {alert['title'] for alert in self.store.list()} | {
            json.loads(row[0])['title'] for row in self.db.execute('SELECT data FROM alerts')}
        # The two oldest resolved alerts went. The oldest open alert stays.
        self.assertNotIn('Resolved 0', kept)
        self.assertNotIn('Resolved 1', kept)
        self.assertIn('Resolved 2', kept)
        self.assertIn('Oldest, open', kept)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 500)
        # With no resolved alert left to delete, the oldest open alert goes.
        newest = self.post(title='Newest')
        titles = {json.loads(row[0])['title'] for row in self.db.execute('SELECT data FROM alerts')}
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 500)
        self.assertNotIn('Resolved 2', titles)
        self.assertIn(newest['title'], titles)

    def test_the_state_has_fifty_open_alerts_and_twenty_resolved_of_the_last_week(self):
        long_ago = self.post(title='Resolved long ago')
        self.store.resolve(long_ago['id'])
        self.clock.pass_(days=8)
        for number in range(25):
            self.store.resolve(self.post(title=f'Resolved {number}')['id'])
        for number in range(55):
            self.post(title=f'Open {number}')
        state = self.store.state()
        titles = [alert['title'] for alert in state['items']]
        self.assertEqual(titles[:50], [f'Open {number}' for number in range(54, 4, -1)])
        self.assertEqual(titles[50:], [f'Resolved {number}' for number in range(24, 4, -1)])
        # The count is of the whole store, also of the alerts that the state leaves out.
        self.assertEqual(state['unread'], 55)

    def test_the_state_of_an_empty_store(self):
        self.assertEqual(self.store.state(), {'items': [], 'unread': 0})


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = Service(self.directory.name, ControlledProvider(), start_workers=False)
        self.addCleanup(self.service.close)

    def request(self, method, path='/v1/alerts', data=None, query=''):
        return self.service.alert_request(method, path, query, data or {})

    def post(self, **fields):
        status, alert = self.request('POST', data=dict(CRITICAL, **fields))
        self.assertEqual(status, 201)
        return alert

    def events(self, after=0):
        return [(event['type'], event['data']) for event in self.service.events(after)['events']
                if event['type'].startswith('alert.')]


class ServiceTests(ServiceCase):
    def test_each_write_adds_one_event_and_a_write_that_changes_nothing_adds_none(self):
        alert = self.post(dedupe_key='ledger')
        self.assertEqual(self.events(), [('alert.created', alert)])
        self.assertEqual(self.post(dedupe_key='ledger'), alert)
        self.assertEqual(len(self.events()), 1)
        read = self.request('POST', f'/v1/alerts/{alert["id"]}/read')[1]
        resolved = self.request('POST', f'/v1/alerts/{alert["id"]}/resolve')[1]
        self.request('POST', f'/v1/alerts/{alert["id"]}/read')
        self.request('POST', f'/v1/alerts/{alert["id"]}/resolve')
        self.assertEqual(self.request('POST', '/v1/alerts/read-all'), (200, {'unread': 0}))
        self.assertEqual(self.events(), [('alert.created', alert), ('alert.updated', read), ('alert.updated', resolved)])

    def test_a_refused_post_answers_400_with_the_reason_and_writes_nothing(self):
        cursor = self.service.snapshot()['cursor']
        for change, reason in ((dict(level='warning'), 'level must be info, notice or critical.'),
                               (dict(title=' '), 'title must not be empty.'),
                               (dict(title='x' * 121), 'title must contain at most 120 characters.'),
                               (dict(source='x' * 81), 'source must contain at most 80 characters.'),
                               (dict(body='x' * 2001), 'body must contain at most 2000 characters.'),
                               (dict(url='javascript:alert(1)'), 'url must be an http or https address.')):
            with self.assertRaises(ServiceError) as refused:
                self.request('POST', data=dict(CRITICAL, **change))
            self.assertEqual((refused.exception.status, str(refused.exception)), (400, reason))
        self.assertEqual(self.service.snapshot()['cursor'], cursor)
        self.assertEqual(self.request('GET'), (200, {'items': [], 'unread': 0}))

    def test_an_unknown_alert_and_an_unknown_route_answer_404(self):
        for method, path in (('POST', f'/v1/alerts/{uuid4()}/read'), ('POST', f'/v1/alerts/{uuid4()}/resolve'),
                             ('POST', f'/v1/alerts/{uuid4()}/delete'), ('GET', f'/v1/alerts/{uuid4()}'),
                             ('POST', '/v1/alerts/read-all/now')):
            with self.assertRaises(ServiceError) as refused:
                self.request(method, path)
            self.assertEqual(refused.exception.status, 404)

    def test_the_list_route_has_the_items_and_the_unread_count(self):
        first, second = self.post(title='First'), self.post(title='Second', level='info')
        self.request('POST', f'/v1/alerts/{first["id"]}/read')
        status, page = self.request('GET')
        self.assertEqual(status, 200)
        self.assertEqual(([alert['title'] for alert in page['items']], page['unread']), (['Second', 'First'], 1))
        self.assertEqual(len(self.request('GET', query='limit=1')[1]['items']), 1)
        with self.assertRaises(ServiceError) as refused:
            self.request('GET', query='limit=many')
        self.assertEqual(refused.exception.status, 400)

    def test_the_state_has_the_alerts_in_each_form_of_the_snapshot(self):
        self.assertEqual(self.service.snapshot()['alerts'], {'items': [], 'unread': 0})
        alert = self.post()
        for options in ({}, dict(content_only=True), dict(content_only=True, compact_history=True, recent_work=True)):
            self.assertEqual(self.service.snapshot(**options)['alerts'], {'items': [alert], 'unread': 1})

    def test_an_update_sends_the_alerts_only_when_they_changed(self):
        state = self.service.snapshot(content_only=True, compact_history=True)
        options = dict(collection_deltas=True, native_events=True, compact_history=True)
        alert = self.post(level='notice')
        page = self.service.updates(state['cursor'], **options)
        # The alert events are a delta, not a reset of the whole state.
        self.assertNotIn('reset', page)
        self.assertEqual(page['alerts'], {'items': [alert], 'unread': 1})
        resolved = self.request('POST', f'/v1/alerts/{alert["id"]}/resolve')[1]
        page = self.service.updates(page['cursor'], **options)
        self.assertEqual(page['alerts'], {'items': [resolved], 'unread': 0})
        idle = self.service.updates(page['cursor'], **options)
        self.assertNotIn('alerts', idle)
        self.assertNotIn('reset', idle)


class PushTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.device = pair_phone(self.service)
        self.transport = RecordingTransport()
        self.service.push.transport = self.transport
        self.register()

    def register(self, device=None, **more):
        data = dict(dict(token='a' * 64, environment='development', enabled=True), **more)
        return self.service.mutate('PUT', '/v1/device/push', data, str(uuid4()), device or self.device)

    def items(self):
        return self.service.db.execute(
            'SELECT device_id,kind,message_id,urgent,status FROM push_items ORDER BY rowid').fetchall()

    def deliver(self):
        while self.service.push.deliver_one():
            pass

    def test_an_info_alert_and_a_notice_alert_never_notify(self):
        self.post(level='info')
        self.post(level='notice')
        self.assertEqual(self.items(), [])
        self.deliver()
        self.assertEqual(self.transport.calls, [])

    def test_a_critical_alert_notifies_each_phone_once(self):
        second = pair_phone(self.service)
        self.register(second, token='b' * 64)
        alert = self.post()
        self.assertEqual(sorted(self.items()), sorted([(self.device, 'alert', alert['id'], 1, 'waiting'),
                                                       (second, 'alert', alert['id'], 1, 'waiting')]))
        self.deliver()
        self.assertEqual(sorted(call[0] for call in self.transport.calls), ['a' * 64, 'b' * 64])
        self.deliver()
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual({item[4] for item in self.items()}, {'sent'})

    def test_a_repeat_of_a_key_notifies_nothing_more(self):
        alert = self.post(dedupe_key='ledger')
        self.deliver()
        self.assertEqual(self.post(dedupe_key='ledger', title='daily-ledger failed again'), alert)
        self.deliver()
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_phone_without_a_relay_route_gets_no_text_of_the_alert(self):
        alert = self.post()
        self.deliver()
        payload = self.transport.calls[0][2]
        self.assertEqual(payload['aps']['alert'], {'title': 'Coconut', 'body': ALERT_BODY})
        self.assertEqual(payload['aps']['thread-id'], ALERT_THREAD)
        self.assertEqual(payload['coconut'], {'version': 1, 'device_id': self.device, 'kind': 'alert',
                                              'alert_id': alert['id']})
        self.assertNotIn('ledger', json.dumps(payload))

    def test_a_sealed_notification_has_the_title_the_source_and_the_start_of_the_body(self):
        relay = EncryptedTransport()
        self.service.push.relay = relay
        self.register(relay=ROUTE)
        self.post(body='**Request** 20261116T1200 status=error. ' + 'The ledger did not balance. ' * 20)
        self.deliver()
        alert = relay.sealed[0]['aps']['alert']
        self.assertEqual((alert['title'], alert['subtitle']), ('daily-ledger failed', 'cron:daily-ledger'))
        self.assertTrue(alert['body'].startswith('Request 20261116T1200 status=error. The ledger did not balance.'))
        self.assertEqual(len(alert['body']), 180)
        self.assertTrue(alert['body'].endswith('…'))
        self.assertEqual(len(relay.sent), 1)

    def test_the_alert_of_a_notification_without_a_body_names_the_level(self):
        alert = feed_alert(dict(CRITICAL, body=''))
        self.assertEqual(alert, {'title': 'daily-ledger failed', 'subtitle': 'cron:daily-ledger',
                                 'body': 'Critical alert'})

    def test_an_alert_that_was_read_or_resolved_before_the_send_does_not_notify(self):
        # The phone holds a notification outside its work hours. The person reads the feed first.
        for action in ('read', 'resolve'):
            alert = self.post(title=f'Alert to {action}')
            self.request('POST', f'/v1/alerts/{alert["id"]}/{action}')
        self.deliver()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual({item[4] for item in self.items()}, {'dropped'})

    def test_the_level_only_urgent_and_a_phone_without_preferences_still_get_a_critical_alert(self):
        self.register(preferences={'level': 'urgent_only', 'messages': False})
        self.post()
        self.deliver()
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_phone_with_notifications_off_gets_nothing(self):
        self.register(enabled=False)
        self.post()
        self.assertEqual(self.items(), [])

    def test_a_group_counts_its_alerts(self):
        self.assertEqual(group_text([{'kind': 'alert'}]), '1 alert is waiting')
        self.assertEqual(group_text([{'kind': 'alert'}, {'kind': 'alert'}, {'kind': 'decision'}]),
                         '1 decision and 2 alerts are waiting')

    def test_two_held_alerts_notify_as_one_group_without_a_task(self):
        # Outside the work hours of a phone with Nothing outside work hours, urgent items wait and then notify as a
        # group. A group of alerts only names no bot and no task.
        first, second = self.post(title='First'), self.post(title='Second')
        with self.service.transaction():
            items = self.service.push._items('device_id=?', (self.device,))
            self.service.push._queue(self.device, 'group:' + items[0]['event_key'], items, time.time())
        self.deliver()
        payload = self.transport.calls[0][2]
        self.assertEqual(payload['aps']['alert']['body'], '2 alerts are waiting')
        self.assertEqual((payload['coconut']['kind'], payload['coconut']['count'], payload['coconut']['task_id']),
                         ('group', 2, None))


class RouteTests(ServiceCase):
    """The routes on the main listener, with the token of the bridge file and with the token of a paired device."""

    def setUp(self):
        super().setUp()
        self.server = self.serve(APIServer(('127.0.0.1', 0), self.service))
        self.service.gateway_bridge.publish(f'http://127.0.0.1:{self.server.server_port}')
        self.token = json.loads((Path(self.directory.name) / 'gateway' / 'bridge.json').read_text())['token']

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        self.addCleanup(self.close, server, thread)
        return server

    def close(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)

    def send(self, method, path, data=None, token=..., *, server=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', (server or self.server).server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        token = self.token if token is ... else token
        if token:
            headers['Authorization'] = f'Bearer {token}'
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_a_producer_posts_reads_and_resolves_with_the_bridge_token(self):
        status, alert = self.send('POST', '/v1/alerts', dict(CRITICAL, dedupe_key='ledger'))
        self.assertEqual((status, alert['title'], alert['read_at']), (201, 'daily-ledger failed', None))
        status, again = self.send('POST', '/v1/alerts', dict(CRITICAL, dedupe_key='ledger'))
        self.assertEqual((status, again['id']), (201, alert['id']))
        self.assertEqual(self.send('GET', '/v1/alerts?limit=20'), (200, {'items': [alert], 'unread': 1}))
        status, read = self.send('POST', f'/v1/alerts/{alert["id"]}/read')
        self.assertEqual((status, read['read_at'] is not None, read['resolved_at']), (200, True, None))
        status, resolved = self.send('POST', f'/v1/alerts/{alert["id"]}/resolve')
        self.assertEqual((status, resolved['resolved_at'] is not None), (200, True))
        self.assertEqual(self.send('POST', '/v1/alerts/read-all'), (200, {'unread': 0}))
        self.assertEqual(self.send('GET', '/v1/alerts')[1], {'items': [resolved], 'unread': 0})

    def test_a_request_without_a_token_or_with_a_wrong_token_is_refused(self):
        for token in (None, 'wrong'):
            for method, path, data in (('POST', '/v1/alerts', CRITICAL), ('GET', '/v1/alerts', None),
                                       ('POST', '/v1/alerts/read-all', {})):
                self.assertEqual(self.send(method, path, data, token)[0], 401)
        self.assertEqual(self.service.snapshot()['alerts'], {'items': [], 'unread': 0})

    def test_a_refused_post_answers_400_and_an_unknown_alert_404(self):
        status, answer = self.send('POST', '/v1/alerts', dict(CRITICAL, level='fatal'))
        self.assertEqual((status, answer), (400, {'error': 'level must be info, notice or critical.'}))
        self.assertEqual(self.send('POST', '/v1/alerts', dict(CRITICAL, url='file:///etc/passwd'))[0], 400)
        self.assertEqual(self.send('POST', f'/v1/alerts/{uuid4()}/resolve', {})[0], 404)

    def test_a_paired_device_reads_and_marks_the_feed_with_its_own_token(self):
        device = self.service.pair({'code': self.service.pairing_code()})
        alert = self.post()
        status, page = self.send('GET', '/v1/alerts', token=device['token'])
        self.assertEqual((status, page), (200, {'items': [alert], 'unread': 1}))
        # The app sends an Idempotency-Key with each write. The alert routes do not need it.
        status, read = self.send('POST', f'/v1/alerts/{alert["id"]}/read', {}, device['token'],
                                 headers={'Idempotency-Key': str(uuid4())})
        self.assertEqual((status, read['id'], read['read_at'] is not None), (200, alert['id'], True))
        self.assertEqual(self.send('POST', '/v1/alerts/read-all', {}, device['token']), (200, {'unread': 0}))

    def test_the_bridge_token_does_not_open_the_feed_on_the_tunnel_listener(self):
        # The tunnel listener serves the phones. The token of the bridge file is for the tools of this Mac only.
        tunnel = self.serve(TunnelServer(0, self.service))
        status, _ = self.send('POST', '/v1/alerts', CRITICAL, server=tunnel, headers={'X-Coconut-Peer': 'a' * 64})
        self.assertEqual(status, 401)
        self.assertEqual(self.service.snapshot()['alerts']['items'], [])


if __name__ == '__main__':
    unittest.main()
