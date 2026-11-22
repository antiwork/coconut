"""The to-dos of Coconut: the open issues of one GitHub repository, in three lists by label.

The person keeps every to-do of the business as an issue of antiwork/gumroad-private, which the team and the agents
work in, and nothing else: no other repository and no store of Coconut holds a to-do. Its label names its list:
`finance`, `support` or `product`, the three tabs of the iPhone, each with its own count. An issue with two of the labels
is in both lists, and an issue with none of them is a to-do of Product, the list of the repository's issues before the
split. COCONUT_TODO_REPOSITORY names another repository of the owner.

The Mac reads and writes with the connected GitHub account (business.github_request), so a new to-do or a move in
Coconut is an issue or a label on GitHub, and a change on GitHub shows at the next read.

    GET   /v1/todos[?fresh=1]         the open to-dos, newest activity first
    POST  /v1/todos {title, area}     a new to-do: an issue with the label of its list
    PATCH /v1/todos {number, area}    moves a to-do to another list: the label of the new list replaces the old one

The number travels in the body, so the request log keeps the route without it.
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
# The labels that a new to-do or a move adds when the repository lacks them.
LABELS = {'finance': ('1A7F37', 'Coconut to-do list Finance: money, payouts, risk, tax, accounting, marketing'),
          'support': ('0969DA', 'Coconut to-do list Support: customers, sellers, privacy requests, content gaps'),
          'product': ('8250DF', 'Coconut to-do list Product: bugs, features and the app (the default)')}
# Each device reads the list on its own, every few minutes and at each start, and a write drops it.
LIST_SECONDS = 15
MAX_TODOS = 300
PAGE = 50
NUMBER = re.compile(r'[1-9][0-9]{0,9}')
MARKER = 'coconut-todo'


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
    def __init__(self, business):
        self.business = business
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
        identity = hashlib.sha256(self.business._token().encode()).hexdigest()
        with self.lock:
            cached = self.cache.get(identity)
            if not fresh and cached and cached[0] > time.monotonic():
                return cached[1]
        login, items, page = self.login(), [], 1
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
        result = dict(repository=self.repository, login=login, items=items[:MAX_TODOS])
        with self.lock:
            self.cache = {identity: (time.monotonic() + LIST_SECONDS, result)}
        return result

    def forget(self) -> None:
        with self.lock:
            self.cache = {}

    def create(self, data: dict, request_key: str) -> dict:
        if set(data) - {'title', 'area'}:
            raise ServiceError(400, 'Send the title and the list of the to-do.')
        title = data.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 256 or '\n' in title or '\r' in title:
            raise ServiceError(400, 'Write the to-do in one line of up to 256 characters.')
        title, area = title.strip(), _area(data)
        signature = hashlib.sha256(encoded([title, area]).encode()).hexdigest()
        prefix = f'<!-- {MARKER}:{request_key}:'
        marker = prefix + signature + ' -->'
        # A receipt lost after GitHub accepted the issue is recovered from GitHub, without the delay of its search index.
        recent = self.business._call('GET', self._path() + '/issues?state=all&sort=created&direction=desc&per_page=25')
        for issue in recent if isinstance(recent, list) else []:
            if isinstance(issue, dict) and 'pull_request' not in issue and prefix in (issue.get('body') or ''):
                if marker not in issue['body']:
                    raise ServiceError(409, 'This request key already created a different to-do.')
                self.forget()
                return summary(issue, self.login())
        self.business.ensure_label(area, *LABELS[area])
        issue = self.business._call('POST', self._path() + '/issues', dict(title=title, body=marker, labels=[area]))
        self.forget()
        return summary(issue, self.login())

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
    """A new to-do or a move from a device. A repeat with the same key gets the saved answer, which is saved only after
    GitHub confirmed the write (business.mutate_business)."""
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
        if method == 'POST':
            result = todos.create(data, request_key)
        elif method == 'PATCH':
            result = todos.move(data)
        else:
            raise ServiceError(404, 'This to-do action was not found.')
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, fingerprint, encoded(result)))
        return result
