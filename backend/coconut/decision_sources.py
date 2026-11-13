"""Keep the sources for a decision visible in chat and Inbox."""
import hashlib
import json
from urllib.parse import urlsplit


def attach_sources(decision, payload):
    from .service import ServiceError

    if 'sources' not in payload:
        return
    raw = payload['sources']
    if not isinstance(raw, list) or len(raw) > 8:
        raise ServiceError(400, 'Supply at most eight sources for this decision.')
    sources = []
    for source in raw:
        if (not isinstance(source, dict) or source.get('kind') not in ('email', 'issue', 'pull_request', 'web')
                or not isinstance(source.get('title'), str) or not 1 <= len(source['title'].strip()) <= 200):
            raise ServiceError(400, 'Each source needs a type and a short title.')
        url = source.get('url')
        if url is not None:
            try:
                if not isinstance(url, str) or len(url) > 4000 or any(char.isspace() for char in url):
                    raise ValueError()
                parsed = urlsplit(url)
                if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError()
                _ = parsed.port
            except ValueError:
                raise ServiceError(400, 'Use the source’s full HTTP or HTTPS link, or omit it.') from None
        item = dict(kind=source['kind'], title=source['title'].strip(), url=url)
        if item not in sources:
            sources.append(item)
    decision['sources'] = sources


def github_source(url):
    """Return one canonical GitHub issue or PR location. Ignore link fragments."""
    import re
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname != 'github.com'
                or parsed.username or parsed.password or parsed.port not in (None, 443, 80)):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9-]{0,38})/'
        r'([A-Za-z0-9_.-]{1,100})/(issues|pull)/([1-9][0-9]{0,9})'
        r'(?:/(files|commits|checks|changes))?/?', parsed.path)
    if not match or match[2] in ('.', '..') or (match[3] == 'issues' and match[5]):
        return None
    kind = 'issues' if match[3] == 'issues' else 'pulls'
    return kind, '/'.join((match[1], match[2], kind, match[4])), f'https://github.com/{match[1]}/{match[2]}/{match[3]}/{match[4]}'


def github_links(decision):
    values = [s.get('url') for s in decision.get('sources', [])] + [decision.get('merge_pr_url')]
    return sorted({source[2] for value in values if (source := github_source(value))})


def decision_source_keys(decision):
    """Match source identities without changing their displayed links."""
    from urllib.parse import parse_qs, unquote
    import re

    keys = set()
    for url in github_links(decision):
        # GitHub uses one number space for issues and PRs; /issues/N can redirect to /pull/N.
        owner, repo, _, number = urlsplit(url).path.strip('/').split('/')
        keys.add(f'github:{owner.lower()}/{repo.lower()}/{number}')
    for source in decision.get('sources', []):
        if source.get('kind') != 'email' or not source.get('url'):
            continue
        url = source['url']
        parsed = urlsplit(url)
        if parsed.hostname == 'mail.google.com' and re.fullmatch(r'/mail(?:/u/\d+)?/?', parsed.path):
            # Gmail's account selector and folder do not identify the thread.
            parts = [unquote(part) for part in parsed.fragment.strip('/').split('/')]
            thread = parse_qs(parsed.query).get('th', [''])[0]
            if not thread and len(parts) >= 2 and parts[0] in (
                    'inbox', 'all', 'sent', 'drafts', 'spam', 'trash', 'starred', 'imp', 'snoozed', 'label', 'search'):
                if parts[0] not in ('label', 'search') or len(parts) >= 3:
                    thread = parts[-1]
            # Short route tokens such as p2 are pages, not thread identities.
            if re.fullmatch(r'(?:[A-Fa-f0-9]{16,}|[A-Za-z0-9_-]{20,})', thread):
                url = 'gmail-thread:' + thread
        keys.add(url)
    return keys


