"""The risk reviews past SLA of Finance, decided in Coconut.

An account that nobody reviewed waits with its unpaid balance until a risk review: Gumroad pays a seller only after a
review marks the account compliant. The review is past SLA when the oldest unpaid balance of an account that holds over
$100 is older than 7 calendar days (Sahil, 2026-07-12). The dashboards publisher reads the accounts from the audited prod
console of the ops machine: it lists the reviews past SLA in risk.json (`pastSla`, antiwork/dashboards risk-dashboard.sh)
and counts them in home.json (`risk.past_sla`), the count of the badge of Finance.

The person decides each review here. A decision goes to Gumclaw, the agent of the Gumroad project, which holds the audited
admin access and reports what it did. Coconut files the review as an issue of the work repository with the label
`finance`, as all the work of the business is (business.work_repository), and the decision is its `@gumclaw` line, or a
comment when the review has an issue already. Gumclaw acts, reports in the issue and closes it, and the account leaves
the list at the next publish. A hidden marker names the account in the body of its issue, so the list shows each review
with its issue, and a repeated request key finds the decision that GitHub took before its answer was lost.

    GET  /v1/risk/reviews[?fresh=1]                 the reviews past SLA, oldest first, each with its issue
    POST /v1/risk/reviews {user, decision, note}    a decision on one account

The account (its external ID) travels in the body, so the request log keeps the route without it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .business import work_repository
from .business_reports import report
from .service_base import ServiceError, encoded

SLA_DAYS = 7
# Each device reads the list every few minutes; a decision drops it.
LIST_SECONDS = 60
MAX_REVIEWS = 200
# The issue of a review is work of Finance that changed in this time.
ISSUE_DAYS = 30
ISSUE_PAGES = 3
PAGE = 100
LABEL = 'finance'
REVIEW = re.compile(r'<!--\s*coconut-risk-review:([0-9]{1,20})\s*-->')
USER = re.compile(r'[0-9]{1,20}')
NOTE_LIMIT = 4000
# What each choice asks Gumclaw to do, after "@gumclaw, ". `ask` sends the person's own words instead.
DECISIONS = {
    'gumclaw': 'review {account} and decide whether to mark it compliant, add it to the watchlist or suspend it. '
               'Report what you did and why here',
    'compliant': 'mark {account} compliant, so its held balance pays out on schedule. Report here',
    'watch': 'add {account} to the watchlist with a revenue threshold that you choose. Report here',
    'suspend': 'suspend {account} for fraud. Report here',
    'ask': None,
}
CLOSE = ', and close this issue when it is done.'


def _since(value):
    """The oldest unpaid balance as the console writes it: `2026-11-13 01:24:35 UTC`."""
    text = str(value or '').strip()
    for pattern in ('%Y-%m-%d %H:%M:%S UTC', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _text(value, limit=200):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _cents(value):
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def money(cents):
    return '$' + format((cents or 0) / 100, ',.2f')


def account(user, now):
    """One account of the published list, with the fields that a row and the review show."""
    if not isinstance(user, dict):
        return None
    external = str(user.get('external_id') if not isinstance(user.get('external_id'), bool) else '').strip()
    if not USER.fullmatch(external):
        return None
    since = _since(user.get('unpaid_since'))
    return dict(user=external, name=_text(user.get('name')), username=_text(user.get('username'), 100),
                email=_text(user.get('email'), 254), balance_cents=_cents(user.get('balance_cents')),
                unpaid_since=since.strftime('%Y-%m-%dT%H:%M:%SZ') if since else None,
                days=max(0, int((now - since).total_seconds() // 86400)) if since else None)


def issue_summary(issue):
    title = issue.get('title') if isinstance(issue.get('title'), str) else ''
    return dict(number=issue['number'], state=issue.get('state') if issue.get('state') in ('open', 'closed') else 'open',
                title=title[:300], url=issue.get('html_url'), updated_at=issue.get('updated_at'),
                comments=issue.get('comments') if isinstance(issue.get('comments'), int) else 0)


class RiskReviews:
    def __init__(self, business, clock=None):
        self.business = business
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.cache = {}

    @property
    def repository(self):
        return f'{self.business.owner}/{work_repository()}'

    def _path(self):
        return 'repos/' + self.repository

    def forget(self):
        with self.lock:
            self.cache = {}

    def _issues(self):
        """The newest issue of the review of each account, by the marker in its body."""
        since = (self.clock() - timedelta(days=ISSUE_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        found = {}
        for page in range(1, ISSUE_PAGES + 1):
            values = self.business._call('GET', self._path() + '/issues?' + urlencode(dict(
                state='all', labels=LABEL, since=since, sort='updated', direction='desc', per_page=PAGE, page=page)))
            if not isinstance(values, list):
                raise ServiceError(502, 'GitHub returned an invalid list.')
            for issue in values:
                if not isinstance(issue, dict) or 'pull_request' in issue or not isinstance(issue.get('number'), int):
                    continue
                match = REVIEW.search(issue.get('body') or '')
                # Newest activity first, so the first issue of an account is its newest.
                if match and match.group(1) not in found:
                    found[match.group(1)] = issue
            if len(values) < PAGE:
                break
        return found

    def list(self, fresh=False):
        identity = hashlib.sha256(self.business._token().encode()).hexdigest()
        with self.lock:
            cached = self.cache.get(identity)
            if not fresh and cached and cached[0] > time.monotonic():
                return cached[1]
        published = report(self.business, 'risk')
        section = published['data'].get('pastSla')
        # A publisher before the list has only the count of home.json. The app then shows that count as one row.
        available = isinstance(section, dict) and isinstance(section.get('users'), list)
        reviews = []
        if available:
            now, issues = self.clock(), self._issues()
            for user in section['users'][:MAX_REVIEWS]:
                entry = account(user, now)
                if entry is None:
                    continue
                issue = issues.get(entry['user'])
                entry['issue'] = issue_summary(issue) if issue else None
                reviews.append(entry)
        days = section.get('days') if available else None
        result = dict(available=available, repository=self.repository, generated_at=published.get('generated_at'),
                      sla_days=days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else SLA_DAYS,
                      reviews=reviews)
        with self.lock:
            self.cache = {identity: (time.monotonic() + LIST_SECONDS, result)}
        return result

    def _account(self, user):
        """The account in the newest published list: a review past SLA, else any account that waits for a review."""
        data, now = report(self.business, 'risk')['data'], self.clock()
        for key in ('pastSla', 'notReviewed'):
            section = data.get(key)
            for row in (section.get('users') or [] if isinstance(section, dict) else []):
                entry = account(row, now)
                if entry and entry['user'] == user:
                    return entry
        return None

    @staticmethod
    def _named(entry):
        who = f'`{entry["username"]}`' if entry['username'] else entry['name'] or 'the account'
        return f'{who} (user {entry["user"]})'

    @staticmethod
    def _title(entry):
        name = entry['username'] or entry['name'] or 'user ' + entry['user']
        held = money(entry['balance_cents']) + ' held' + (f' {entry["days"]} days' if entry['days'] is not None else '')
        return f'Risk review: {name} ({entry["user"]}), {held}'[:256]

    @staticmethod
    def _body(entry, line, marker):
        parts = [entry['name'], f'`{entry["username"]}`' if entry['username'] else '', entry['email']]
        who = ', '.join(part for part in parts[1:] if part)
        who = (parts[0] + (f' ({who})' if who else '')) if parts[0] else who or 'An account'
        since = _since(entry['unpaid_since'])
        when = (f' since {since.strftime("%b %-d")}, {entry["days"]} days,' if since and entry['days'] is not None else '')
        return (f'{who}, Gumroad user {entry["user"]}, has {money(entry["balance_cents"])} unpaid{when} and no risk review '
                f'yet. The SLA of a review is {SLA_DAYS} days.\n\n{line}\n\n'
                f'<!-- coconut-risk-review:{entry["user"]} -->\n{marker}')

    def decide(self, data, request_key):
        if set(data) - {'user', 'decision', 'note'}:
            raise ServiceError(400, 'Send the account, the decision and an optional note.')
        user = data.get('user')
        user = str(user) if isinstance(user, int) and not isinstance(user, bool) else user
        if not isinstance(user, str) or not USER.fullmatch(user):
            raise ServiceError(400, 'Choose an account of the review queue.')
        decision = data.get('decision')
        if decision not in DECISIONS:
            raise ServiceError(400, 'Choose Let Gumclaw decide, Mark compliant, Watch, Suspend for fraud or Ask Gumclaw.')
        note = data.get('note')
        if decision == 'ask':
            if not isinstance(note, str) or not note.strip() or len(note) > NOTE_LIMIT:
                raise ServiceError(400, f'Write what to ask Gumclaw in up to {NOTE_LIMIT:,} characters.')
            if '<!-- coconut-' in note.lower():
                raise ServiceError(400, 'Remove the Coconut marker from the note.')
            note = note.strip()
        elif note is not None:
            raise ServiceError(400, 'Only Ask Gumclaw takes a note.')
        entry = self._account(user)
        if entry is None:
            raise ServiceError(404, 'This account no longer waits for a risk review. Refresh Finance.')
        line = '@gumclaw, ' + (note if decision == 'ask' else DECISIONS[decision].format(account=self._named(entry)) + CLOSE)
        signature = hashlib.sha256(encoded([user, decision, note]).encode()).hexdigest()
        prefix = f'<!-- coconut-risk:{request_key}:'
        marker = prefix + signature + ' -->'

        def found(body):
            # The decision of this request key is on GitHub already: GitHub took it before its answer was lost.
            if prefix not in (body or ''):
                return False
            if marker not in body:
                raise ServiceError(409, 'This request key already made a different decision.')
            return True

        path = self._path()
        issue = self._issues().get(user)
        if issue:
            number = issue['number']
            seen = found(issue.get('body'))
            total = issue.get('comments') if isinstance(issue.get('comments'), int) else 0
            if not seen and total:
                page = max(1, math.ceil(total / PAGE))
                comments = self.business._call('GET', f'{path}/issues/{number}/comments?per_page={PAGE}&page={page}')
                seen = any(isinstance(item, dict) and found(item.get('body')) for item in comments or [])
            if not seen:
                # A new decision on a closed review is new work: the issue opens again until Gumclaw closes it.
                if issue.get('state') == 'closed':
                    self.business._call('PATCH', f'{path}/issues/{number}', dict(state='open'))
                self.business._call('POST', f'{path}/issues/{number}/comments', dict(body=line + '\n\n' + marker))
            issue = self.business._call('GET', f'{path}/issues/{number}')
        else:
            recent = self.business._call('GET', path + '/issues?state=all&sort=created&direction=desc&per_page=25')
            issue = next((item for item in (recent if isinstance(recent, list) else [])
                          if isinstance(item, dict) and 'pull_request' not in item and found(item.get('body'))), None)
            if issue is None:
                from .github_todos import LABELS
                self.business.ensure_label(LABEL, *LABELS[LABEL])
                issue = self.business._call('POST', path + '/issues', dict(
                    title=self._title(entry), body=self._body(entry, line, marker), labels=[LABEL]))
        self.forget()
        if not isinstance(issue, dict) or not isinstance(issue.get('number'), int):
            raise ServiceError(502, 'GitHub did not confirm the decision. Refresh before deciding again.')
        return dict(entry, issue=issue_summary(issue), decision=decision)


def mutate_risk(service, method, data, request_key, fingerprint):
    """A decision from a device. A repeat with the same key gets the saved answer, which is saved only after GitHub
    confirmed the write (business.mutate_business)."""
    if method != 'POST':
        raise ServiceError(404, 'This risk action was not found.')
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this decision.')
    fingerprint = hashlib.sha256((fingerprint + str(service.github_access.account().get('login'))).encode()).hexdigest()
    with service.business.write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], fingerprint):
                raise ServiceError(409, 'This request key belongs to a different change.')
            return json.loads(receipt[1])
        result = service.risk_reviews.decide(data, request_key)
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, fingerprint, encoded(result)))
        return result
