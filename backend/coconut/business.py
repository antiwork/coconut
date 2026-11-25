"""Native business screens backed entirely by the organization's GitHub repositories.

Accounting, Marketing and Support are reserved repository keys. Project repositories opt in with the coconut-project topic.
The work of every function is an issue of one repository, antiwork/gumroad-private (work_repository), as every to-do
is (github_todos): the person keeps all work there and nothing else. The label of its list says whose work it is
(WORK_AREAS), and a project label scopes it to a project.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import OrderedDict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .service_base import ServiceError, encoded, require_text

FUNCTIONS = {'accounting': ('Accounting', 'Ledger, close, taxes and financial operations.', 'books.vertical'),
             'marketing': ('Marketing', 'Campaigns, content, launches and growth.', 'megaphone'),
             'support': ('Support', 'Customer queues, service levels and resolutions.', 'lifepreserver')}
PROJECT_TOPIC = 'coconut-project'
PROJECT_LABEL = 'coconut-project:'
KEY = re.compile(r'[a-z0-9._-]{1,100}')
# The to-do list of each function, the label of its work in the work repository. Accounting and Marketing open from
# Finance, so their work is the work of Finance.
WORK_AREAS = {'accounting': 'finance', 'marketing': 'finance', 'support': 'support'}


def project_label(project_key):
    suffix = project_key if len(PROJECT_LABEL + project_key) <= 50 else '~' + hashlib.sha256(project_key.encode()).hexdigest()[:32]
    return PROJECT_LABEL + suffix


def work_repository():
    """The repository of every to-do and every piece of work of the business. COCONUT_TODO_REPOSITORY names another."""
    return key(os.environ.get('COCONUT_TODO_REPOSITORY', 'gumroad-private'))


def key(value):
    if not isinstance(value, str) or value in ('.', '..') or not KEY.fullmatch(value.lower()):
        raise ServiceError(400, 'Choose a valid GitHub repository key.')
    return value.lower()


def refusal(error):
    """GitHub's own sentence for a refused change, such as "Pull Request is not mergeable", or None."""
    try:
        message = json.loads(error.read(65_536) or b'{}').get('message')
    except (OSError, ValueError, AttributeError):
        return None
    return message.strip()[:300] if isinstance(message, str) and message.strip() else None


# A read that GitHub answers with 304 Not Modified costs nothing of its rate limit. The Mac keeps the newest answer of
# each read with its ETag and asks again with If-None-Match (GitHubBusiness._read), so the live pages of the apps and
# the watch of the business (business_watch.py) can read every few seconds, and a read that finds nothing new is free.
KEPT_READS = 400
KEPT_BYTES = 24 * 1024 * 1024


