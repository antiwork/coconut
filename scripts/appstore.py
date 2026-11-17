#!/usr/bin/env python3
"""The App Store listing of the iPhone app, and its submission to App Review.

  appstore.py status  --key AuthKey.p8 --key-id KEY --issuer ISSUER
  appstore.py prepare --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]
  appstore.py submit  --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]

status only reads App Store Connect, and prints what a submission still needs. prepare writes the listing of
app-store/ (the text and the screenshots), the age rating, the price, the availability and the review details, and
picks the build. submit prepares, then sends the version to App Review.

The logs of this repository are public, so the script never prints personal data such as the review contact.
The script uses only the standard library and the openssl command, like testflight.py.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testflight  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP_ID = testflight.APP_ID
HOST = 'https://api.appstoreconnect.apple.com'


class Api:
    """App Store Connect with a token that renews itself before Apple's 20 minutes run out."""

    def __init__(self, key: Path, key_id: str, issuer: str):
        self.key, self.key_id, self.issuer = key, key_id, issuer
        self.bearer, self.made = '', 0.0

    def token(self) -> str:
        import time
        if time.time() - self.made > 15 * 60:
            self.bearer, self.made = testflight.token(self.key, self.key_id, self.issuer), time.time()
        return self.bearer

    def call(self, method: str, path: str, body: dict | None = None, quiet: bool = False) -> tuple[int, dict]:
        url = path if path.startswith('http') else HOST + (path if path.startswith('/v') else '/v1' + path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Authorization': 'Bearer ' + self.token(), 'Content-Type': 'application/json'}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                text = response.read()
                return response.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as error:
            text = error.read()
            try:
                document = json.loads(text) if text else {}
            except ValueError:
                document = {'raw': text[:400].decode(errors='replace')}
            if not quiet:
                print(f'  {method} {url.removeprefix(HOST)} -> {error.code}: {problems(document)}')
            return error.code, document

    def get(self, path: str, quiet: bool = False) -> dict:
        status, document = self.call('GET', path, quiet=quiet)
        return document if status == 200 else {}

    def all(self, path: str) -> list[dict]:
        items, following = [], path
        while following:
            document = self.get(following)
            items.extend(document.get('data', []))
            following = document.get('links', {}).get('next', '')
        return items


def problems(document: dict) -> str:
    errors = document.get('errors') or []
    if not errors:
        return json.dumps(document)[:400]
    parts = []
    for error in errors:
        detail = error.get('detail') or error.get('title') or ''
        pointer = (error.get('source') or {}).get('pointer', '')
        parts.append(f"{error.get('code', '')} {detail} {pointer}".strip())
        for associated in ((error.get('meta') or {}).get('associatedErrors') or {}).values():
            for item in associated:
                parts.append('    ' + f"{item.get('code', '')} {item.get('detail', '')}".strip())
    return '\n    '.join(parts)


def query(**values) -> str:
    return '?' + urllib.parse.urlencode(values)


def filled(attributes: dict, names: list[str]) -> str:
    return ', '.join(f"{name}={'set' if attributes.get(name) not in (None, '', []) else 'missing'}" for name in names)


def status(api: Api) -> None:
    app = api.get(f'/apps/{APP_ID}')
    attributes = app.get('data', {}).get('attributes', {})
    print('App:', {key: attributes.get(key) for key in ('name', 'bundleId', 'sku', 'primaryLocale',
                                                         'contentRightsDeclaration', 'isOrEverWasMadeForKids')})

    print('\nOther apps of the team:')
    for other in api.all('/apps' + query(limit=200, **{'fields[apps]': 'name,bundleId'})):
        print('  ', other['id'], other['attributes'].get('name'), other['attributes'].get('bundleId'))

    print('\nApp infos:')
    for info in api.all(f'/apps/{APP_ID}/appInfos' + query(include='primaryCategory,secondaryCategory')):
        values = info['attributes']
        relations = info.get('relationships', {})
        primary = (relations.get('primaryCategory') or {}).get('data') or {}
        secondary = (relations.get('secondaryCategory') or {}).get('data') or {}
        print('  ', info['id'], {key: values.get(key) for key in ('state', 'appStoreState', 'appStoreAgeRating',
                                                                    'kidsAgeBand')},
              'primary:', primary.get('id'), 'secondary:', secondary.get('id'))
        for localization in api.all(f"/appInfos/{info['id']}/appInfoLocalizations"):
            values = localization['attributes']
            print('     ', values.get('locale'), {key: values.get(key) for key in ('name', 'subtitle', 'privacyPolicyUrl',
                                                                                    'privacyChoicesUrl')})
        rating = api.get(f"/appInfos/{info['id']}/ageRatingDeclaration")
        if rating:
            print('      age rating', rating['data']['id'], json.dumps(rating['data']['attributes'], sort_keys=True))

    print('\nApp Store versions:')
    for version in api.all(f'/apps/{APP_ID}/appStoreVersions' + query(include='build')):
        values = version['attributes']
        build = ((version.get('relationships', {}).get('build') or {}).get('data') or {}).get('id')
        print('  ', version['id'], {key: values.get(key) for key in ('platform', 'versionString', 'appStoreState',
                                                                       'appVersionState', 'releaseType', 'copyright',
                                                                       'createdDate')}, 'build:', build)
        for localization in api.all(f"/appStoreVersions/{version['id']}/appStoreVersionLocalizations"):
            values = localization['attributes']
            print('     ', localization['id'], values.get('locale'),
                  {key: (len(values.get(key) or '') if key in ('description', 'keywords', 'promotionalText', 'whatsNew')
                         else values.get(key)) for key in ('description', 'keywords', 'promotionalText', 'whatsNew',
                                                          'supportUrl', 'marketingUrl')})
            for screenshot_set in api.all(f"/appStoreVersionLocalizations/{localization['id']}/appScreenshotSets"):
                shots = api.all(f"/appScreenshotSets/{screenshot_set['id']}/appScreenshots")
                print('        screenshots', screenshot_set['attributes'].get('screenshotDisplayType'), len(shots))
        detail = api.get(f"/appStoreVersions/{version['id']}/appStoreReviewDetail", quiet=True)
        if detail.get('data'):
            print('      review detail', filled(detail['data']['attributes'], [
                'contactFirstName', 'contactLastName', 'contactPhone', 'contactEmail', 'notes']),
                  'demoAccountRequired=', detail['data']['attributes'].get('demoAccountRequired'))
        else:
            print('      review detail missing')

    print('\nNewest builds:')
    fields = 'version,uploadedDate,processingState,expired,buildAudienceType,usesNonExemptEncryption,minOsVersion'
    for build in api.get('/builds' + query(**{'filter[app]': APP_ID, 'sort': '-uploadedDate', 'limit': 8,
                                             'fields[builds]': fields, 'include': 'preReleaseVersion'})).get('data', []):
        values = build['attributes']
        print('  ', build['id'], {key: values.get(key) for key in fields.split(',')})

    print('\nPrice schedule:')
    schedule = api.get(f'/apps/{APP_ID}/appPriceSchedule' + query(include='manualPrices,baseTerritory'), quiet=True)
    print('  ', 'set' if schedule.get('data') else 'missing',
          [item.get('type') + ':' + item.get('id', '')[:40] for item in schedule.get('included', [])][:4])

    print('\nAvailability:')
    status_code, availability = api.call('GET', f'/apps/{APP_ID}/appAvailabilityV2', quiet=True)
    if status_code == 200 and availability.get('data'):
        identifier = availability['data']['id']
        territories = api.all(f'/v2/appAvailabilities/{identifier}/territoryAvailabilities' + query(limit=200))
        available = [item for item in territories if item['attributes'].get('available')]
        print('  ', availability['data']['attributes'], f'{len(available)} of {len(territories)} territories available')
    else:
        print('   missing', status_code)

    print('\nTestFlight review details:')
    beta = api.get(f'/apps/{APP_ID}/betaAppReviewDetail', quiet=True)
    if beta.get('data'):
        print('  ', filled(beta['data']['attributes'], ['contactFirstName', 'contactLastName', 'contactPhone',
                                                        'contactEmail', 'notes', 'demoAccountName']))
    for localization in api.all(f'/apps/{APP_ID}/betaAppLocalizations'):
        values = localization['attributes']
        print('  ', values.get('locale'), filled(values, ['description', 'feedbackEmail', 'privacyPolicyUrl',
                                                          'marketingUrl']))

    print('\nReview submissions:')
    for submission in api.all('/reviewSubmissions' + query(**{'filter[app]': APP_ID, 'limit': 20})):
        print('  ', submission['id'], submission['attributes'])

    print('\nApp privacy (not in the public API, so a status code only):')
    for path in (f'/apps/{APP_ID}/appDataUsages', f'/apps/{APP_ID}/dataUsages', '/appDataUsages' +
                 query(**{'filter[app]': APP_ID})):
        code, _ = api.call('GET', path, quiet=True)
        print('  ', path, code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['status'])
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--issuer', required=True)
    parser.add_argument('--build', type=int, help='The build number. Without it: the newest build App Review takes.')
    args = parser.parse_args()
    api = Api(args.key, args.key_id, args.issuer)
    if args.action == 'status':
        status(api)


if __name__ == '__main__':
    main()
