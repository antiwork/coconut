"""The Personal budget: Antiwork pays for the model use of Personal, up to a monthly limit (docs/personal-budget.md).

Antiwork trusts people, so the budget has no sign-in. A tap on Use the Antiwork budget in a Personal chat asks this
Mac to turn it on. The Mac asks the relay for a new account and its budget token (relay/src/budget.js), and points only
the Personal project's Hermes profile at the relay's budget proxy (budget_native.py). The relay keeps the ledger. This
Mac keeps the token in `<data>/personal-budget.json` and the public state, without the token, in the setting
`personal_budget`, which the snapshot shows and a poller keeps current. Turning it off puts the profile's own model
back and revokes the token. The state keeps the names of the sign-in it once had (`signed_in`, `needs_sign_in`),
because the apps that are out read them.

While Personal runs on the Hermes home (a fresh Mac without a `personal` profile), the person picks no other model for
the home in the Coconut chat: the budget holds its model until they turn it off.

The relay offers the budget only once it holds Antiwork's key. Until then the apps show no line and no button: the
poller asks the relay every five minutes, keeps the answer in the setting `personal_budget_offered`, and the state
names no Personal project for a person who has not turned it on.
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
# The last answer of the relay to whether it offers the budget: true, false, or no row before the first answer.
OFFERED = 'personal_budget_offered'
POLL_SECONDS = 300
HTTP_SECONDS = 20
# Cloudflare refuses Python's own user agent with 403 (Error 1010), so each call to the relay names Coconut, as the
# GitHub calls do.
HEADERS = {'accept': 'application/json', 'user-agent': 'Coconut'}
NOT_AVAILABLE = 'The Personal budget needs Hermes on this Mac.'
NO_PERSONAL = 'This Coconut has no Personal project yet.'
HOLDS_MODEL = ('Personal uses the Antiwork budget, which sets its model. Turn off the Antiwork budget in Personal to '
               'choose another model.')


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
        headers = {'content-type': 'application/json', **HEADERS}
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

    def sign_in(self):
        """Open a new account. The relay asks for no proof of who asks."""
        return self._request('POST', '/v1/budget/accounts', body={})

    def usage(self, token):
        return self._request('GET', '/v1/budget/usage', token=token)

    def revoke(self, token):
        return self._request('DELETE', '/v1/budget/token', token=token)

    def offered(self):
        """Whether the relay offers the budget, or None when it gave no clear answer.

        The relay answers every budget route with 503 until it holds Antiwork's key, and a request without a token with
        401 after that. This request carries no token and changes nothing at the relay."""
        request = urllib.request.Request(self.origin + '/v1/budget/usage', headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_SECONDS):
                return None
        except urllib.error.HTTPError as error:
            return {401: True, 503: False}.get(error.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None


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
        """The public state, with the Personal project that the apps show it on.

        The apps show the line and the button only in the chats of that project (PersonalBudget.shows). While the relay
        does not offer the budget, the state names no project for a person who has not turned it on, so every app hides
        both, also the builds that came out before this rule. A person who turned it on keeps the line and its menu."""
        personal = self._personal_bot()
        saved = self._budget_saved_state()
        shown = saved.get('signed_in') or self._setting(OFFERED) is True
        return dict(saved, bot_id=personal['id'] if personal and shown else None)

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
                # The iPhone builds with Sign in with Apple still send an identity token, which nothing reads now.
                return self._budget_sign_in(bot)
            except BudgetError as error:
                raise ServiceError(error.status if error.status in (401, 403, 409, 429) else 503, str(error)) from None

    def _budget_sign_in(self, bot):
        from .service_base import now
        state = self.personal_budget_state()
        # A budget that is on stays on: a second tap or a retry opens no second account.
        if self._budget_token() and state.get('signed_in') and not state.get('needs_sign_in'):
            return state
        account = self.budget_relay.sign_in()
        token = account['token']
        # The Mac keeps the token before it changes the profile, so turning it off can always revoke it.
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
        """Read the month's use from the relay. A refused token means the person turns it on again."""
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

    def refresh_budget_offer(self):
        """Ask the relay whether it offers the budget. When the answer changed, the apps read the whole state again."""
        from .service_base import encoded
        # Without Hermes the budget cannot work (NOT_AVAILABLE), so the apps keep it hidden and the relay is not asked.
        if not hasattr(self.provider, 'native_settings'):
            return
        offered = self.budget_relay.offered()
        if offered is None:
            return
        with self.transaction():
            current = self.db.execute('SELECT value FROM settings WHERE key=?', (OFFERED,)).fetchone()
            if current and json.loads(current[0]) == offered:
                return
            self.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (OFFERED, encoded(offered)))
            # The updates of the apps carry the budget only in a whole state. An event type that the updates do not
            # know (state_sync.py) sends the next update as a whole state, so the line shows or hides at once.
            self._event('personal_budget.updated', {SETTING: self.personal_budget_state()})

    def _start_budget_poller(self):
        if self.budget_poller is not None or not getattr(self, 'dispatcher', None):
            return
        self.budget_poller = threading.Thread(target=self._poll_personal_budget, name='coconut-budget-poll', daemon=True)
        self.budget_poller.start()

    def _poll_personal_budget(self):
        while not self.stopping.is_set():
            for step in (self.refresh_budget_offer, self.refresh_personal_budget):
                try:
                    step()
                except Exception:
                    LOG.exception('Could not read the Personal budget.')
            self.stopping.wait(POLL_SECONDS)
