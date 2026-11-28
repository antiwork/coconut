"""The to-dos of Coconut: the open issues of one GitHub repository, in three lists by label.

The person keeps every to-do of the business as an issue of antiwork/gumroad-private, which the team and the agents
work in, and nothing else: no other repository and no store of Coconut holds a to-do. Its label names its list:
`finance`, `support` or `product`, the three tabs of the iPhone, each with its own count. An issue with two of the labels
is in both lists, and an issue with none of them is a to-do of Product, the list of the repository's issues before the
split. COCONUT_TODO_REPOSITORY names another repository of the owner.

The Mac reads and writes with its GitHub login (github_access.py), so a move in Coconut is a label on GitHub, and a
change on GitHub shows at the next read. A Mac whose login cannot read the repository, or that has none, reads Gumclaw's
copy of the list instead: a team page (PAGE_PREFIX and the name of the repository) that coconut-pages.py of the
dashboards repository shares at each publish. Nobody connects GitHub in Coconut, so the lists show on every Mac of the
team (the person, 2026-11-28). The copy is read only: its answer has `source` "gumclaw", and a move needs a GitHub login.
The Library does not list the page (TeamStore.library).

    GET   /v1/todos[?fresh=1]         the open to-dos, newest activity first
    PATCH /v1/todos {number, area}    moves a to-do to another list: the label of the new list replaces the old one

The number travels in the body, so the request log keeps the route without it.

Coconut creates no to-do. The person removed to-do creation from Coconut (2026-11-26): "Remove todo creation from
coconut. Agent does that only as part of getting blocked on doing something". An agent files a to-do on GitHub only when
the work that it does cannot go on until someone acts (the hint of the Coconut platform, hermes_plugin/coconut, HINT),
so no route, button or sheet of Coconut makes one, and POST /v1/todos answers 410 to older builds that still have a +.
Do not add a way to create one back.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from urllib.parse import quote, urlencode

from .business import work_repository
from .service_base import ServiceError, encoded

AREAS = ('finance', 'support', 'product')
# An issue without the label of a list.
DEFAULT_AREA = 'product'
# The labels that a move, a decision of a review (risk_reviews) or a handoff of Support (support_handoffs) adds when the
# repository lacks them.
LABELS = {'finance': ('1A7F37', 'Coconut to-do list Finance: money, payouts, risk, tax, accounting, marketing'),
          'support': ('0969DA', 'Coconut to-do list Support: customers, sellers, privacy requests, content gaps'),
          'product': ('8250DF', 'Coconut to-do list Product: bugs, features and the app (the default)')}
# Each device reads the list on its own, every few minutes and at each start, and a write drops it.
LIST_SECONDS = 15
MAX_TODOS = 300
PAGE = 50
NUMBER = re.compile(r'[1-9][0-9]{0,9}')
# The key of the team page of Gumclaw's copy of the list is this and the name of the repository.
PAGE_PREFIX = 'todos-'
# The copy is the JSON of a fenced block of the page: the repository and its open issues as GitHub lists them, cut to
# the fields that `summary` reads.
COPY = re.compile(r'```json\n(.*)\n```', re.S)
# The answer to a device that asks Coconut for a new to-do (see the top of this file).
NO_CREATION = 'Coconut no longer creates to-dos. An agent files one on GitHub when it gets blocked.'


def areas_of(labels) -> list[str]:
    """The lists of an issue, from its label names: each list whose label it has, else Product."""
    names = {name.lower() for name in labels}
    return [area for area in AREAS if area in names] or [DEFAULT_AREA]


def _names(issue) -> list[str]:
    return [label['name'] for label in issue.get('labels') or [] if isinstance(label, dict) and isinstance(label.get('name'), str)]


def _color(value) -> str | None:
    return value.lower() if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{6}', value) else None


def summary(issue: dict, login: str | None) -> dict:
    """The fields of a to-do that a row shows. `mine`: the issue is assigned to the connected account."""
    labels = [dict(name=label['name'][:50], color=_color(label.get('color')))
              for label in issue.get('labels') or [] if isinstance(label, dict) and isinstance(label.get('name'), str)]
    assignees = [person['login'] for person in issue.get('assignees') or [] if isinstance(person, dict) and isinstance(person.get('login'), str)]
    own = (login or '').lower()
    title = issue.get('title') if isinstance(issue.get('title'), str) else ''
    return dict(number=issue['number'], title=title[:1000], areas=areas_of(_names(issue)), labels=labels,
                assignees=assignees, author=(issue.get('user') or {}).get('login'),
                comments=issue.get('comments') if isinstance(issue.get('comments'), int) else 0,
                created_at=issue.get('created_at'), updated_at=issue.get('updated_at'), url=issue.get('html_url'),
                mine=bool(own) and own in (name.lower() for name in assignees))


def _area(data) -> str:
    area = data.get('area')
    if area not in AREAS:
        raise ServiceError(400, 'Choose Finance, Support or Product.')
    return area


def _number(value) -> int:
    value = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(value, str) or not NUMBER.fullmatch(value) or int(value) >= 2**31:
        raise ServiceError(400, 'Choose a to-do by its number.')
    return int(value)


class GitHubTodos:
    def __init__(self, business, root=None):
        self.business = business
        # The data folder of the Mac, whose database holds Gumclaw's copy of the list.
        self.root = root
        self.lock = threading.Lock()
        self.cache = {}

    @property
    def repository(self) -> str:
        return f'{self.business.owner}/{work_repository()}'

    def _path(self) -> str:
        return 'repos/' + self.repository

    def login(self) -> str | None:
        return self.business.connection.account().get('login')

    def list(self, fresh: bool = False) -> dict:
        try:
            identity = hashlib.sha256(self.business._token().encode()).hexdigest()
        except ServiceError as error:
            if error.status != 409:
                raise
            return self._shared(error)
        with self.lock:
            cached = self.cache.get(identity)
            if not fresh and cached and cached[0] > time.monotonic():
                return cached[1]
        login, items, page = self.login(), [], 1
        try:
            while len(items) < MAX_TODOS:
                values = self.business._call('GET', self._path() + '/issues?' + urlencode(
                    dict(state='open', sort='updated', direction='desc', per_page=PAGE, page=page)))
                if not isinstance(values, list):
                    raise ServiceError(502, 'GitHub returned an invalid list.')
                # GitHub's list of issues has the pull requests too.
                items += [summary(issue, login) for issue in values if isinstance(issue, dict) and 'pull_request' not in issue]
                if len(values) < PAGE:
                    break
                page += 1
        except ServiceError as error:
            # GitHub refused the login (409), the login has no access to the repository (404), or its rate limit ran
            # out (403).
            if error.status not in (403, 404, 409):
                raise
            return self._shared(error, login)
        result = dict(repository=self.repository, login=login, items=items[:MAX_TODOS])
        with self.lock:
            self.cache = {identity: (time.monotonic() + LIST_SECONDS, result)}
        return result

    def _shared(self, error: ServiceError, login: str | None = None) -> dict:
        """Gumclaw's copy of the list (see the top of this file), with the rows of a read of GitHub. Without a copy, the
        error of the read stands. The newest valid page of an active team has it, from any bot: the room's members are
        trusted as the Library trusts them."""
        import contextlib
        import sqlite3
        from .service_base import now
        from .team_store import TeamStore
        content = ''
        if self.root is not None:
            with contextlib.closing(sqlite3.connect((self.root / 'coconut.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                store = TeamStore(db)
                row = store.keyed_page(PAGE_PREFIX + work_repository().lower(), now())
                content = store.content(row['room'], row['entry']) if row else ''
        found = COPY.search(content or '')
        try:
            data = json.loads(found.group(1)) if found else None
        except ValueError:
            data = None
        issues = data.get('issues') if isinstance(data, dict) else None
        if not isinstance(issues, list) or str(data.get('repository') or '').lower() != self.repository.lower():
            raise error
        items = [summary(issue, login) for issue in issues
                 if isinstance(issue, dict) and type(issue.get('number')) is int and 'pull_request' not in issue]
        return dict(repository=self.repository, login=login, items=items[:MAX_TODOS], source='gumclaw')

    def forget(self) -> None:
        with self.lock:
            self.cache = {}

    def move(self, data: dict) -> dict:
        if set(data) != {'number', 'area'}:
            raise ServiceError(400, 'Send the number of the to-do and its new list.')
        number, area = _number(data['number']), _area(data)
        path = self._path() + f'/issues/{number}'
        issue = self.business._call('GET', path)
        if not isinstance(issue, dict) or 'pull_request' in issue:
            raise ServiceError(400, 'This is a pull request, not a to-do.')
        names = _names(issue)
        others = [name for name in names if name.lower() in AREAS and name.lower() != area]
        if area not in (name.lower() for name in names):
            self.business.ensure_label(area, *LABELS[area])
            # One label at a time, so a label that someone adds meanwhile stays.
            self.business._call('POST', path + '/labels', dict(labels=[area]))
        for name in others:
            try:
                self.business._call('DELETE', path + '/labels/' + quote(name, safe=''))
            except ServiceError as error:
                if error.status != 404:
                    raise
        self.forget()
        return summary(self.business._call('GET', path), self.login())


def mutate_todos(service, method, data, request_key, fingerprint):
    """A move from a device. A repeat with the same key gets the saved answer, which is saved only after GitHub confirmed
    the write (business.mutate_business). Coconut creates no to-do (see the top of this file)."""
    if method == 'POST':
        raise ServiceError(410, NO_CREATION)
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this to-do.')
    todos = service.todos
    fingerprint = hashlib.sha256((fingerprint + str(todos.login())).encode()).hexdigest()
    with service.business.write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], fingerprint):
                raise ServiceError(409, 'This request key belongs to a different change.')
            return json.loads(receipt[1])
        if method == 'PATCH':
            result = todos.move(data)
        else:
            raise ServiceError(404, 'This to-do action was not found.')
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, fingerprint, encoded(result)))
        return result
