"""GitHub issues and pull requests, read and answered in Coconut.

The apps open an issue or a pull request of any repository that the GitHub login of the Mac can read, in place of a
link to github.com: its text, labels, people and comments, and for a pull request its checks, its review, its merge
state and its changed files. The person comments, closes, reopens and merges in the app. Each write goes to GitHub
with that login (github_access.py), so GitHub and every agent see it as the person's own.

    GET   /v1/github/item?repository=<owner>/<name>&number=<n>
    GET   /v1/github/item/files?repository=<owner>/<name>&number=<n>
    POST  /v1/github/item/comments   {repository, number, body}
    PATCH /v1/github/item            {repository, number, state: open|closed, reason?: completed|not_planned}
    POST  /v1/github/item/merge      {repository, number, sha}

The repository and the number travel in the query or the body, so the request log keeps the route without them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re

from .github_preview import GitHubPreviews, OWNER, REPOSITORY, SHA, _count, _person, _text
from .service_base import ServiceError, encoded

NUMBER = re.compile(r'[1-9][0-9]{0,9}')
# Coconut's own markers in a body (the receipt of a comment or of new work) are not text for a person.
MARKER = re.compile(r'\n*<!--\s*coconut-[^>]*-->[ \t]*', re.I)
MAX_BODY = 60_000
MAX_COMMENTS = 100
COMMENT_PAGE = 50
MAX_FILES = 300
FILES_PAGE = 30
MAX_PATCH = 20_000
PATCH_BUDGET = 1_000_000
MERGE_METHODS = (('squash', 'allow_squash_merge'), ('merge', 'allow_merge_commit'), ('rebase', 'allow_rebase_merge'))


def target(repository, number):
    """The owner, the name and the number of an item, checked."""
    if not isinstance(repository, str) or repository.count('/') != 1:
        raise ServiceError(400, 'Name the repository as owner/name.')
    owner, name = repository.split('/')
    if not OWNER.fullmatch(owner) or not REPOSITORY.fullmatch(name) or name in ('.', '..'):
        raise ServiceError(400, 'Name the repository as owner/name.')
    number = str(number)
    if not NUMBER.fullmatch(number) or int(number) >= 2**31:
        raise ServiceError(400, 'Name the number of the issue or the pull request.')
    return owner, name, int(number)


def clean(body):
    """The text of a body for a person: without Coconut's markers and the space they leave."""
    return MARKER.sub('', body if isinstance(body, str) else '').strip()


def _labels(issue):
    return [dict(name=_text(label['name'], 50),
                 color=label['color'].lower() if re.fullmatch(r'[0-9a-fA-F]{6}', label.get('color') or '') else None)
            for label in issue.get('labels') or [] if isinstance(label, dict) and isinstance(label.get('name'), str)]


def _comment_entry(comment):
    return dict(id=str(comment.get('id') or ''), kind='comment', author=_person(comment.get('user')),
                body=clean(comment.get('body')), created_at=comment.get('created_at'),
                updated_at=comment.get('updated_at'), state=None)


def _review_entry(review):
    state = {'APPROVED': 'approved', 'CHANGES_REQUESTED': 'changes_requested', 'COMMENTED': 'commented',
             'DISMISSED': 'dismissed'}.get(review.get('state'))
    return dict(id='review-' + str(review.get('id') or ''), kind='review', author=_person(review.get('user')),
                body=clean(review.get('body')), created_at=review.get('submitted_at'),
                updated_at=review.get('submitted_at'), state=state)


