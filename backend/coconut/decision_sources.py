"""Keep the sources for a decision visible in chat and Inbox."""
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
            facts[url] = current
    return facts, {url: revisions[url] for url in links}


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
# The decisions_task index uses the same expression. Keep the two the same.
# Only the first three links matter, so the newest twenty decisions are sufficient.
TASK_DECISIONS = "SELECT data FROM decisions WHERE json_extract(data,'$.task_id') IN ({}) ORDER BY rowid DESC LIMIT 20"


def read_source_activity(service, task_id, limit=3):
    """Read new activity on the task's GitHub sources before a turn starts.

    Return the activity for the prompt and the revisions to record. The GitHub
    reads run outside the service lock. A failed read never blocks the turn.
    """
    import json

    with service.lock:
        task = service._get('tasks', task_id)
        assignment = service._assignment(task)
        identities = list(dict.fromkeys((task['id'], assignment['id'])))
        rows = service.db.execute(TASK_DECISIONS.format(','.join('?' for _ in identities)), identities)
        decisions = [d for d in (json.loads(row[0]) for row in rows) if not d.get('_hidden') and not d.get('_deleted')]
        if task.get('_resume_decision'):
            resumed = service._get('decisions', task['_resume_decision'])
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
        return [], {}
    for record in (assignment, task):
        for key, revision in record.get('_source_activity_seen', {}).items():
            if key in known:
                known[key].add(_github_time(revision))
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
    activity, seen = [], {}
    for key, url in links.items():
        since = max((value for value in known[key] if value), default=None)
        current = None
        if tracker is not None and time.monotonic() < deadline:
            try:
                current = tracker.observe_issue_activity(url, since or '1970-01-01T00:00:00Z', deadline)
            except Exception:
                current = None
        if not isinstance(current, dict) or not isinstance(current.get('updated_at'), str):
            activity.append(dict(url=url, read_failed=True))
            continue
        observed = current.get('observed_at')
        seen[key] = observed if isinstance(observed, str) and observed > current['updated_at'] else current['updated_at']
        if since is None or current['updated_at'] > since:
            activity.append(dict(url=url, since=since, **{k: current.get(k) for k in (
                'state', 'state_reason', 'title', 'title_withheld', 'updated_at', 'new_activity', 'omitted_items', 'incomplete',
                'changed_without_new_activity') if k in current}))
    return activity, seen