def _send(method, path, token, body=None, etag=None):
    """One request to GitHub: its status, its ETag and its body. A 304 Not Modified has no body."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token,
               'Content-Type': 'application/json', 'User-Agent': 'Coconut', 'X-GitHub-Api-Version': '2022-11-28'}
    if etag:
        headers['If-None-Match'] = etag
    request = Request('https://api.github.com/' + path, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            content = response.read(2_097_153)
            if len(content) > 2_097_152:
                raise ServiceError(502, 'GitHub returned too much data. Narrow the view and try again.')
            return response.status, response.headers.get('ETag'), content
    except HTTPError as error:
        if error.code == 304 and etag:
            return 304, error.headers.get('ETag') or etag, b''
        if error.code == 401:
            raise ServiceError(409, 'Reconnect GitHub in You → Connections → GitHub.') from None
        if error.code == 404:
            raise ServiceError(404, 'This repository or work item is unavailable to the connected GitHub account.') from None
        if error.code in (403, 429):
            raise ServiceError(403, 'GitHub refused this request. Check repository access or try again after its rate limit resets.') from None
        if error.code in (405, 409, 422):
            # GitHub says why it refused a change: a pull request that cannot merge yet, a head that moved, a closed
            # issue. The person reads that reason in the app.
            raise ServiceError(422, refusal(error) or 'GitHub refused this change. Refresh before retrying.') from None
        raise ServiceError(502, 'GitHub did not confirm the change. Refresh the work before retrying.') from None
    except (URLError, TimeoutError):
        raise ServiceError(502, 'GitHub is unavailable. Refresh before retrying a change.') from None


def _decoded(content):
    try:
        return json.loads(content) if content else {}
    except ValueError:
        raise ServiceError(502, 'GitHub is unavailable. Refresh before retrying a change.') from None


def github_request(method, path, token, body=None):
    return _decoded(_send(method, path, token, body)[2])


class GitHubBusiness:
    def __init__(self, connection, owner=None, request=github_request):
        self.connection = connection
        self.owner = key(owner or os.environ.get('COCONUT_BUSINESS_OWNER', 'antiwork'))
        self.request = request
        self.write_lock = threading.RLock()
        self.cache_lock = threading.Lock()
        self.cache = {}
        # The newest answer of each read with its ETag, oldest use first (_read).
        self.read_lock = threading.Lock()
        self.reads = OrderedDict()
        self.read_bytes = 0

    def _token(self):
        self.connection.refresh()
        token = self.connection.token()
        if not token:
            raise ServiceError(409, 'Connect GitHub in You → Connections → GitHub to open your business.')
        return token

    def _call(self, method, path, body=None):
        token = self._token()
        if method == 'GET' and self.request is github_request:
            return self._read(path, token)
        return self.request(method, path, token, body)

    def _read(self, path, token):
        """A read with the ETag of its kept answer. GitHub answers 304 Not Modified when nothing changed, which costs
        nothing of its rate limit, and the kept answer is decoded again, so each caller gets its own copy. Another
        account sees other data, so the account is part of the key."""
        identity = (hashlib.sha256(token.encode()).hexdigest(), path)
        with self.read_lock:
            kept = self.reads.get(identity)
            if kept:
                self.reads.move_to_end(identity)
        status, etag, content = _send('GET', path, token, etag=kept[0] if kept else None)
        if status == 304 and kept:
            return _decoded(kept[1])
        with self.read_lock:
            old = self.reads.pop(identity, None)
            if old:
                self.read_bytes -= len(old[1])
            if etag and len(content) <= KEPT_BYTES // 8:
                self.reads[identity] = (etag, content)
                self.read_bytes += len(content)
            while self.reads and (len(self.reads) > KEPT_READS or self.read_bytes > KEPT_BYTES):
                self.read_bytes -= len(self.reads.popitem(last=False)[1][1])
        return _decoded(content)

    def _pages(self, path, size=100):
        page = 1
        while True:
            values = self._call('GET', path + ('&' if '?' in path else '?') + f'per_page={size}&page={page}')
            if not isinstance(values, list):
                raise ServiceError(502, 'GitHub returned an invalid list.')
            yield from values
            if len(values) < size:
                return
            page += 1

    def _repo_path(self, repository):
        return f'repos/{self.owner}/{key(repository)}'

    def _repo(self, repository):
        return self._call('GET', self._repo_path(repository))

    def _repository(self, repository):
        return dict(key=key(repository['name']), name=repository['name'],
                    full_name=repository['full_name'], url=repository['html_url'],
                    description=repository.get('description') or '', private=bool(repository['private']),
                    archived=bool(repository.get('archived')), issues_enabled=bool(repository.get('has_issues')))

    def catalog(self, fresh=False):
        identity = hashlib.sha256(self._token().encode()).hexdigest()
        with self.cache_lock:
            cached = self.cache.get(identity)
            if not fresh and cached and cached[0] > time.monotonic():
                return cached[1]
        repositories = list(self._pages(f'orgs/{self.owner}/repos?type=all&sort=full_name'))
        by_key = {key(repo['name']): repo for repo in repositories}
        functions = [dict(key=name, name=details[0], description=details[1], symbol=details[2], reserved=True,
                          repository=self._repository(by_key[name]) if name in by_key else None)
                     for name, details in FUNCTIONS.items()]
        candidates = [self._repository(repo) for repo in repositories
                      if key(repo['name']) not in FUNCTIONS and not repo.get('archived')]
        projects = [self._repository(repo) for repo in repositories
                    if key(repo['name']) not in FUNCTIONS and PROJECT_TOPIC in repo.get('topics', [])
                    and not repo.get('archived')]
        result = dict(owner=self.owner, functions=functions, projects=projects, repositories=candidates)
        with self.cache_lock:
            self.cache = {identity: (time.monotonic() + 30, result)}
        return result

    def project(self, project_key):
        project_key = key(project_key)
        if project_key in FUNCTIONS:
            raise ServiceError(409, 'Accounting, Marketing and Support are reserved functions, not project keys.')
        repository = self._repo(project_key)
        if repository.get('archived') or PROJECT_TOPIC not in repository.get('topics', []):
            raise ServiceError(404, 'Add this repository as a Coconut project first.')
        return repository

    def add_project(self, data):
        if set(data) != {'key'}:
            raise ServiceError(400, 'Choose the repository key of the project.')
        project_key = key(data['key'])
        if project_key in FUNCTIONS:
            raise ServiceError(409, 'Accounting, Marketing and Support are reserved functions, not project keys.')
        repository = self._repo(project_key)
        if repository.get('archived'):
            raise ServiceError(409, 'Restore this GitHub repository before adding the project.')
        topics = list(dict.fromkeys([*repository.get('topics', []), PROJECT_TOPIC]))
        self._call('PUT', self._repo_path(project_key) + '/topics', {'names': topics})
        with self.cache_lock:
            self.cache.clear()
        return self._repository(repository)

    def _function(self, function_key, writable=False):
        function_key = key(function_key)
        if function_key not in FUNCTIONS:
            raise ServiceError(404, 'Choose Accounting, Marketing or Support.')
        if writable:
            repository = self._repo(work_repository())
            if repository.get('archived') or not repository.get('has_issues'):
                raise ServiceError(409, 'Enable issues and restore the work repository on GitHub before adding work.')
        return function_key

    def _work_path(self):
        return self._repo_path(work_repository())

    def ensure_label(self, name, color, description):
        """Adds a label to the work repository when it lacks it."""
        try:
            self._call('GET', self._work_path() + '/labels/' + quote(name, safe=''))
        except ServiceError as error:
            if error.status != 404:
                raise
            self._call('POST', self._work_path() + '/labels', dict(name=name, color=color, description=description))

    def _work(self, function_key, issue):
        labels = [label['name'] if isinstance(label, dict) else label for label in issue.get('labels', [])]
        project = next((name[len(PROJECT_LABEL):] for name in labels if name.startswith(PROJECT_LABEL)), None)
        if project and project.startswith('~'):
            project = next((repo['key'] for repo in self.catalog()['projects'] if project_label(repo['key']) in labels), None)
        body = re.sub(r'\n?<!-- coconut-work:[^>]+ -->\s*', '', issue.get('body') or '').strip()
        return dict(id=f'{function_key}:{issue["number"]}', number=issue['number'], function_key=function_key,
                    project_key=project, title=issue['title'], body=body, state=issue['state'],
                    url=issue['html_url'], updated_at=issue['updated_at'],
                    author=(issue.get('user') or {}).get('login'), labels=labels)

    def work(self, function_key=None, project_key=None, state='open'):
        if state not in ('open', 'closed', 'all'):
            raise ServiceError(400, 'Choose open, closed or all work.')
        function_key = self._function(function_key) if function_key else None
        if project_key:
            project_key = key(project_key)
            self.project(project_key)
        labels = ([WORK_AREAS[function_key]] if function_key else []) + ([project_label(project_key)] if project_key else [])
        query = dict(state=state, sort='updated', direction='desc')
        if labels:
            query['labels'] = ','.join(labels)
        items = []
        for issue in self._pages(self._work_path() + '/issues?' + urlencode(query), size=25):
            if 'pull_request' not in issue:
                names = {label['name'] if isinstance(label, dict) else label for label in issue.get('labels', [])}
                items.append(self._work(function_key or ('support' if 'support' in names else 'accounting'), issue))
        items.sort(key=lambda item: (item['updated_at'], item['id']), reverse=True)
        return dict(items=items)

    def create_work(self, function_key, data, request_key):
        function_key = self._function(function_key, writable=True)
        if set(data) - {'title', 'body', 'project_key'}:
            raise ServiceError(400, 'Specify a title, description and optional project.')
        title = require_text(data, 'title', 256)
        if '\n' in title or '\r' in title:
            raise ServiceError(400, 'Use a single-line title.')
        body = data.get('body') or ''
        if not isinstance(body, str) or len(body) > 60_000 or '<!-- coconut-work:' in body:
            raise ServiceError(400, 'Use a description of up to 60,000 characters.')
        project_key = data.get('project_key')
        from .github_todos import LABELS
        area = WORK_AREAS[function_key]
        self.ensure_label(area, *LABELS[area])
        labels = [area]
        if project_key:
            project_key = key(project_key)
            self.project(project_key)
            label = project_label(project_key)
            self.ensure_label(label, '6f55a5', f'Coconut project: {self.owner}/{project_key}')
            labels.append(label)
        signature = hashlib.sha256(encoded([function_key, title, body, project_key]).encode()).hexdigest()
        prefix = f'<!-- coconut-work:{request_key}:'
        marker = prefix + signature + ' -->'
        # A receipt lost after GitHub accepted the write is recovered from GitHub, without search-index delay.
        for issue in self._pages(self._work_path() + '/issues?state=all&sort=created&direction=desc', size=25):
            if 'pull_request' not in issue and prefix in (issue.get('body') or ''):
                if marker not in issue['body']:
                    raise ServiceError(409, 'This request key already created different work.')
                return self._work(function_key, issue)
        issue = self._call('POST', self._work_path() + '/issues',
                           dict(title=title, body=body.rstrip() + '\n\n' + marker, labels=labels))
        return self._work(function_key, issue)

    def update_work(self, function_key, number, data):
        function_key = self._function(function_key, writable=True)
        if not re.fullmatch(r'[1-9][0-9]{0,9}', number) or int(number) >= 2**31 or set(data) != {'state'} or data['state'] not in ('open', 'closed'):
            raise ServiceError(400, 'Choose open or closed for this work item.')
        path = self._work_path() + '/issues/' + number
        if 'pull_request' in self._call('GET', path):
            raise ServiceError(400, 'This is a pull request, not business work.')
        return self._work(function_key, self._call('PATCH', path, data))


def mutate_business(service, method, parts, data, request_key, fingerprint):
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this business action.')
    client = service.business
    fingerprint = hashlib.sha256((fingerprint + str(service.github.account().get('login'))).encode()).hexdigest()
    with client.write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], fingerprint):
                raise ServiceError(409, 'This request key belongs to a different change.')
            client._token()
            return json.loads(receipt[1])
        if method == 'POST' and parts == ['projects']:
            result = client.add_project(data)
        elif method == 'POST' and len(parts) == 3 and parts[0] == 'functions' and parts[2] == 'work':
            result = client.create_work(parts[1], data, request_key)
        elif method == 'PATCH' and len(parts) == 4 and parts[0] == 'functions' and parts[2] == 'work':
            result = client.update_work(parts[1], parts[3], data)
        else:
            raise ServiceError(404, 'This business action was not found.')
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)', (request_key, fingerprint, encoded(result)))
        return result