class GitHubItems(GitHubPreviews):
    """The whole of an issue or a pull request, with the writes of a person. It reads through the GitHub login of
    the business views, as the previews do, and keeps nothing: each read goes to GitHub."""

    def _base(self, owner, name):
        return f'repos/{owner}/{name}'

    def _comments(self, base, number, total):
        """The newest comments, oldest first, and the count of older ones that the page leaves out."""
        if not total:
            return [], 0
        pages = max(1, math.ceil(total / COMMENT_PAGE))
        shown = []
        for page in range(pages, 0, -1):
            values = self._get(f'{base}/issues/{number}/comments?per_page={COMMENT_PAGE}&page={page}')
            if not isinstance(values, list):
                raise ServiceError(502, 'GitHub returned an invalid list.')
            shown = values + shown
            if len(shown) >= MAX_COMMENTS:
                break
        shown = shown[-MAX_COMMENTS:]
        return [_comment_entry(item) for item in shown if isinstance(item, dict)], max(0, total - len(shown))

    def _merge_method(self, base):
        """The first way of merging that the repository allows, squash first. GitHub gives these settings only to an
        account that can push, so None means the account cannot merge here, or GitHub did not say."""
        repository = self._optional(base) or {}
        return next((method for method, setting in MERGE_METHODS if repository.get(setting)), None)

    def item(self, repository, number):
        owner, name, number = target(repository, number)
        base = self._base(owner, name)
        issue = self._get(f'{base}/issues/{number}')
        if not isinstance(issue, dict) or 'number' not in issue:
            raise ServiceError(502, 'GitHub returned an issue that Coconut cannot show.')
        entries, more = self._comments(base, number, _count(issue.get('comments')) or 0)
        state = 'open' if issue.get('state') == 'open' else 'closed'
        result: dict = dict(kind='issue', repository=f'{owner}/{name}', number=number, title=_text(issue.get('title'), 1000),
                      body=clean(issue.get('body')), state=state, state_reason=issue.get('state_reason'),
                      author=_person(issue.get('user')), labels=_labels(issue),
                      assignees=[person for person in map(_person, issue.get('assignees') or []) if person],
                      created_at=issue.get('created_at'), updated_at=issue.get('updated_at'),
                      closed_at=issue.get('closed_at'), url=issue.get('html_url'),
                      viewer=self.business().connection.account().get('login'), comments=entries,
                      more_comments=more, pull=None)
        if issue.get('pull_request') is not None:
            result.update(kind='pull', pull=self._pull_details(base, number, entries, result))
        return result

    def _pull_details(self, base, number, entries, result):
        pull = self._get(f'{base}/pulls/{number}')
        merged = bool(pull.get('merged'))
        if merged:
            result['state'] = 'merged'
        reviews = self._optional(f'{base}/pulls/{number}/reviews?per_page=100')
        # A review shows in the conversation when it says something or decides: an approval, a change request.
        shown = [_review_entry(review) for review in reviews if isinstance(review, dict)
                 and (clean(review.get('body')) or review.get('state') in ('APPROVED', 'CHANGES_REQUESTED'))] \
            if isinstance(reviews, list) else []
        entries.extend(shown)
        entries.sort(key=lambda entry: (entry['created_at'] or '', entry['id']))
        head = pull.get('head') or {}
        sha = head.get('sha')
        sha = sha if isinstance(sha, str) and SHA.fullmatch(sha) else None
        open_pull = pull.get('state') == 'open'
        return dict(draft=bool(pull.get('draft')), merged=merged, merged_at=pull.get('merged_at'),
                    merged_by=(_person(pull.get('merged_by')) or {}).get('login'),
                    mergeable=pull.get('mergeable') if isinstance(pull.get('mergeable'), bool) else None,
                    mergeable_state=pull.get('mergeable_state') if isinstance(pull.get('mergeable_state'), str) else None,
                    head_sha=sha, head_ref=_text(head.get('ref'), 200),
                    base_ref=_text((pull.get('base') or {}).get('ref'), 200),
                    additions=_count(pull.get('additions')), deletions=_count(pull.get('deletions')),
                    changed_files=_count(pull.get('changed_files')), commits=_count(pull.get('commits')),
                    checks=self._checks(base, sha) if open_pull and sha else None,
                    review=self._review(base, number, pull) if open_pull else None,
                    merge_method=self._merge_method(base) if open_pull else None)

    def files(self, repository, number):
        """The changed files of a pull request with their patches. A patch past the size that a phone shows well is
        left out, and the file says so."""
        owner, name, number = target(repository, number)
        base = self._base(owner, name)
        files, budget = [], PATCH_BUDGET
        for page in range(1, MAX_FILES // FILES_PAGE + 1):
            values = self._get(f'{base}/pulls/{number}/files?per_page={FILES_PAGE}&page={page}')
            if not isinstance(values, list):
                raise ServiceError(502, 'GitHub returned an invalid list.')
            for value in values:
                if not isinstance(value, dict) or not isinstance(value.get('filename'), str):
                    continue
                patch = value.get('patch') if isinstance(value.get('patch'), str) else None
                large = patch is not None and (len(patch) > MAX_PATCH or len(patch) > budget)
                if patch is not None and not large:
                    budget -= len(patch)
                files.append(dict(path=_text(value['filename'], 400), previous_path=_text(value.get('previous_filename'), 400) or None,
                                  status=value.get('status') if isinstance(value.get('status'), str) else 'modified',
                                  additions=_count(value.get('additions')) or 0, deletions=_count(value.get('deletions')) or 0,
                                  patch=None if large else patch, too_large=large))
            if len(values) < FILES_PAGE:
                break
        return dict(files=files)

    def comment(self, repository, number, data, request_key):
        owner, name, number = target(repository, number)
        body = data.get('body')
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
            raise ServiceError(400, 'Write a comment of up to 60,000 characters.')
        if '<!-- coconut-' in body.lower():
            raise ServiceError(400, 'Remove the Coconut marker from the comment.')
        base = self._base(owner, name)
        marker = f'<!-- coconut-comment:{request_key} -->'
        # A comment that GitHub took before its answer was lost is found again, so a second Send posts nothing.
        issue = self._get(f'{base}/issues/{number}')
        total = _count(issue.get('comments')) or 0
        if total:
            page = max(1, math.ceil(total / COMMENT_PAGE))
            for item in self._get(f'{base}/issues/{number}/comments?per_page={COMMENT_PAGE}&page={page}') or []:
                if isinstance(item, dict) and marker in (item.get('body') or ''):
                    return _comment_entry(item)
        created = self.business()._call('POST', f'{base}/issues/{number}/comments',
                                        dict(body=body.rstrip() + '\n\n' + marker))
        return _comment_entry(created)

    def set_state(self, repository, number, data):
        owner, name, number = target(repository, number)
        state, reason = data.get('state'), data.get('reason')
        if state not in ('open', 'closed') or reason not in (None, 'completed', 'not_planned'):
            raise ServiceError(400, 'Choose open or closed, and completed or not planned.')
        base = self._base(owner, name)
        issue = self._get(f'{base}/issues/{number}')
        if issue.get('pull_request') is not None:
            self.business()._call('PATCH', f'{base}/pulls/{number}', dict(state=state))
        else:
            change = dict(state=state)
            if state == 'closed':
                change['state_reason'] = reason or 'completed'
            self.business()._call('PATCH', f'{base}/issues/{number}', change)
        return self.item(f'{owner}/{name}', number)

    def merge(self, repository, number, data):
        owner, name, number = target(repository, number)
        sha = data.get('sha')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ServiceError(400, 'Name the head commit that you reviewed.')
        base = self._base(owner, name)
        pull = self._get(f'{base}/pulls/{number}')
        if pull.get('merged'):
            return self.item(f'{owner}/{name}', number)
        if (pull.get('head') or {}).get('sha') != sha:
            raise ServiceError(409, 'The pull request changed since you opened it. Read the new commits, then merge.')
        method = self._merge_method(base)
        if not method:
            raise ServiceError(403, 'The GitHub login of this Mac cannot merge in this repository.')
        # GitHub merges only the head that the person saw: a newer push makes it refuse.
        self.business()._call('PUT', f'{base}/pulls/{number}/merge', dict(sha=sha, merge_method=method))
        return self.item(f'{owner}/{name}', number)


def mutate_github_item(service, method, parts, data, request_key, fingerprint):
    """A write to an issue or a pull request. A repeated request key answers with the saved result, which is saved only
    after GitHub confirmed the write (business.mutate_business)."""
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this GitHub action.')
    client = service.github_items
    fingerprint = hashlib.sha256((fingerprint + str(service.github_access.account().get('login'))).encode()).hexdigest()
    with service.business.write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], fingerprint):
                raise ServiceError(409, 'This request key belongs to a different change.')
            return json.loads(receipt[1])
        repository, number = data.get('repository'), data.get('number')
        if method == 'POST' and parts == ['comments']:
            if set(data) - {'repository', 'number', 'body'}:
                raise ServiceError(400, 'Send the repository, the number and the comment.')
            result = client.comment(repository, number, data, request_key)
        elif method == 'PATCH' and parts == []:
            if set(data) - {'repository', 'number', 'state', 'reason'}:
                raise ServiceError(400, 'Send the repository, the number and the state.')
            result = client.set_state(repository, number, data)
        elif method == 'POST' and parts == ['merge']:
            if set(data) - {'repository', 'number', 'sha'}:
                raise ServiceError(400, 'Send the repository, the number and the head commit.')
            result = client.merge(repository, number, data)
        else:
            raise ServiceError(404, 'This GitHub action was not found.')
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, fingerprint, encoded(result)))
        return result
