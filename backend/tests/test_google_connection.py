import hashlib
import base64
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from coconut.google_connection import GoogleConnections, GoogleProfile, GoogleError, GoogleAuthorizationError, SCOPES, read_json, write_json


class MemoryProfile(GoogleProfile):
    saved = {}
    def __init__(self, home):
        super().__init__(home, executable='/test/gog')
    def save_tokens(self, tokens):
        self.saved[str(self.home)] = dict(tokens)
    def tokens(self, email):
        return dict(self.saved[str(self.home)])
    def run(self, args, payload=None):
        self.saved.pop(str(self.home), None)


class GoogleConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = GoogleConnections(self.temp.name, '123-example.apps.googleusercontent.com', MemoryProfile)
        self.profile = self.manager.profile()
        self.network = patch('coconut.google_connection.request_json')
        self.request = self.network.start()
        self.request.side_effect = [self.grant(), dict(email='owner@example.com', email_verified=True, sub='owner')]

    def tearDown(self):
        self.network.stop()
        self.temp.cleanup()
        MemoryProfile.saved.clear()

    def grant(self):
        return dict(access_token='access-private', refresh_token='refresh-private', expires_in=3600, scope=' '.join(SCOPES))

    def start(self, key=None):
        return self.manager.start('phone', key or str(uuid4()))

    def finish(self, flow):
        return self.manager.finish('phone', flow['state'], 'one-use-code')

    def test_system_gog_cannot_bypass_the_pinned_private_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'connections/google/home'
            system_bin = Path(folder) / 'bin'
            system_bin.mkdir()
            old = system_bin / 'gog'; old.write_text('#!/bin/sh\nexit 0\n'); old.chmod(0o700)
            with patch.dict('os.environ', {'PATH': str(system_bin)}):
                profile = GoogleProfile(home)
            self.assertIsNone(profile.executable)
            self.assertEqual(profile.tools, Path(folder) / 'google-tools/0.39.1')

    def test_pkce_and_phone_redirect(self):
        flow = self.start()
        query = parse_qs(urlsplit(flow['authorization_url']).query)
        pending = read_json(self.profile.root / 'pending.json')
        challenge = base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['redirect_uri'], ['com.googleusercontent.apps.123-example:/oauthredirect'])
        self.assertNotIn(pending['verifier'], str(flow))
        self.assertEqual((self.profile.root / 'pending.json').stat().st_mode & 0o777, 0o600)

    def test_retry_start_reuses_flow_and_new_start_invalidates_previous(self):
        key = str(uuid4()); first = self.start(key)
        self.assertEqual(first, self.start(key))
        self.start()
        with self.assertRaises(GoogleError): self.finish(first)
        self.request.assert_not_called()

    def test_callback_is_bound_to_device_and_state(self):
        flow = self.start()
        for device, state in [('other-phone', flow['state']), ('phone', 'wrong')]:
            with self.assertRaises(GoogleError): self.manager.finish(device, state, 'code')
        self.request.assert_not_called()

    def test_concurrent_bots_share_one_refresh(self):
        from concurrent.futures import ThreadPoolExecutor
        self.finish(self.start())
        MemoryProfile.saved[str(self.profile.home)]['access_token_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.request.reset_mock()
        self.request.side_effect = [dict(access_token='shared-access', expires_in=3600)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: self.manager.profile().access(), range(8)))
        self.assertEqual(results, [('shared-access', 'owner@example.com')] * 8)
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.profile.home, Path(self.temp.name) / 'connections/google/home')

    def test_expired_flow_does_not_exchange_code(self):
        flow = self.start(); path = self.profile.root / 'pending.json'
        saved = read_json(path); saved['expires'] = time.time() - 1; write_json(path, saved)
        with self.assertRaises(GoogleError): self.finish(flow)
        self.request.assert_not_called()

    def test_finish_survives_response_loss_and_service_restart(self):
        flow = self.start(); result = self.finish(flow)
        self.assertTrue(result['connected']); self.assertEqual(result['email'], 'owner@example.com')
        self.assertNotIn('private', str(result))
        restarted = GoogleConnections(self.temp.name, self.manager.client_id, MemoryProfile)
        self.assertEqual(result, restarted.finish('phone', flow['state'], 'one-use-code'))
        self.assertEqual(self.request.call_count, 2)
        self.assertFalse((self.profile.root / 'pending.json').exists())
        self.assertNotIn('refresh-private', (self.profile.root / 'connection.json').read_text())

    def test_replacing_account_removes_previous_credentials_before_saving_new_account(self):
        self.finish(self.start())
        self.request.side_effect = [self.grant(), dict(email='second@example.com', email_verified=True, sub='second')]
        with patch.object(MemoryProfile, 'run', autospec=True, side_effect=lambda profile, args, payload=None: profile.saved.pop(str(profile.home), None)) as remove:
            self.assertEqual(self.finish(self.start())['email'], 'second@example.com')
            self.assertEqual(remove.call_args.args[1], ['auth', 'remove', 'owner@example.com', '--force'])
            self.manager.disconnect()
            self.assertEqual(remove.call_args.args[1], ['auth', 'remove', 'second@example.com', '--force'])
        self.assertFalse(MemoryProfile.saved)

    def test_missing_scope_does_not_connect(self):
        self.request.side_effect = [dict(self.grant(), scope=SCOPES[2])]
        with self.assertRaises(GoogleError): self.finish(self.start())
        self.assertFalse(self.profile.status()['connected'])
        self.assertFalse(MemoryProfile.saved)

    def test_unverified_identity_does_not_connect(self):
        self.request.side_effect = [self.grant(), dict(email='owner@example.com', email_verified=False, sub='owner')]
        with self.assertRaises(GoogleError): self.finish(self.start())
        self.assertFalse(self.profile.status()['connected'])

    def test_uncertain_exchange_is_not_repeated(self):
        flow = self.start(); self.request.side_effect = GoogleError('Temporary failure')
        with self.assertRaises(GoogleError): self.finish(flow)
        with self.assertRaises(GoogleError): self.finish(flow)
        self.assertEqual(self.request.call_count, 1)

    def test_disconnect_cancels_pending_and_retry_cannot_remove_new_connection(self):
        first = self.start(); key = str(uuid4())
        result = self.manager.disconnect('phone', key)
        with self.assertRaises(GoogleError): self.finish(first)
        second = self.start(); self.finish(second)
        self.assertEqual(result, self.manager.disconnect('phone', key))
        self.assertTrue(self.profile.status()['connected'])

    def test_refresh_uses_original_client_and_preserves_refresh_token(self):
        self.finish(self.start())
        stored = MemoryProfile.saved[str(self.profile.home)]
        stored['access_token_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.request.side_effect = [dict(access_token='new-private', expires_in=3600)]
        token, email = self.profile.access()
        self.assertEqual((token, email), ('new-private', 'owner@example.com'))
        self.assertEqual(self.request.call_args.args[1]['client_id'], self.manager.client_id)
        self.assertEqual(MemoryProfile.saved[str(self.profile.home)]['refresh_token'], 'refresh-private')
        self.request.reset_mock()
        self.profile.access()
        self.request.assert_not_called()

    def test_failed_refresh_keeps_connection_for_retry(self):
        self.finish(self.start())
        MemoryProfile.saved[str(self.profile.home)]['access_token_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.request.side_effect = GoogleError('Network failure')
        with self.assertRaises(GoogleError): self.profile.access()
        self.assertTrue(self.profile.status()['connected'])
        self.assertIn(str(self.profile.home), MemoryProfile.saved)

    def test_revoked_grant_requests_reconnection(self):
        self.finish(self.start())
        MemoryProfile.saved[str(self.profile.home)]['access_token_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.request.side_effect = GoogleAuthorizationError('Revoked')
        with self.assertRaises(GoogleAuthorizationError): self.profile.access()
        self.assertTrue(self.profile.status()['needs_reconnect'])