def check_new_decision_sources(service, task_id, payload):
    """Check the source version before storing a card or asking for attention.

    Return the issue facts and the verified revision of each GitHub link.
    """
    from .service import ServiceError

    decision = {'merge_pr_url': payload.get('merge_pr_url')}
    attach_sources(decision, payload)
    links = github_links(decision)
    if not links:
        return {}, {}
    revisions = payload.get('source_revisions', {})
    if not isinstance(revisions, dict) or any(not isinstance(revisions.get(url), str) for url in links):
        raise ServiceError(400, 'No card was created. Read each linked GitHub issue or PR and its latest comments. '
            'Check the original human request and standing permission. Continue without a card if the choice is already settled '
            'or the work is already authorized. If human input remains necessary, retry with source_revisions: '
            'map each source URL to the exact updated_at from the GitHub issues API (also for PRs).')
    with service.lock:
        task = service._get('tasks', task_id)
    tracker = service._pr_tracker_for(task['bot_id'])
    facts = {}
    for url in links:
        _, route, _ = github_source(url)
        owner, repo, _, number = route.split('/')
        issue_url = f'https://github.com/{owner}/{repo}/issues/{number}'
        current = tracker.observe_issue(issue_url)
        if current is None:
            raise ServiceError(400, 'No card was created. Coconut could not verify the current GitHub source: ' + url
                + '. Keep the work pending and retry the source check. Do not create a permission card for this check.')
        if current['updated_at'] != revisions[url]:
            raise ServiceError(400, 'No card was created. The GitHub source changed: ' + url
                + '. Read its current state and latest comments before deciding whether human input is still needed. '
                'Continue within existing permission if the choice is already settled. Otherwise retry with the updated source_revisions.')
        if github_source(url)[0] == 'issues':
            facts[url] = issue_facts(current)
    return facts, {url: revisions[url] for url in links}


def issue_facts(current):
    """The issue facts that a card keeps. The time of the close only decides a close (source_change), so a card does
    not keep it: a new fact would make the assessment of each card with an issue stale once."""
    return {key: value for key, value in current.items() if key != 'closed_at'}


def _instant(value):
    from datetime import datetime, timezone
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def source_change(decision, url, *, state, ended_at=None, head_sha=None):
    """Return the outcome of a card whose GitHub source changed after the card was made, or None.

    A closed issue, and a merged or closed PR, count only when GitHub's time of the end is later than the card. The
    time of the card is the source revision that the bot read before Coconut made the card (`_source_seen`, a GitHub
    time), or else the card's own time. A source that had ended before the card is not a change. A new head of the PR
    of a merge card is a change. The caller gives only facts that GitHub returned.
    """
    number = url.rstrip('/').rsplit('/', 1)[1]
    merge = (decision.get('merge_pr_url') or '').lower() == url.lower()
    if merge and state == 'merged':
        # _resolve_merged_pr_decisions closes this card with "Already merged.".
        return None
    if (merge and state == 'open' and head_sha and decision.get('_merge_pr_head')
            and head_sha != decision['_merge_pr_head']):
        return f'Pull request #{number} has new commits.'
    if state not in ('closed', 'merged'):
        return None
    end = _instant(ended_at)
    start = _instant(decision.get('_source_seen', {}).get(url) or decision.get('created_at'))
    if end is None or start is None or end <= start:
        return None
    if github_source(url)[0] == 'issues':
        return f'Issue #{number} closed on GitHub.'
    return f'Pull request #{number} {state} on GitHub.'


def _github_time(value):
    """Return a GitHub `since` value, or None if the value is not a time."""
    from datetime import datetime, timezone
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _issue_url(url):
    # GitHub uses one number space for issues and PRs. One key covers both links.
    owner, repo, _, number = github_source(url)[1].split('/')
    return f'https://github.com/{owner}/{repo}/issues/{number}'


