"""The Personal budget: Antiwork pays for the model use of Personal, up to a monthly limit (docs/personal-budget.md).

The iPhone signs in with Apple and sends the identity token here. The Mac exchanges it at the relay for a budget token
of that Apple ID's account (relay/src/budget.js), and points only the Personal project's Hermes profile at the relay's
budget proxy (budget_native.py). The relay keeps the ledger. This Mac keeps the token in `<data>/personal-budget.json`
and the public state, without the token, in the setting `personal_budget`, which the snapshot shows and a poller keeps
current. Signing out puts the profile's own model back and revokes the token.

While Personal runs on the Hermes home (a fresh Mac without a `personal` profile), the person picks no other model for
the home in the Coconut chat: the budget holds its model until they sign out.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

LOG = logging.getLogger(__name__)
SETTING = 'personal_budget'
POLL_SECONDS = 300
HTTP_SECONDS = 20
NOT_AVAILABLE = 'The Personal budget needs Hermes on this Mac.'
NO_PERSONAL = 'This Coconut has no Personal project yet.'
HOLDS_MODEL = ('Personal uses the Antiwork budget, which sets its model. Sign out of the budget in Personal to choose '
               'another model.')


class BudgetError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class BudgetRelay:
    """The budget routes of the relay."""

    def __init__(self, origin):
        self.origin = origin.rstrip('/')

    @property
    def proxy_url(self):
        return self.origin + '/v1/budget/anthropic'

    def _request(self, method, path, *, token=None, body=None):
        headers = {'content-type': 'application/json', 'accept': 'application/json'}
        if token:
            headers['x-api-key'] = token
        request = urllib.request.Request(self.origin + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_SECONDS) as response:
                text = response.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read().decode())['error']['message']
            except Exception:
                message = 'The Personal budget did not answer. Try again.'
            raise BudgetError(error.code, message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BudgetError(503, 'The Mac could not reach the Personal budget. Try again.') from None

    def sign_in(self, identity_token):
        return self._request('POST', '/v1/budget/accounts', body={'identity_token': identity_token})

    def usage(self, token):
        return self._request('GET', '/v1/budget/usage', token=token)

    def revoke(self, token):
        return self._request('DELETE', '/v1/budget/token', token=token)


def public_usage(usage):
    return {key: usage.get(key) for key in ('month', 'spent_usd', 'limit_usd', 'paused', 'resets_at', 'model')}


class PersonalBudget:
    def _init_personal_budget(self, relay=None):
        from .push_relay import RELAY_URL
        self.budget_relay = relay or BudgetRelay(RELAY_URL)
        self.budget_lock = threading.Lock()
        self.budget_poller = None

    @property
    def _budget_file(self) -> Path:
        return Path(self.root) / 'personal-budget.json'

    def _budget_token(self):
        try:
            return json.loads(self._budget_file.read_text()).get('token')
        except (OSError, ValueError):
            return None

    def _save_budget_token(self, token, bot_id):
        path = self._budget_file
        temporary = path.with_name(path.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'token': token, 'bot_id': bot_id}, handle)
        os.replace(temporary, path)

    def _personal_bot(self):
        bot_id = next((bot_id for bot_id, key in self._project_ids().items() if key == 'personal'), None)
        return self._get('bots', bot_id) if bot_id else None

    def _budget_saved_state(self):
        return self._setting(SETTING, None) or {'signed_in': False}

    def personal_budget_state(self):
        """The public state, with the Personal project that the apps show it on."""
        personal = self._personal_bot()
        return dict(self._budget_saved_state(), bot_id=personal['id'] if personal else None)

    def _publish_budget(self, state):
        from .service_base import encoded
        state = {key: value for key, value in state.items() if key != 'bot_id'} | (
            {'bot_id': state['bot_id']} if state.get('signed_in') and state.get('bot_id') else {})
        with self.transaction():
            current = self.db.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
            if not current or json.loads(current[0]) != state:
                self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                (SETTING, encoded(state)))
                self._event('settings.updated', {SETTING: self.personal_budget_state()})
        return self.personal_budget_state()

    def personal_budget_holds_default_model(self) -> bool:
        """Personal on the Hermes home with the budget on: the home's model belongs to the budget."""
        if not self.personal_budget_state().get('signed_in'):
            return False
        bot = self._personal_bot()
        row = self.profiles.rows.get(bot['id']) if bot else None
        return bool(row) and Path(row['home']) == Path(self.hermes_home)

    def mutate_personal_budget(self, method, data):
        from .service_base import ServiceError
        if not hasattr(self.provider, 'native_settings'):
            raise ServiceError(409, NOT_AVAILABLE)
        with self.budget_lock:
            with self.lock:
                bot = self._personal_bot()
            if bot is None:
                raise ServiceError(409, NO_PERSONAL)
            try:
                if method == 'DELETE':
                    return self._budget_sign_out(bot)
                if not isinstance(data, dict) or not isinstance(data.get('identity_token'), str):
                    raise ServiceError(400, 'Sign in with Apple to use the Personal budget.')
                return self._budget_sign_in(bot, data['identity_token'])
            except BudgetError as error:
                raise ServiceError(error.status if error.status in (401, 403, 409, 429) else 503, str(error)) from None

    def _budget_sign_in(self, bot, identity_token):
        from .service_base import now
        account = self.budget_relay.sign_in(identity_token)
        token = account['token']
        # The Mac keeps the token before it changes the profile, so a sign-out can always revoke it.
        self._save_budget_token(token, bot['id'])
        self.provider.native_settings('budget_on', bots=[bot], token=token, base_url=self.budget_relay.proxy_url,
                                      model=account['model'])
        state = dict(public_usage(account), signed_in=True, bot_id=bot['id'], checked_at=now())
        self._start_budget_poller()
        return self._publish_budget(state)

    def _budget_sign_out(self, bot):
        self.provider.native_settings('budget_off', bots=[bot])
        token = self._budget_token()
        if token:
            try:
                self.budget_relay.revoke(token)
            except BudgetError as error:
                # The profile no longer uses the token. A token that the relay keeps is one of at most ten per account.
                LOG.warning('The relay did not revoke the Personal budget token: %s', error)
        self._budget_file.unlink(missing_ok=True)
        return self._publish_budget({'signed_in': False})

    def refresh_personal_budget(self):
        """Read the month's use from the relay. A refused token means the person signs in again."""
        from .service_base import now
        token = self._budget_token()
        state = self.personal_budget_state()
        if not token or not state.get('signed_in'):
            return state
        try:
            usage = self.budget_relay.usage(token)
        except BudgetError as error:
            if error.status != 401:
                return state
            return self._publish_budget(dict(state, signed_in=True, needs_sign_in=True, checked_at=now()))
        return self._publish_budget(dict(state, **public_usage(usage), needs_sign_in=False, checked_at=now()))

    def _start_budget_poller(self):
        if self.budget_poller is not None or not getattr(self, 'dispatcher', None):
            return
        self.budget_poller = threading.Thread(target=self._poll_personal_budget, name='coconut-budget-poll', daemon=True)
        self.budget_poller.start()

    def _poll_personal_budget(self):
        while not self.stopping.is_set():
            try:
                self.refresh_personal_budget()
            except Exception:
                LOG.exception('Could not read the Personal budget.')
            self.stopping.wait(POLL_SECONDS)
