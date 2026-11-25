"""Tell the apps at once when GitHub or Gmail changes, so a page shows the change without a pull to refresh.

The apps read the to-dos, the reports of Finance and Support, issues, pull requests and support conversations from the
Mac while a page shows them (github_todos, business_reports, github_items, support_threads), and nothing told the Mac
when GitHub or Gmail changed: a list stayed as it was until the person pulled to refresh, or until the next timed read
minutes later (the person: "Make it so things update instantly and optimistically so one does not need to refresh to
see changes on GitHub Gmail finance chat and others"). While an app reads the updates (StateSync.updates), this watch
asks every few seconds whether one of these changed:

    issues   the issues and pull requests of the work repository (business.work_repository), by the newest change
    reports  the reports that the dashboards repository publishes for Finance, Support and Product (business_reports)
    mail     the mailbox that support conversations read, when this Mac has one (support_threads): its history ID

GitHub answers an unchanged read with 304 Not Modified, which costs nothing of its rate limit (GitHubBusiness._read).
A change drops what the Mac keeps for a short time (the to-dos, the risk reviews, the support handoffs and the GitHub
previews of the chats) and adds one `business.updated` event with the sources, which GET /v1/updates gives the apps as
`business_changed`; each app then reads its pages of the business again (AppStore.reloadChangedBusiness). The first
answer of a source only sets what the next ones are compared with, and nothing is asked while no app reads the updates.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlencode

from .business import work_repository
from .business_reports import FOLDER, reports_repository
from .service_base import ServiceError

# How often the watch asks while an app reads the updates.
WATCH_SECONDS = 5
# An app in front reads the updates at least every 25 seconds (StateSync.updates waits up to 25), so the watch goes on
# for a minute after the last read and stops when every app is in the background.
WATCHED_SECONDS = 60
# A Google token comes from a helper process (google_connection), so the watch keeps it, and no longer than the two
# minutes before its end in which GoogleProfile.access gets a new one.
MAIL_TOKEN_SECONDS = 100

LOG = logging.getLogger(__name__)


class BusinessWatch:
    def __init__(self, service, clock=time.monotonic):
        self.service = service
        self.clock = clock
        # The watch asks until this time of the clock.
        self.until = 0.0
        # The newest mark of each source.
        self.marks: dict[str, object] = {}
        # The token and the account of the mailbox, or None for a Mac without one, and until when they hold.
        self.mail: tuple | None = None
        self.thread: threading.Thread | None = None

    def watched(self):
        """An app reads the updates, so the watch asks for the next minute."""
        self.until = self.clock() + WATCHED_SECONDS

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='coconut-business-watch', daemon=True)
            self.thread.start()

    def _run(self):
        stopping = self.service.stopping
        while not stopping.is_set():
            if self.until <= self.clock():
                stopping.wait(1)
                continue
            try:
                self.check()
            except Exception:
                LOG.exception('Could not ask GitHub and Gmail for changes.')
            stopping.wait(WATCH_SECONDS)

    def check(self) -> list[str]:
        """Asks each source once and tells the apps of the sources that changed. Returns them."""
        changed = []
        for source, read in (('issues', self._issues), ('reports', self._reports), ('mail', self._mail)):
            try:
                mark = read()
            except ServiceError:
                # No connection, or GitHub or Gmail did not answer: the source keeps its mark until an answer.
                continue
            if mark is None:
                continue
            before = self.marks.get(source)
            self.marks[source] = mark
            if before is not None and before != mark:
                changed.append(source)
        if changed:
            self._tell(changed)
        return changed

    def _issues(self):
        """The newest change of an issue or a pull request: a new one, a comment, a label, a close."""
        business = self.service.business
        values = business._call('GET', f'repos/{business.owner}/{work_repository()}/issues?' + urlencode(
            dict(state='all', sort='updated', direction='desc', per_page=1)))
        newest = values[0] if isinstance(values, list) and values and isinstance(values[0], dict) else {}
        return [newest.get('id'), newest.get('updated_at')]

    def _reports(self):
        """The newest commit of the folder of the reports: the publisher writes each publish as one."""
        business = self.service.business
        values = business._call('GET', f'repos/{business.owner}/{reports_repository()}/commits?' + urlencode(
            dict(path=FOLDER, per_page=1)))
        newest = values[0] if isinstance(values, list) and values and isinstance(values[0], dict) else {}
        return newest.get('sha') or ''

    def _mail(self):
        """The history ID of the mailbox: Gmail raises it with each message and each change of a label."""
        from .support_threads import GMAIL, _access, _fetch
        if self.mail is None or self.mail[2] <= self.clock():
            try:
                token, account, _ = _access(self.service.root)
            except ServiceError:
                # A Mac without a Google connection asks again after a while, so a new connection is watched too.
                token = account = None
            self.mail = (token, account, self.clock() + MAIL_TOKEN_SECONDS)
        token, account, _ = self.mail
        if token is None:
            return None
        try:
            profile = _fetch(GMAIL + 'profile', token, account)
        except ServiceError:
            self.mail = None
            raise
        return str(profile.get('historyId') or '') or None

    def _tell(self, sources):
        service = self.service
        if 'issues' in sources or 'reports' in sources:
            # What the Mac keeps for a short time reads GitHub again: the to-dos, the risk reviews and the support
            # handoffs, which join a report with the issues of the work repository, and the previews of the chats.
            for kept in (service.todos, service.risk_reviews, service.support_handoffs, service.github_previews):
                kept.forget()
        with service.transaction():
            service._event('business.updated', {'sources': sorted(sources)})