SOURCE_READ_SECONDS = 6
# A turn reads each linked GitHub thread before it starts: about 0.6 s per read on the Mac mini, and 1.2 s per turn at
# p50. Most threads did not change since the revision that Coconut recorded. A background refresher keeps the read of
# each thread that a turn used in the last THREAD_KEEP_SECONDS: the thread at its latest revision, with the items of that
# second, as a turn reads it. It reads at most THREAD_KEEP_COUNT threads again every THREAD_REFRESH_SECONDS. A turn
# that reads a thread at its revision second keeps its read too, so the next turn of the thread can use it.
# A turn uses a kept read that is at most THREAD_RECORD_SECONDS old only when it shows nothing new for that turn: the
# thread did not change after the recorded revision, and each item of the revision second was delivered. Then the
# activity and the recorded revision are the ones that a read at the turn returns. Any new activity reads the thread at
# the turn, as before. A change after the kept read shows at the next turn, because a turn records only what it saw.
#
# All bots share one GitHub login with 5,000 requests an hour. The refresher reads with conditional requests, and GitHub
# does not count a 304 answer against the limit, so an unchanged thread costs nothing. The refresher sends at most
# THREAD_REFRESH_BUDGET requests that GitHub can count in any hour: each request counts against the budget before it
# goes out, and a request beyond the budget stops the read. It also stops while GitHub reports fewer than
# THREAD_QUOTA_FLOOR remaining requests for the login. Then turns read their threads themselves, as before.
THREAD_RECORD_SECONDS = 75
THREAD_REFRESH_SECONDS = 60
THREAD_KEEP_SECONDS = 900
THREAD_KEEP_COUNT = 6
THREAD_REFRESH_BUDGET = 300
THREAD_QUOTA_FLOOR = 1000
# A read with this `since` returns the thread facts without activity.
_LATEST = '9999-12-31T23:59:59Z'
_NO_ACTIVITY = dict(new_activity=[], omitted_items=0, incomplete=False)


