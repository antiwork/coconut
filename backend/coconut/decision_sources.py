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
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/'
        r'([A-Za-z0-9_.-]{1,100})/(issues|pull)/([1-9][0-9]{0,9})/?(?:#[^\s]*)?', url or '')
    if not match or match[2] in ('.', '..'):
        return None
    kind = 'issues' if match[3] == 'issues' else 'pulls'
    return kind, '/'.join((match[1], match[2], kind, match[4])), f'https://github.com/{match[1]}/{match[2]}/{match[3]}/{match[4]}'


def github_links(decision):
    values = [s.get('url') for s in decision.get('sources', [])] + [decision.get('merge_pr_url')]
    return sorted({source[2] for value in values if (source := github_source(value))})
