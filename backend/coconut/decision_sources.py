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
