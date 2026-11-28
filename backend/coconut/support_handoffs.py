"""The customer conversations of Support that the person hands to Gumclaw in Coconut, with the choices of a poll.

Gumclaw, the agent of the Gumroad project, answers the support mailbox: it reads each conversation, acts with its audited
admin access and sends the answer, and its labels mark a conversation open or done (support_threads.py). A conversation
that waits in the queue of Support is one that Gumclaw left open. Its page offers the person how to respond as the
options of a poll, beside the box for an answer of their own: Let Gumclaw answer, Refund and answer, or Close without
answer. A Mac with the support mailbox closes a conversation itself; any other Mac asks Gumclaw to close it, because
a team member never connects Gmail in the app.

A choice goes to Gumclaw as a risk review does (risk_reviews.py): Coconut files the conversation as an issue of the work
repository with the label `support` and an `@gumclaw` line, or comments on the issue that the conversation has already.
Gumclaw answers the customer, reports in the issue and closes it with the conversation, which leaves the queue at the
next publish. A hidden marker names the Gmail thread in the body of its issue, so the page shows the issue of its
conversation, and a repeated request key finds the choice that GitHub took before its answer was lost.

    GET  /v1/support/handoffs[?fresh=1]                    the issue of each conversation that went to Gumclaw
    POST /v1/support/handoffs {thread, decision, subject}  a choice on one conversation

The Gmail thread travels in the body, so the request log keeps the route without it.
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
from .risk_reviews import issue_summary
from .service_base import ServiceError, encoded
from .support_threads import THREAD

# Each page of a conversation reads the list; a choice drops it.
LIST_SECONDS = 60
# The issue of a conversation is work of Support that changed in this time.
ISSUE_DAYS = 30
ISSUE_PAGES = 3
PAGE = 100
LABEL = 'support'
HANDOFF = re.compile(r'<!--\s*coconut-support:([0-9a-f]{8,32})\s*-->')
SUBJECT_LIMIT = 300
# What each choice asks Gumclaw to do, after "@gumclaw, ".
DECISIONS = {
    'gumclaw': 'answer the customer in {conversation} from the support mailbox. Decide what to tell them from what you '
               'find, and report what you sent and why here',
    'refund': 'refund the purchase that {conversation} is about and tell the customer from the support mailbox. '
              'Report the refund and the answer here',
    # A Mac without the support mailbox closes through Gumclaw too (support_threads.py).
    'close': 'send no answer to the customer in {conversation}',
}
CLOSE = ', then mark the conversation done and close this issue.'


def _date(value):
    text = value.strip() if isinstance(value, str) else ''
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')) if text else None
    except ValueError:
        return None


def _subject(value):
    return ' '.join(value.split())[:SUBJECT_LIMIT] if isinstance(value, str) else ''


class SupportHandoffs:
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
        """The newest issue of each conversation, by the marker in its body."""
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
                match = HANDOFF.search(issue.get('body') or '')
                # Newest activity first, so the first issue of a conversation is its newest.
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
        handoffs = [dict(thread=thread, issue=issue_summary(issue)) for thread, issue in self._issues().items()]
        result = dict(repository=self.repository, handoffs=handoffs)
        with self.lock:
            self.cache = {identity: (time.monotonic() + LIST_SECONDS, result)}
        return result

    def _queued(self, thread):
        """The conversation in the newest published queue, or None when the queue does not have it."""
        try:
            section = report(self.business, 'support')['data'].get('support')
        except ServiceError:
            return None
        for row in (section.get('open') or [] if isinstance(section, dict) else []):
            if isinstance(row, dict) and str(row.get('slug') or '').lower() == thread:
                return row
        return None

    def _body(self, thread, subject, row, line, marker):
        row = row if isinstance(row, dict) else {}
        waiting = _date(row.get('lastUserEmailCreatedAt')) or _date(row.get('createdAt'))
        state = ' waits for an answer.'
        if waiting:
            days = max(0, int((self.clock() - waiting).total_seconds() // 86400))
            state = (f' has waited in the queue of Support since {waiting.strftime("%b %-d")}'
                     + (f' ({days} days).' if days > 1 else ' (1 day).' if days == 1 else '.'))
        named = f'The conversation "{subject}"' if subject else 'A conversation'
        return (f'{named} (Gmail thread `{thread}` of the support mailbox){state}\n\n'
                f'{line}\n\n<!-- coconut-support:{thread} -->\n{marker}')

    def decide(self, data, request_key):
        if set(data) - {'thread', 'decision', 'subject'}:
            raise ServiceError(400, 'Send the conversation, the choice and its subject.')
        thread = data.get('thread')
        if not isinstance(thread, str) or not THREAD.fullmatch(thread):
            raise ServiceError(400, 'Choose a conversation of the support queue.')
        thread = thread.lower()
        decision = data.get('decision')
        if decision not in DECISIONS:
            raise ServiceError(400, 'Choose Let Gumclaw answer, Refund and answer, or Close without answer.')
        if data.get('subject') is not None and not isinstance(data.get('subject'), str):
            raise ServiceError(400, 'Send the subject as text.')
        if '<!-- coconut-' in (data.get('subject') or '').lower():
            raise ServiceError(400, 'Remove the Coconut marker from the subject.')
        row = self._queued(thread)
        # The published queue names the conversation; one that it does not have yet keeps the subject of the page.
        subject = _subject((row or {}).get('subject')) or _subject(data.get('subject'))
        conversation = f'the Gmail thread `{thread}`' + (f' ("{subject}")' if subject else '')
        line = '@gumclaw, ' + DECISIONS[decision].format(conversation=conversation) + CLOSE
        signature = hashlib.sha256(encoded([thread, decision]).encode()).hexdigest()
        prefix = f'<!-- coconut-support-choice:{request_key}:'
        marker = prefix + signature + ' -->'

        def found(body):
            # The choice of this request key is on GitHub already: GitHub took it before its answer was lost.
            if prefix not in (body or ''):
                return False
            if marker not in body:
                raise ServiceError(409, 'This request key already made a different choice.')
            return True

        path = self._path()
        issue = self._issues().get(thread)
        if issue:
            number = issue['number']
            seen = found(issue.get('body'))
            total = issue.get('comments') if isinstance(issue.get('comments'), int) else 0
            if not seen and total:
                page = max(1, math.ceil(total / PAGE))
                comments = self.business._call('GET', f'{path}/issues/{number}/comments?per_page={PAGE}&page={page}')
                seen = any(isinstance(item, dict) and found(item.get('body')) for item in comments or [])
            if not seen:
                # A new choice on a closed conversation is new work: the issue opens again until Gumclaw closes it.
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
                title = ('Support: ' + (subject or f'Gmail thread {thread}'))[:256]
                issue = self.business._call('POST', path + '/issues', dict(
                    title=title, body=self._body(thread, subject, row, line, marker), labels=[LABEL]))
        self.forget()
        if not isinstance(issue, dict) or not isinstance(issue.get('number'), int):
            raise ServiceError(502, 'GitHub did not confirm the choice. Refresh before choosing again.')
        return dict(thread=thread, decision=decision, issue=issue_summary(issue))


def mutate_handoff(service, method, data, request_key, fingerprint):
    """A choice from a device. A repeat with the same key gets the saved answer, which is saved only after GitHub
    confirmed the write (business.mutate_business)."""
    if method != 'POST':
        raise ServiceError(404, 'This support action was not found.')
    if not isinstance(data, dict):
        raise ServiceError(400, 'Send a JSON object for this choice.')
    fingerprint = hashlib.sha256((fingerprint + str(service.github.account().get('login'))).encode()).hexdigest()
    with service.business.write_lock:
        with service._read_view() as view:
            receipt = view.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (request_key,)).fetchone()
        if receipt:
            if not hmac.compare_digest(receipt[0], fingerprint):
                raise ServiceError(409, 'This request key belongs to a different change.')
            return json.loads(receipt[1])
        result = service.support_handoffs.decide(data, request_key)
        with service.transaction():
            service.db.execute('INSERT INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                               (request_key, fingerprint, encoded(result)))
        return result
