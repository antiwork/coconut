"""Notifications of GitHub and Gmail, so Coconut takes the place of their apps (the person: "Build out slack like
notification settings as that's needed to replace slack. Telegram. GitHub. Gmail. Want to replace all Those with
this.").

Each minute, the Mac reads each source whose level is not `nothing` (notification_settings.py):

    github  the unread threads of the GitHub inbox of the connected account (GET /notifications, 304 when unchanged)
    mail    the history of the inbox of the Google connection of the person (support_threads._access, personal)

A GitHub thread with a new `updated_at` is new, and so is a message that came unread into the Gmail inbox. At
`mentions`, GitHub notifies of mentions, review requests and assignments. At `important`, Gmail notifies of the mail
that it marks important. What is older than an hour never notifies, and the first read of an account only sets what
the next ones are compared with.

A new item goes to the iPhones (push.record_source), which follow the pause, the wait while the person works at a Mac
and the work hours, and to the alerts that the Mac shows as banners: GET /v1/notifications/alerts, and a
`business.updated` event with the source `alerts` when they change. What the person reads on GitHub or in Gmail leaves
the alerts, and so does an older item of a GitHub thread. An item that is no longer among them does not notify. The
marks and the alerts are kept in the settings table, so a restart neither notifies again nor removes a banner.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import quote, urlencode

from .notification_settings import notifies
from .service_base import ServiceError, encoded

KEY = 'notification_sources'
# GitHub asks for at most one read a minute (X-Poll-Interval).
POLL_SECONDS = 60
# An item older than this never notifies: a Mac that was asleep, or a thread that came back to the first page.
FRESH_SECONDS = 3600
# The alerts that the Mac keeps, and the GitHub threads of one read.
ALERTS = 50
# A Google token comes from a helper process, so the read keeps it for less than the two minutes before its end in
# which GoogleProfile.access gets a new one (business_watch).
MAIL_TOKEN_SECONDS = 100
# The pages of mail history of one read, and the new messages whose sender and subject it reads.
MAIL_PAGES = 5
MAIL_MESSAGES = 10
# What notifies at `mentions`. Agents work with the token of the person, so `author`, a thread that the person opened,
# is mostly the work of agents.
MENTIONS = ('mention', 'team_mention', 'review_requested', 'assign')
REASONS = {
    'mention': 'Mentioned you', 'team_mention': 'Mentioned your team', 'review_requested': 'Review requested',
    'assign': 'Assigned to you', 'author': 'Your thread', 'comment': 'New comment', 'state_change': 'State changed',
    'ci_activity': 'CI activity', 'security_alert': 'Security alert', 'invitation': 'Invitation',
    'approval_requested': 'Approval requested', 'subscribed': 'Watching', 'manual': 'Subscribed',
}

LOG = logging.getLogger(__name__)


def _seconds(value) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def github_item(thread: dict) -> dict:
    """What a thread of the GitHub inbox shows and opens: the repository and number, the reason and the title."""
    subject = thread.get('subject') if isinstance(thread.get('subject'), dict) else {}
    repository = thread.get('repository') if isinstance(thread.get('repository'), dict) else {}
    name = str(repository.get('full_name') or '')
    home = str(repository.get('html_url') or (f'https://github.com/{name}' if name else 'https://github.com/notifications'))
    kind = subject.get('type')
    tail = str(subject.get('url') or '').rstrip('/').rsplit('/', 1)[-1]
    number = int(tail) if kind in ('Issue', 'PullRequest') and tail.isdigit() else None
    if number is not None:
        url = f"{home}/{'pull' if kind == 'PullRequest' else 'issues'}/{number}"
    elif kind == 'Commit' and tail:
        url = f'{home}/commit/{tail}'
    else:
        url = home + {'Release': '/releases', 'Discussion': '/discussions', 'CheckSuite': '/actions'}.get(kind, '')
    updated = str(thread.get('updated_at') or '')
    item = {'id': f"github:{thread['id']}:{updated}", 'kind': 'github',
            'title': f'{name} #{number}' if number is not None else name or 'GitHub',
            'subtitle': REASONS.get(str(thread.get('reason') or ''), ''), 'body': str(subject.get('title') or ''),
            'url': url, 'repository': name, 'thread': str(thread['id']), 'updated': updated, 'created_at': updated}
    if number is not None:
        item['number'] = number
    return item


def mail_item(message: dict) -> dict:
    """What a message shows and opens: the sender, the subject, the start of the text, and its thread."""
    from .support_threads import _headers
    headers = _headers(message)
    name, address = parseaddr(headers.get('from', ''))
    try:
        sent = int(message.get('internalDate') or 0) / 1000
    except (TypeError, ValueError):
        sent = 0
    return {'id': 'mail:' + str(message['id']), 'kind': 'mail', 'title': name or address or 'Gmail',
            'subtitle': headers.get('subject', ''), 'body': html.unescape(str(message.get('snippet') or '')),
            'thread': str(message.get('threadId') or message['id']), 'message': str(message['id']),
            'created_at': _stamp(sent) if sent else ''}


def _wanted(labels, level) -> bool:
    """An unread message of the inbox that the person did not write, and at `important` one that Gmail marks so."""
    labels = set(labels or [])
    return ({'INBOX', 'UNREAD'} <= labels and not labels & {'SENT', 'DRAFT'}
            and (level == 'all' or 'IMPORTANT' in labels))


def _everything(alert) -> bool:
    return True


class NotificationSources:
    def __init__(self, service, clock=time.time):
        self.service = service
        self.clock = clock
        # The marks of each source and the alerts, as the settings table keeps them. Only `check` replaces them, and it
        # does so in its transaction, so a send in the transaction of the push sees the alerts with their items.
        self.marks: dict | None = None
        # The token and the account of the mailbox, and until when they hold.
        self.mail: tuple | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='coconut-notification-sources', daemon=True)
            self.thread.start()

    def _run(self):
        stopping = self.service.stopping
        while not stopping.is_set():
            try:
                self.check()
            except Exception:
                LOG.exception('Could not read the notifications of GitHub and Gmail.')
            stopping.wait(POLL_SECONDS)

    def _kept(self) -> dict:
        if self.marks is None:
            kept = self.service._setting(KEY, {})
            self.marks = kept if isinstance(kept, dict) else {}
        return self.marks

    def current(self) -> list[dict]:
        """GET /v1/notifications/alerts: the alerts, newest first, for the banners of the Mac."""
        return [dict(alert) for alert in self._kept().get('alerts') or []]

    def relevant(self, data) -> bool:
        """Whether a queued item still notifies: it is among the alerts, so it is unread, and no newer item of its
        GitHub thread came."""
        return any(alert.get('id') == data.get('id') for alert in self._kept().get('alerts') or [])

    def check(self) -> list[dict]:
        """Reads each source once, queues what is new and notifies, and keeps the marks and the alerts. Returns the
        items that notify."""
        service = self.service
        settings = service.notification_settings()
        before = self._kept()
        marks = {key: value for key, value in before.items() if key != 'alerts'}
        alerts = [dict(alert) for alert in before.get('alerts') or [] if isinstance(alert, dict)]
        new = []
        for kind, read in (('github', self._github), ('mail', self._mail)):
            level = settings.get(kind, 'nothing')
            if level != 'nothing':
                try:
                    items, mark, keep = read(level, marks.get(kind) or {})
                except ServiceError as error:
                    if error.status != 409:
                        # GitHub or Gmail did not answer: the source keeps its mark and its alerts until an answer.
                        continue
                    # No connection, or it needs a new sign-in: a new one starts again from its first read.
                else:
                    marks[kind] = mark
                    alerts = [alert for alert in alerts if alert.get('kind') != kind or keep(alert)]
                    new += items
                    continue
            marks.pop(kind, None)
            alerts = [alert for alert in alerts if alert.get('kind') != kind]
        known = {alert.get('id') for alert in alerts}
        notifying = [item for item in new if item['id'] not in known and notifies(settings, item['kind'])]
        for item in sorted(notifying, key=lambda item: item.get('created_at') or ''):
            if item['kind'] == 'github':
                alerts = [alert for alert in alerts if alert.get('kind') != 'github' or alert.get('thread') != item['thread']]
            alerts.insert(0, item)
        result = dict(marks, alerts=alerts[:ALERTS])
        if result == before:
            return notifying
        changed = [alert.get('id') for alert in result['alerts']] != [alert.get('id') for alert in before.get('alerts') or []]
        try:
            with service.transaction():
                for item in notifying:
                    service.push.record_source(item)
                service.db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   (KEY, encoded(result)))
                if changed:
                    service._event('business.updated', {'sources': ['alerts']})
                self.marks = result
        except BaseException:
            self.marks = before
            raise
        return notifying

    def _github(self, level, before):
        """The new threads of the GitHub inbox, the mark of the next read, and which alerts stay."""
        service = self.service
        values = service.business._call('GET', 'notifications?' + urlencode({'per_page': ALERTS}))
        login = str(service.github_access.account().get('login') or '')
        threads = [value for value in values if isinstance(value, dict) and value.get('id')] if isinstance(values, list) else []
        unread = {str(thread['id']): str(thread.get('updated_at') or '') for thread in threads}
        mark = {'account': login, 'threads': unread}
        keep = lambda alert: unread.get(alert.get('thread')) == alert.get('updated')
        if before.get('account') != login:
            return [], mark, keep
        seen = before.get('threads') or {}
        current = self.clock()
        items = []
        for thread in threads:
            updated = unread[str(thread['id'])]
            moment = _seconds(updated)
            if seen.get(str(thread['id'])) == updated or moment is None or current - moment > FRESH_SECONDS:
                continue
            if level == 'all' or thread.get('reason') in MENTIONS:
                items.append(github_item(thread))
        return items, mark, keep

    def _mail(self, level, before):
        """The new messages of the Gmail inbox, the mark of the next read, and which alerts stay."""
        from .support_threads import _access
        if self.mail is None or self.mail[2] <= self.clock():
            token, account, _ = _access(self.service.root, personal=True)
            self.mail = (token, account, self.clock() + MAIL_TOKEN_SECONDS)
        try:
            return self._history(level, before, *self.mail[:2])
        except ServiceError:
            self.mail = None
            raise

    def _history(self, level, before, token, account):
        from .support_threads import GMAIL, _fetch
        start = before.get('history') if before.get('account') == account else None
        baseline = lambda: ([], {'account': account, 'history': str(_fetch(GMAIL + 'profile', token, account).get('historyId') or '')},
                            _everything)
        if not start:
            return baseline()
        added, done, page, newest = {}, set(), None, start
        for _ in range(MAIL_PAGES):
            query = [('startHistoryId', start), ('historyTypes', 'messageAdded'), ('historyTypes', 'labelRemoved'),
                     ('historyTypes', 'messageDeleted'), ('maxResults', 100)] + ([('pageToken', page)] if page else [])
            try:
                answer = _fetch(GMAIL + 'history?' + urlencode(query), token, account)
            except ServiceError as error:
                if error.status != 404:
                    raise
                # Gmail no longer keeps the history from the mark: the read starts again from now.
                return baseline()
            newest = str(answer.get('historyId') or newest)
            for record in answer.get('history') or []:
                for change in record.get('messagesAdded') or []:
                    message = change.get('message') or {}
                    if message.get('id'):
                        added[message['id']] = message
                for change in record.get('labelsRemoved') or []:
                    if {'UNREAD', 'INBOX'} & set(change.get('labelIds') or []):
                        done.add((change.get('message') or {}).get('id'))
                for change in record.get('messagesDeleted') or []:
                    done.add((change.get('message') or {}).get('id'))
            page = answer.get('nextPageToken')
            if not page:
                break
        wanted = [key for key, message in added.items() if key not in done and _wanted(message.get('labelIds'), level)]
        current = self.clock()
        items = []
        for key in reversed(wanted[-MAIL_MESSAGES:]):
            try:
                message = _fetch(GMAIL + f'messages/{quote(key, safe="")}?' + urlencode(
                    [('format', 'metadata'), ('metadataHeaders', 'From'), ('metadataHeaders', 'Subject')]), token, account)
            except ServiceError as error:
                if error.status != 404:
                    raise
                continue
            item = mail_item(message)
            sent = _seconds(item['created_at'])
            if _wanted(message.get('labelIds'), level) and sent is not None and current - sent <= FRESH_SECONDS:
                items.append(item)
        return items, {'account': account, 'history': newest}, lambda alert: alert.get('message') not in done