class ThreadRecords:
    """The kept reads of the GitHub threads that turns used, by token, thread and trust."""

    def __init__(self, service):
        import threading
        self.service = service
        self.guard = threading.Lock()
        self.records = {}
        self.used = {}
        self.thread = None
        # (time, counted requests) of each refresh read in the last hour.
        self.spent = []

    @staticmethod
    def key(tracker, url, operator):
        return hashlib.sha256((getattr(tracker, '_token', '') or '').encode()).hexdigest(), _issue_url(url).lower(), operator

    def kept(self, tracker, bot_id, url, operator, since, delivered):
        """What a read at the turn returns when the kept read shows nothing new for this turn, else None.

        The refresher keeps the bot, not its tracker, so a token stays in memory no longer than the service keeps it.
        """
        import time
        key = self.key(tracker, url, operator)
        with self.guard:
            self.used[key] = (time.monotonic(), url, bot_id)
            kept = self.records.get(key)
            self._start()
        if kept is None or since is None or time.monotonic() - kept[0] > THREAD_RECORD_SECONDS:
            return None
        current = json.loads(kept[1])
        if current['updated_at'] < since:
            # A read of a thread that did not change after `since` returns its facts and no activity.
            current.pop('changed_without_new_activity', None)
            return dict(current, **_NO_ACTIVITY, observed_at=current['updated_at'])
        if (current['updated_at'] == since and not current.get('incomplete')
                and all(activity_signature(item) in delivered for item in current.get('new_activity', []))):
            # The items of the revision second were all delivered. The turn shows nothing for this thread.
            return current
        return None

    def budget_left(self, now):
        """The counted requests that the refresher may still spend in the current hour."""
        self.spent = [(at, cost) for at, cost in self.spent if now - at < 3600]
        return THREAD_REFRESH_BUDGET - sum(cost for _, cost in self.spent)

    @staticmethod
    def quota_low(tracker):
        """True while GitHub reports fewer than THREAD_QUOTA_FLOOR remaining requests for the tracker's login."""
        import time
        from .pr_tracking import quota
        reported = quota(getattr(tracker, '_token', ''))
        return reported is not None and reported[0] < THREAD_QUOTA_FLOOR and reported[1] > time.time()

    def keep(self, tracker, bot_id, url, operator, current, started):
        """Keep a turn's read at the revision second, as the refresher keeps its reads."""
        import time
        key = self.key(tracker, url, operator)
        with self.guard:
            self.used[key] = (time.monotonic(), url, bot_id)
            previous = self.records.get(key)
            if previous is None or previous[0] < started:
                self.records[key] = (started, json.dumps(current))

    def miss_reason(self, tracker, url, operator, since):
        """Why a turn read this thread itself: no kept read, an old one, no revision, a change, or an undelivered item."""
        import time
        with self.guard:
            kept = self.records.get(self.key(tracker, url, operator))
        if kept is None:
            return 'none'
        if since is None:
            return 'no_revision'
        if time.monotonic() - kept[0] > THREAD_RECORD_SECONDS:
            return 'old'
        return 'changed' if json.loads(kept[1])['updated_at'] > since else 'undelivered'

    def state(self):
        """The refresher counts for the log: kept reads, threads in use, and the counted requests of the last hour."""
        import time
        with self.guard:
            return dict(kept=len(self.records), used=len(self.used),
                        budget_left=THREAD_REFRESH_BUDGET - sum(cost for at, cost in self.spent
                                                                if time.monotonic() - at < 3600))

    def refresh(self):
        """Read each thread that a turn used lately, at its latest revision. Drop the others."""
        import time
        from .pr_tracking import budget_stopped, own_responses, request_budget, spent_requests
        now = time.monotonic()
        with self.guard:
            for key in [key for key, (used, _, _) in self.used.items() if now - used > THREAD_KEEP_SECONDS]:
                self.used.pop(key)
                self.records.pop(key, None)
            due = sorted(self.used.items(), key=lambda item: item[1][0], reverse=True)[:THREAD_KEEP_COUNT]
        for key, (_, url, bot_id) in due:
            if self.service.stopping.is_set():
                return
            started = time.monotonic()
            left = self.budget_left(started)
            if left <= 0:
                # The hour's budget is spent. The kept reads age out, and turns read their threads themselves.
                return
            current = None
            before = spent_requests()
            stopped = False
            try:
                tracker = self.service._pr_tracker_for(bot_id)
                if self.quota_low(tracker):
                    return
                if self.key(tracker, url, key[2]) == key:
                    from .pr_tracking import GitHubPRTracker
                    # The same options as a read at the turn (see read_source_activity), with conditional lists.
                    options = dict(operator=key[2])
                    if isinstance(tracker, GitHubPRTracker):
                        options.update(include_same_second=True, cache_lists=True)
                    with self.guard:
                        previous = self.records.get(key)
                    since = json.loads(previous[1])['updated_at'] if previous else _LATEST
                    # Each request counts against the rest of the hour's budget before it goes out.
                    # Its own response cache keeps its conditional requests free: the PR poll cannot push them out.
                    with request_budget(left), own_responses('thread-refresh'):
                        for _ in range(2):
                            current = tracker.observe_issue_activity(url, since, started + SOURCE_READ_SECONDS,
                                                                     **options)
                            if not isinstance(current, dict) or not isinstance(current.get('updated_at'), str):
                                current = None
                                break
                            if current['updated_at'] == since:
                                break
                            # The thread changed. Read it again at its new revision second.
                            since, current = current['updated_at'], None
                        stopped = budget_stopped()
            except Exception:
                current = None
            self.spent.append((started, spent_requests() - before))
            if stopped:
                # The budget stopped this read. Keep the last good kept read, and end this pass.
                return
            with self.guard:
                if current is not None:
                    self.records[key] = (started, json.dumps(current))
                else:
                    # A failed read or another login keeps no old read: the next turn reads the thread itself.
                    self.records.pop(key, None)

    def _start(self):
        import threading
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='source-refresh', daemon=True)
            self.thread.start()

    def _run(self):
        import logging
        while not self.service.stopping.wait(THREAD_REFRESH_SECONDS):
            try:
                self.refresh()
            except Exception:
                logging.getLogger(__name__).exception('The refresh of GitHub thread reads failed. The next pass retries.')


# The decisions_task index uses the same expression. Keep the two the same.
# The reader stops once it has enough links, so the newest decisions come first.
TASK_DECISIONS = ("SELECT data FROM decisions WHERE json_extract(data,'$.task_id') IN ({}) "
                  "AND COALESCE(json_extract(data,'$._hidden'), 0) = 0 "
                  "AND COALESCE(json_extract(data,'$._deleted'), 0) = 0 ORDER BY rowid DESC")
