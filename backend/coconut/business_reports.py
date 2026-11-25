"""Read published business reports and canonical Beancount files from GitHub only.

The dashboards repository remains the publisher until ingestion and visual parity are verified.
No financial calculation or customer operation is duplicated in the client.
"""
import base64
import binascii
import json
import os
from pathlib import PurePosixPath
from urllib.parse import quote

from .business import key
from .service_base import ServiceError

REPORTS = {'overview': 'home', 'accounting': 'home', 'support': 'support', 'risk': 'risk', 'engineering': 'prs',
           'periods': 'periods'}
# The folder of the published reports in their repository. A new publish is a new commit there (business_watch.py).
FOLDER = 'docs/0825'


def reports_repository():
    """The repository that publishes the reports: dashboards, or COCONUT_BUSINESS_REPORTS_REPO."""
    return key(os.environ.get('COCONUT_BUSINESS_REPORTS_REPO', 'dashboards'))


def contents(business, repository, path):
    result = business._call('GET', business._repo_path(repository) + '/contents/' + quote(path, safe='/'))
    if not isinstance(result, dict) or result.get('encoding') != 'base64' or not result.get('sha'):
        raise ServiceError(502, 'GitHub did not return this report. Refresh and try again.')
    try:
        content = base64.b64decode(result['content']).decode('utf-8')
    except (KeyError, ValueError, UnicodeError, binascii.Error):
        raise ServiceError(502, 'This GitHub file is not readable text.') from None
    return content, result


def report(business, name):
    if name not in REPORTS:
        raise ServiceError(404, 'This report is unavailable.')
    repository = reports_repository()
    path = FOLDER + '/' + REPORTS[name] + '.json'
    text, source = contents(business, repository, path)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError()
    except ValueError:
        raise ServiceError(502, 'The published report is not valid JSON.') from None
    return dict(key=name, data=data, source=dict(repository=business.owner + '/' + repository, path=path,
                sha=source['sha'], url=source['html_url']),
                generated_at=data.get('generated_at') or data.get('generatedAt'),
                ledger_url=f'https://github.com/{business.owner}/accounting/blob/main/main.beancount')


def ledger(business, path=None):
    if path is not None:
        parts = PurePosixPath(path).parts
        if not path.endswith('.beancount') or not parts or '..' in parts or path.startswith('/') or '\\' in path:
            raise ServiceError(400, 'Choose a Beancount file from Accounting.')
        text, source = contents(business, 'accounting', path)
        return dict(path=path, content=text, url=source['html_url'], sha=source['sha'])
    # The recursive tree of the whole Accounting repository holds every receipt in records/ and passes the 2 MB limit
    # of a GitHub response. The index reads the top level, then each folder on its own. A folder that is too large to
    # list, or that GitHub truncates, is named in `skipped` instead of failing the whole index.
    repository = business._repo('accounting')
    branch = repository['default_branch']
    top = business._call('GET', business._repo_path('accounting') + '/git/trees/' + quote(branch, safe=''))
    files, skipped = [], []
    for item in top.get('tree', []):
        if item.get('type') == 'blob' and item['path'].endswith('.beancount'):
            files.append(dict(path=item['path'], sha=item['sha']))
        elif item.get('type') == 'tree':
            try:
                tree = business._call('GET', business._repo_path('accounting') + '/git/trees/'
                                      + quote(item['sha'], safe='') + '?recursive=1')
            except ServiceError as error:
                if error.status != 502:
                    raise
                skipped.append(item['path'])
                continue
            if tree.get('truncated'):
                skipped.append(item['path'])
            files += [dict(path=item['path'] + '/' + entry['path'], sha=entry['sha']) for entry in tree.get('tree', [])
                      if entry.get('type') == 'blob' and entry['path'].endswith('.beancount')]
    return dict(files=sorted(files, key=lambda item: (item['path'] != 'main.beancount', item['path'])), skipped=skipped)
