"""The preview of a GitHub address for an inline card in a chat.

The Mac reads GitHub with the connected account of the person and returns a small normalized object. An address that
the account cannot read gets an error, and the app keeps the plain link.
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from .service_base import ServiceError

NAME = r'[A-Za-z0-9._-]{1,100}'
OWNER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})')
REPOSITORY = re.compile(NAME)
SHA = re.compile(r'[0-9a-fA-F]{7,40}')
LINES = re.compile(r'L(\d{1,7})(?:C\d+)?(?:-L?(\d{1,7})(?:C\d+)?)?')
COMMENT = re.compile(r'(issuecomment|discussion_r|pullrequestreview|commitcomment)-?(\d{1,20})')
# Pages of github.com that are not an account.
RESERVED = {'about', 'apps', 'collections', 'contact', 'customer-stories', 'enterprise', 'events', 'explore',
            'features', 'issues', 'login', 'marketplace', 'new', 'notifications', 'orgs', 'organizations', 'pricing',
            'pulls', 'search', 'security', 'sessions', 'settings', 'signup', 'site', 'sponsors', 'topics', 'trending',
            'users'}
MAX_URL = 2000
MAX_LINES = 20
MAX_LINE = 240
MAX_TITLE = 300
MAX_EXCERPT = 400
MAX_LABELS = 6
MAX_ASSIGNEES = 5
MAX_ENTRIES = 256
# A preview reads GitHub with the ETag of its last answer (GitHubBusiness._read), so a short time costs no rate limit.
FRESH = 20
MISSING = 300
REFUSED = 60


def parse(address):
    """The parts of a GitHub address that the app shows as a card, or None for any other address."""
    if not isinstance(address, str) or not address or len(address) > MAX_URL:
        return None
    try:
        url = urlsplit(address.strip())
        host = (url.hostname or '').lower()
        port = url.port
    except ValueError:
        return None
    if url.scheme != 'https' or host not in ('github.com', 'www.github.com') or url.username or port not in (None, 443):
        return None
    parts = [unquote(part) for part in url.path.split('/') if part]
    if len(parts) < 2 or parts[0].lower() in RESERVED or not OWNER.fullmatch(parts[0]):
        return None
    name = parts[1][:-4] if parts[1].endswith('.git') else parts[1]
    if not REPOSITORY.fullmatch(name) or name in ('.', '..'):
        return None
    link = dict(owner=parts[0], repository=name)
    rest = parts[2:]
    fragment = unquote(url.fragment)
    comment = COMMENT.fullmatch(fragment)
    if not rest:
        return dict(link, kind='repository')
    if rest[0] in ('pull', 'issues') and len(rest) >= 2 and re.fullmatch(r'[1-9]\d{0,9}', rest[1]):
        link.update(kind='pull' if rest[0] == 'pull' else 'issue', number=int(rest[1]))
        if comment and comment[1] != 'commitcomment':
            link.update(comment=comment[1], comment_id=int(comment[2]))
        return link
    if rest[0] == 'commit' and len(rest) == 2 and SHA.fullmatch(rest[1]):
        link.update(kind='commit', sha=rest[1].lower())
        if comment and comment[1] == 'commitcomment':
            link.update(comment=comment[1], comment_id=int(comment[2]))
        return link
    if rest[0] == 'blob' and len(rest) >= 3:
        segments = rest[1:]
        if any(part in ('.', '..') or '\n' in part or '\\' in part for part in segments):
            return None
        link.update(kind='file', segments=segments)
        lines = LINES.fullmatch(fragment)
        if lines:
            start, end = int(lines[1]), int(lines[2] or lines[1])
            if start >= 1:
                link.update(start=min(start, end), end=max(start, end))
        return link
    return None


def _text(value, limit):
    value = value if isinstance(value, str) else ''
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def _person(user):
    if not isinstance(user, dict) or not isinstance(user.get('login'), str):
        return None
    avatar = user.get('avatar_url')
    # Only the avatar host of GitHub: the app loads this address.
    if not (isinstance(avatar, str) and urlsplit(avatar).scheme == 'https'
            and (urlsplit(avatar).hostname or '').endswith('.githubusercontent.com')):
        avatar = None
    return dict(login=_text(user['login'], 60), avatar_url=avatar)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _excerpt(body):
    """The start of a comment as plain text: no HTML comments, no quoted lines, no code fences, one line."""
    body = re.sub(r'<!--.*?-->', ' ', body if isinstance(body, str) else '', flags=re.S)
    lines = [line.strip() for line in body.splitlines()]
    lines = [line for line in lines if line and not line.startswith('>') and not line.startswith('```')]
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', ' '.join(lines))
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]{1,200}>', ' ', text)
    return _text(re.sub(r'\s+', ' ', text).strip(), MAX_EXCERPT)


class GitHubPreviews:
    """Previews of GitHub addresses, read through the GitHub connection of the business views and kept for a short time."""

    def __init__(self, business, clock=time.monotonic):
        # A function, so a new GitHub connection of the service is the one that reads.
        self.business = business
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = {}

    def preview(self, address):
        link = parse(address)
        if not link:
            raise ServiceError(400, 'This address is not a GitHub page that Coconut can preview.')
        # Another account sees other repositories, so the account is part of the key.
        identity = hashlib.sha256(self.business()._token().encode()).hexdigest()
        cache_key = (identity, address.strip())
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and cached[0] > self.clock():
                if isinstance(cached[1], ServiceError):
                    raise ServiceError(cached[1].status, str(cached[1]))
                return cached[1]
        try:
            result = self._load(link)
            result['url'] = address.strip()
            self._keep(cache_key, FRESH, result)
            return result
        except ServiceError as error:
            # A missing page stays missing for a while. A refusal is often the rate limit, so the Mac waits before
            # it asks again. An account that needs a new login is not kept: the next request sees the new login.
            if error.status in (403, 404):
                self._keep(cache_key, MISSING if error.status == 404 else REFUSED, error)
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ServiceError(502, 'GitHub returned a page that Coconut cannot preview.') from None

    def forget(self):
        """GitHub changed (business_watch.py): the next preview of each address reads it again."""
        with self.lock:
            self.cache = {}

    def _keep(self, cache_key, seconds, value):
        with self.lock:
            now = self.clock()
            if len(self.cache) >= MAX_ENTRIES:
                self.cache = {key: entry for key, entry in self.cache.items() if entry[0] > now}
            while len(self.cache) >= MAX_ENTRIES:
                del self.cache[min(self.cache, key=lambda key: self.cache[key][0])]
            self.cache[cache_key] = (now + seconds, value)

    def _get(self, path):
        return self.business()._call('GET', path)

    def _optional(self, path):
        """A part of a card that can be absent, such as the checks: None when GitHub does not give it."""
        try:
            return self._get(path)
        except ServiceError as error:
            if error.status == 409:
                raise
            return None

    def _load(self, link):
        base = f"repos/{quote(link['owner'], safe='')}/{quote(link['repository'], safe='')}"
        repository = f"{link['owner']}/{link['repository']}"
        kind = link['kind']
        if kind == 'repository':
            return self._repository(base)
        if kind == 'file':
            return self._file(base, repository, link)
        if link.get('comment'):
            return self._comment(base, repository, link)
        if kind == 'commit':
            return self._commit(base, repository, link['sha'])
        if kind == 'pull':
            return self._pull(base, repository, link['number'])
        issue = self._get(f"{base}/issues/{link['number']}")
        # GitHub sends an issue address of a pull request to the pull request.
        if issue.get('pull_request') is not None:
            return self._pull(base, repository, link['number'])
        return self._issue(repository, issue)

    def _repository(self, base):
        data = self._get(base)
        return dict(kind='repository', repository=_text(data['full_name'], 200),
                    description=_text(data.get('description'), MAX_TITLE),
                    stars=_count(data.get('stargazers_count')), forks=_count(data.get('forks_count')),
                    language=_text(data.get('language'), 40) or None, private=bool(data.get('private')),
                    archived=bool(data.get('archived')), updated_at=data.get('pushed_at') or data.get('updated_at'))

    def _issue(self, repository, issue):
        state = 'open' if issue.get('state') == 'open' else ('not_planned' if issue.get('state_reason') in ('not_planned', 'duplicate') else 'closed')
        labels = [dict(name=_text(label['name'], 50), color=label['color'].lower() if re.fullmatch(r'[0-9a-fA-F]{6}', label.get('color') or '') else None)
                  for label in issue.get('labels') or [] if isinstance(label, dict) and isinstance(label.get('name'), str)]
        assignees = [person for person in map(_person, issue.get('assignees') or []) if person]
        return dict(kind='issue', repository=repository, number=issue['number'], title=_text(issue['title'], MAX_TITLE),
                    state=state, author=_person(issue.get('user')), labels=labels[:MAX_LABELS],
                    more_labels=max(len(labels) - MAX_LABELS, 0), assignees=assignees[:MAX_ASSIGNEES],
                    comments=_count(issue.get('comments')), updated_at=issue.get('updated_at'))

    def _pull(self, base, repository, number):
        pull = self._get(f'{base}/pulls/{number}')
        state = 'merged' if pull.get('merged') else 'closed' if pull.get('state') != 'open' else 'draft' if pull.get('draft') else 'open'
        result = dict(kind='pull', repository=repository, number=pull['number'], title=_text(pull['title'], MAX_TITLE),
                      state=state, author=_person(pull.get('user')), additions=_count(pull.get('additions')),
                      deletions=_count(pull.get('deletions')), changed_files=_count(pull.get('changed_files')),
                      comments=_count((pull.get('comments') or 0) + (pull.get('review_comments') or 0)),
                      checks=None, review=None, updated_at=pull.get('updated_at'))
        sha = (pull.get('head') or {}).get('sha')
        # A merged or closed pull request shows its state, not its checks.
        if state in ('open', 'draft'):
            if isinstance(sha, str) and SHA.fullmatch(sha):
                result['checks'] = self._checks(base, sha)
            result['review'] = self._review(base, number, pull)
        return result

    def _checks(self, base, sha):
        passed = failed = pending = 0
        runs = self._optional(f'{base}/commits/{sha}/check-runs?per_page=100')
        for run in (runs or {}).get('check_runs') or []:
            if run.get('status') != 'completed':
                pending += 1
            elif run.get('conclusion') in ('success', 'neutral', 'skipped'):
                passed += 1
            else:
                failed += 1
        status = self._optional(f'{base}/commits/{sha}/status?per_page=100')
        for entry in (status or {}).get('statuses') or []:
            if entry.get('state') == 'success':
                passed += 1
            elif entry.get('state') == 'pending':
                pending += 1
            else:
                failed += 1
        if not passed + failed + pending:
            return None
        return dict(state='failing' if failed else 'pending' if pending else 'passing', passed=passed, failed=failed, pending=pending)

    def _review(self, base, number, pull):
        reviews = self._optional(f'{base}/pulls/{number}/reviews?per_page=100')
        latest = {}
        for review in reviews if isinstance(reviews, list) else []:
            login = (review.get('user') or {}).get('login')
            if login and review.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                latest[login] = review['state']
        states = set(latest.values())
        if 'CHANGES_REQUESTED' in states:
            return 'changes_requested'
        if 'APPROVED' in states:
            return 'approved'
        if pull.get('requested_reviewers') or pull.get('requested_teams'):
            return 'review_requested'
        return None

    def _commit(self, base, repository, sha):
        data = self._get(f'{base}/commits/{sha}')
        commit = data.get('commit') or {}
        author = _person(data.get('author'))
        name = (commit.get('author') or {}).get('name')
        if not author and isinstance(name, str) and name:
            author = dict(login=_text(name, 60), avatar_url=None)
        stats = data.get('stats') or {}
        files = data.get('files')
        return dict(kind='commit', repository=repository, sha=data['sha'][:40], title=_text((commit.get('message') or '').split('\n', 1)[0], MAX_TITLE),
                    author=author, additions=_count(stats.get('additions')), deletions=_count(stats.get('deletions')),
                    changed_files=len(files) if isinstance(files, list) else None,
                    updated_at=(commit.get('committer') or {}).get('date') or (commit.get('author') or {}).get('date'))

    def _file(self, base, repository, link):
        segments = link['segments']
        data = None
        # A branch name can hold a slash, so the address does not say where the name ends. Try the short names first.
        for size in range(1, min(len(segments), 4)):
            ref, path = '/'.join(segments[:size]), '/'.join(segments[size:])
            try:
                data = self._get(f"{base}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
                break
            except ServiceError as error:
                if error.status != 404:
                    raise
        if not isinstance(data, dict) or data.get('type') != 'file':
            raise ServiceError(404, 'This file is unavailable to the connected GitHub account.')
        result = dict(kind='file', repository=repository, path=_text(path, 400), ref=_text(ref, 100),
                      start_line=None, end_line=None, lines=[], more_lines=0)
        start = link.get('start')
        if start is None or data.get('encoding') != 'base64':
            return result
        raw = base64.b64decode(data.get('content') or '')
        if b'\0' in raw[:8000]:
            return result
        text = raw.decode('utf-8', 'replace').splitlines()
        end = min(link['end'], len(text))
        if start > len(text):
            return result
        shown = text[start - 1:min(end, start - 1 + MAX_LINES)]
        result.update(start_line=start, end_line=end, more_lines=end - start + 1 - len(shown),
                      lines=[_text(line.expandtabs(4).rstrip(), MAX_LINE) for line in shown])
        return result

    def _comment(self, base, repository, link):
        kind, identifier = link['comment'], link['comment_id']
        if kind == 'issuecomment':
            comment = self._get(f'{base}/issues/comments/{identifier}')
        elif kind == 'discussion_r':
            comment = self._get(f'{base}/pulls/comments/{identifier}')
        elif kind == 'pullrequestreview':
            comment = self._get(f"{base}/pulls/{link['number']}/reviews/{identifier}")
        else:
            comment = self._get(f'{base}/comments/{identifier}')
        result = dict(kind='comment', repository=repository, number=link.get('number'), sha=link.get('sha'),
                      on='commit' if link['kind'] == 'commit' else link['kind'], title='',
                      author=_person(comment.get('user')), excerpt=_excerpt(comment.get('body')),
                      path=_text(comment.get('path'), 400) or None,
                      updated_at=comment.get('updated_at') or comment.get('submitted_at') or comment.get('created_at'))
        # The title of the page of the comment. Without it the card still shows the comment.
        if link['kind'] == 'commit':
            parent = self._optional(f"{base}/commits/{link['sha']}")
            result['title'] = _text((((parent or {}).get('commit') or {}).get('message') or '').split('\n', 1)[0], MAX_TITLE)
        else:
            parent = self._optional(f"{base}/issues/{link['number']}")
            result['title'] = _text((parent or {}).get('title'), MAX_TITLE)
            if (parent or {}).get('pull_request') is not None:
                result['on'] = 'pull'
        return result