TASK_DECISIONS_PAGE = TASK_DECISIONS.replace('SELECT data', 'SELECT rowid, data', 1).replace(
    ' ORDER BY rowid DESC', ' AND rowid < ? ORDER BY rowid DESC LIMIT 100')


def operator_owners(service):
    """Return the trusted GitHub owners and repositories, in lowercase.

    A repository role is trusted only in these repositories. They come from
    configured work-source assignments (the exact owner/repo) and the optional
    COCONUT_GITHUB_TRUSTED_OWNERS list, which accepts owner or owner/repo
    entries (comma-separated). A bot can track any PR, so tracked PRs do not
    make a repository trusted.
    """
    import json
    import os
    from .service import ServiceError
    from .work_sources import source_fingerprint
    owners = {owner.strip().lower() for owner in os.environ.get('COCONUT_GITHUB_TRUSTED_OWNERS', '').split(',') if owner.strip()}
    path = service.root / 'work-sources.json'
    try:
        sources = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(sources, list):
            sources = []
    except (OSError, ValueError):
        sources = []  # A broken configuration cannot add trust.
    with service.lock:
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get('id'), str):
                continue
            try:
                bot = service._get('bots', source.get('bot_id'))
            except (KeyError, ServiceError):
                continue
            if bot.get('_deleted') or bot.get('archived'):
                continue
            state = service._setting('work-source:' + source['id'], {})
            if isinstance(state, dict) and state.get('trusted_repositories_source') == source_fingerprint(source):
                owners.update(repo for repo in state.get('trusted_repositories', []) if isinstance(repo, str))
    return owners


def operator_repository(url, trusted):
    """Return True when the configured owners or repositories include the repository of `url`."""
    owner, repo = github_source(url)[1].split('/')[:2]
    return owner.lower() in trusted or f'{owner}/{repo}'.lower() in trusted


