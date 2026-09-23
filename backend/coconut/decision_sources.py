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
    """Check the source version before storing a card or asking for attention."""
    from .service import ServiceError

    decision = {'merge_pr_url': payload.get('merge_pr_url')}
    attach_sources(decision, payload)
    links = github_links(decision)
    if not links:
        return
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
    return facts