def activity_signature(item):
    """Fingerprint delivered metadata and bounded text, not untrusted raw input."""
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_source_activity(service, task_id, limit=3):
    """Read new activity on the task's GitHub sources before a turn starts.

    Return the activity for the prompt and the revisions to record. The GitHub
    reads run outside the service lock. A failed read never blocks the turn.
    """
    import json
    import time

    started = time.monotonic()
    with service.lock:
        task = service._get('tasks', task_id)
        assignment = service._assignment(task)
        identities = list(dict.fromkeys((task['id'], assignment['id'])))
        resumed = service._get('decisions', task['_resume_decision']) if task.get('_resume_decision') else None
    # Decode outside the lock and release it between bounded database pages.
    decisions, distinct = [], set()
    before = 9223372036854775807
    scanned = 0
    selection_limited = False
    while len(distinct) < limit:
        if scanned >= 1000 or time.monotonic() - started >= 1:
            selection_limited = True
            break
        with service.lock:
            rows = service.db.execute(TASK_DECISIONS_PAGE.format(','.join('?' for _ in identities)),
                                      (*identities, before)).fetchall()
        scanned += len(rows)
        for rowid, data in rows:
            decision = json.loads(data)
            if decision.get('_hidden') or decision.get('_deleted'):
                continue
            decisions.append(decision)
            distinct |= {_issue_url(url).lower() for url in github_links(decision)}
            if len(distinct) >= limit:
                break
        if len(rows) < 100:
            break
        before = rows[-1][0]
    if resumed:
        decisions = [resumed] + [d for d in decisions if d['id'] != resumed['id']]
    # Each candidate is a link and a known revision. The prompt shows only activity after the latest revision.
    candidates = []
    for record in (task, assignment):
        source = record.get('_source_assignment')
        if isinstance(source, dict) and github_source(source.get('id')):
            candidates.append((source['id'], source.get('updated_at')))
    for decision in decisions:
        # Use the revision that the bot proved when Coconut stored the card.
        # Do not use _source_facts. The background refresh changes it while the card waits.
        seen = decision.get('_source_seen', {})
        for url in github_links(decision):
            candidates.append((url, seen.get(url) or decision.get('created_at')))
    known, links = {}, {}
    for url, revision in candidates:
        key = _issue_url(url).lower()
        if key not in links and len(links) >= limit:
            continue
        links.setdefault(key, github_source(url)[2])
        known.setdefault(key, set()).add(_github_time(revision))
    if not links:
        return ([dict(selection_limited=True)] if selection_limited else []), {}
    operators = operator_owners(service)
    delivered = {}
    for record in (assignment, task):
        for key, revision in record.get('_source_activity_seen', {}).items():
            if key in known:
                known[key].add(_github_time(revision))
        for key, state in record.get('_source_activity_items_seen', {}).items():
            if key in known and isinstance(state, dict):
                delivered.setdefault(key, []).append(state)
    import time
    # One wall-clock budget covers the gh login and all reads. The turn waits no longer than this.
    deadline = time.monotonic() + SOURCE_READ_SECONDS
    from .pr_tracking import GitHubPRTracker
    try:
        tracker = service._pr_tracker_for(task['bot_id'])
    except Exception:
        tracker = None
    if isinstance(tracker, GitHubPRTracker) and not tracker._token:
        # Without a gh login, GitHub allows 60 requests an hour for all readers. Keep them for card checks and PR refresh.
        tracker = None
    records = service.__dict__.get('_thread_records')
    if records is None:
        records = service.__dict__.setdefault('_thread_records', ThreadRecords(service))

    def read(key, url):
        since = max((value for value in known[key] if value), default=None)
        if tracker is None or time.monotonic() >= deadline:
            return since, None
        operator = operator_repository(url, operators)
        delivered_signatures = {sig for state in delivered.get(key, []) for sig in state.get('signatures', [])
                                if isinstance(sig, str)}
        # The reason comes before the read: the refresher can change the kept read meanwhile.
        reason = records.miss_reason(tracker, url, operator, since)
        kept = records.kept(tracker, task['bot_id'], url, operator, since, delivered_signatures)
        if kept is not None:
            outcomes[key] = ('kept', 0)
            return since, kept
        started = time.monotonic()
        try:
            options = dict(operator=operator)
            if isinstance(tracker, GitHubPRTracker):
                options['include_same_second'] = True
            current = tracker.observe_issue_activity(url, since or '1970-01-01T00:00:00Z', deadline, **options)
        except Exception:
            current = None
        outcomes[key] = (reason, round((time.monotonic() - started) * 1000))
        if since is not None and isinstance(current, dict) and current.get('updated_at') == since:
            # A read at the revision second is the read that the refresher keeps. A later turn of this thread can use it.
            records.keep(tracker, task['bot_id'], url, operator, current, started)
        return since, current

    outcomes = {}
    # Each read takes about 0.6 s on the Mac mini, and the turn waits for all of them. Read the threads at once.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(links), thread_name_prefix='source-read') as pool:
        reads = {key: pool.submit(read, key, url) for key, url in links.items()}
    activity, seen = [], {}
    for key, url in links.items():
        since, current = reads[key].result()
        if not isinstance(current, dict) or not isinstance(current.get('updated_at'), str):
            activity.append(dict(url=url, read_failed=True))
            continue
        observed = current.get('observed_at')
        seen[key] = observed if isinstance(observed, str) and observed > current['updated_at'] else current['updated_at']
        previous = {sig for state in delivered.get(key, []) for sig in state.get('signatures', []) if isinstance(sig, str)}
        current['new_activity'] = [item for item in current.get('new_activity', [])
                                   if activity_signature(item) not in previous]
        # Items at the recorded second can be new although the thread time did not move.
        if (since is None or current['updated_at'] > since or current.get('new_activity')
                or current.get('incomplete') or current.get('changed_without_new_activity')):
            activity.append(dict(url=url, since=since, **{k: current.get(k) for k in (
                'state', 'state_reason', 'title', 'title_withheld', 'updated_at', 'new_activity', 'omitted_items', 'incomplete',
                'changed_without_new_activity') if k in current}))
    if selection_limited:
        activity.append(dict(selection_limited=True))
    # How each thread was read, without its address or its content: kept, or the reason a kept read did not apply.
    import logging
    logging.getLogger(__name__).warning('coconut.source_read %s', json.dumps(dict(
        task_id=task_id, threads=len(links), outcomes=sorted(outcomes.values()), **records.state())))
    return activity, seen
