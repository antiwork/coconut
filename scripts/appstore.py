#!/usr/bin/env python3
"""The App Store version of the iPhone app, and its submission to App Review.

  appstore.py status --key AuthKey.p8 --key-id KEY --issuer ISSUER
  appstore.py submit --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]

status only reads App Store Connect: the listing, the build, the age rating, the price, the availability, the review
details and the submissions. submit gives the iOS version its build (the newest one that App Review takes, unless
--build names one), keeps the app out of China mainland, and sends the version to App Review. Apple releases it after
approval. The listing itself (text, screenshots, review notes) lives in App Store Connect.

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


def redact(text: str) -> str:
    """Text without e-mail addresses and phone numbers, for a public log."""
    import re
    text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '<e-mail>', text)
    return re.sub(r'\+?\d[\d ().-]{6,}\d', '<number>', text)


def query(**values) -> str:
    return '?' + urllib.parse.urlencode(values)


def filled(attributes: dict, names: list[str]) -> str:
    return ', '.join(f"{name}={'set' if attributes.get(name) not in (None, '', []) else 'missing'}" for name in names)


def status(api: Api) -> None:
    app = api.get(f'/apps/{APP_ID}')
    attributes = app.get('data', {}).get('attributes', {})
    print('App:', {key: attributes.get(key) for key in ('name', 'bundleId', 'sku', 'primaryLocale',
                                                         'contentRightsDeclaration', 'isOrEverWasMadeForKids')})

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
                  {key: values.get(key) for key in ('supportUrl', 'marketingUrl')})
            for key in ('promotionalText', 'keywords', 'description', 'whatsNew'):
                print(f'        {key} ({len(values.get(key) or "")}):', values.get(key))
            for screenshot_set in api.all(f"/appStoreVersionLocalizations/{localization['id']}/appScreenshotSets"):
                shots = api.all(f"/appScreenshotSets/{screenshot_set['id']}/appScreenshots")
                print('        screenshots', screenshot_set['attributes'].get('screenshotDisplayType'), len(shots))
                for shot in shots:
                    asset = shot['attributes'].get('imageAsset') or {}
                    state = (shot['attributes'].get('assetDeliveryState') or {}).get('state')
                    template = asset.get('templateUrl') or ''
                    print('          ', state, asset.get('width'), asset.get('height'),
                          template.replace('{w}', str(asset.get('width'))).replace('{h}', str(asset.get('height')))
                          .replace('{f}', 'png'))
        detail = api.get(f"/appStoreVersions/{version['id']}/appStoreReviewDetail", quiet=True)
        if detail.get('data'):
            values = detail['data']['attributes']
            print('      review detail', filled(values, [
                'contactFirstName', 'contactLastName', 'contactPhone', 'contactEmail', 'notes']),
                  'demoAccountRequired=', values.get('demoAccountRequired'))
            print('      review notes:', redact(values.get('notes') or ''))
        else:
            print('      review detail missing')

    print('\nNewest builds:')
    fields = 'version,uploadedDate,processingState,expired,buildAudienceType,usesNonExemptEncryption,minOsVersion'
    document = api.get('/builds' + query(**{'filter[app]': APP_ID, 'sort': '-uploadedDate', 'limit': 4,
                                           'fields[builds]': fields + ',preReleaseVersion',
                                           'include': 'preReleaseVersion'}))
    trains = {item['id']: item['attributes'].get('version') for item in document.get('included', [])
              if item['type'] == 'preReleaseVersions'}
    for build in document.get('data', []):
        values = build['attributes']
        train = ((build.get('relationships', {}).get('preReleaseVersion') or {}).get('data') or {}).get('id')
        print('  ', build['id'], 'version', trains.get(train), {key: values.get(key) for key in fields.split(',')})

    print('\nPrice schedule:')
    schedule = api.get(f'/apps/{APP_ID}/appPriceSchedule', quiet=True)
    if schedule.get('data'):
        prices = api.get(f"/appPriceSchedules/{schedule['data']['id']}/manualPrices"
                         + query(include='appPricePoint,territory', **{'fields[appPricePoints]': 'customerPrice'}))
        points = [item['attributes'].get('customerPrice') for item in prices.get('included', [])
                  if item['type'] == 'appPricePoints']
        territories = [item['id'] for item in prices.get('included', []) if item['type'] == 'territories']
        print('   set, customer price', points, 'in', territories)
    else:
        print('   missing')

    print('\nAvailability:')
    status_code, availability = api.call('GET', f'/apps/{APP_ID}/appAvailabilityV2', quiet=True)
    if status_code == 200 and availability.get('data'):
        identifier = availability['data']['id']
        territories = api.all(f'/v2/appAvailabilities/{identifier}/territoryAvailabilities'
                              + query(limit=200, include='territory'))
        available = [item for item in territories if item['attributes'].get('available')]
        print('  ', availability['data']['attributes'], f'{len(available)} of {len(territories)} territories available')
        missing = [related(item, 'territory') or item['id'][:12] for item in territories
                   if not item['attributes'].get('available')]
        print('   not available in:', missing)
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
        for item in api.all(f"/reviewSubmissions/{submission['id']}/items" + query(include='appStoreVersion')):
            relations = {name: (value.get('data') or {}).get('id') for name, value in item.get('relationships', {}).items()
                         if isinstance(value, dict) and value.get('data')}
            print('     item', item['id'][:12], item['attributes'], relations)

    print('\nApp privacy (not in the public API, so a status code only):')
    for path in (f'/apps/{APP_ID}/appDataUsages', f'/apps/{APP_ID}/dataUsages', '/appDataUsages' +
                 query(**{'filter[app]': APP_ID})):
        code, _ = api.call('GET', path, quiet=True)
        print('  ', path, code)


# The states of a version that can still change and go to App Review.
EDITABLE = ('PREPARE_FOR_SUBMISSION', 'DEVELOPER_REJECTED', 'REJECTED', 'METADATA_REJECTED', 'INVALID_BINARY')
# The states of a version that App Review has or that waits for it.
SENT = ('WAITING_FOR_REVIEW', 'IN_REVIEW', 'PENDING_DEVELOPER_RELEASE', 'PENDING_APPLE_RELEASE',
        'PROCESSING_FOR_DISTRIBUTION', 'READY_FOR_DISTRIBUTION', 'ACCEPTED')
# China mainland needs an ICP filing number and a licence for generative AI. Coconut has neither, so App Review would
# refuse the whole version.
LEFT_OUT = ('CHN',)


def related(item: dict, name: str) -> str | None:
    return ((item.get('relationships', {}).get(name) or {}).get('data') or {}).get('id')


def review_build(api: Api, number: int | None) -> tuple[dict, str]:
    """The build for App Review and its version: the given build number, else the newest valid build that App Review
    takes. A TestFlight run without `public` makes an internal-only build, which App Review never takes."""
    filters = {'filter[app]': APP_ID, 'sort': '-uploadedDate', 'limit': 50, 'include': 'preReleaseVersion',
               'fields[builds]': 'version,processingState,expired,buildAudienceType,preReleaseVersion'}
    if number:
        filters['filter[version]'] = str(number)
    document = api.get('/builds' + query(**filters))
    trains = {item['id']: item['attributes'].get('version') for item in document.get('included', [])
              if item['type'] == 'preReleaseVersions'}
    for build in document.get('data', []):
        values = build['attributes']
        if values.get('processingState') == 'VALID' and not values.get('expired') \
                and values.get('buildAudienceType') == 'APP_STORE_ELIGIBLE':
            return build, trains.get(related(build, 'preReleaseVersion'), '')
    raise SystemExit(f'No valid build {number or ""} that App Review takes. Upload one with a TestFlight run with public.')


def leave_out_territories(api: Api) -> None:
    code, availability = api.call('GET', f'/apps/{APP_ID}/appAvailabilityV2')
    if code != 200:
        raise SystemExit('App Store Connect has no availability for the app. Set it before App Review.')
    identifier = availability['data']['id']
    path = f'/v2/appAvailabilities/{identifier}/territoryAvailabilities' + query(limit=200, include='territory')
    for item in api.all(path):
        territory = related(item, 'territory')
        if territory not in LEFT_OUT or not item['attributes'].get('available'):
            continue
        code, _ = api.call('PATCH', f"/territoryAvailabilities/{item['id']}", {'data': {
            'type': 'territoryAvailabilities', 'id': item['id'], 'attributes': {'available': False}}})
        if code != 200:
            raise SystemExit(f'App Store Connect kept the app in {territory}. Read the error above.')
        print(f'The app is not available in {territory} now.')


def submit(api: Api, number: int | None) -> None:
    """Send the iOS version to App Review with the build, and let Apple release it after approval."""
    versions = api.all(f'/apps/{APP_ID}/appStoreVersions' + query(**{'filter[platform]': 'IOS', 'include': 'build'}))
    state = lambda version: version['attributes'].get('appVersionState') or version['attributes'].get('appStoreState')
    waiting = [version for version in versions if state(version) in SENT[:2]]
    if waiting:
        print(f"Version {waiting[0]['attributes']['versionString']} is {state(waiting[0])} already.")
        return
    version = next((version for version in versions if state(version) in EDITABLE), None)
    if not version:
        raise SystemExit('No iOS version can go to App Review. Make one in App Store Connect first.')
    build, train = review_build(api, number)
    if train and version['attributes'].get('versionString') != train:
        # A version takes only a build of the same version: the UTC date of its TestFlight run.
        code, _ = api.call('PATCH', f"/appStoreVersions/{version['id']}", {'data': {
            'type': 'appStoreVersions', 'id': version['id'], 'attributes': {'versionString': train}}})
        if code != 200:
            raise SystemExit('App Store Connect kept the version number. Read the error above.')
        print(f"Version {version['attributes'].get('versionString')} is version {train} now, like its build.")
    if related(version, 'build') != build['id']:
        code, _ = api.call('PATCH', f"/appStoreVersions/{version['id']}/relationships/build",
                           {'data': {'type': 'builds', 'id': build['id']}})
        if code != 204:
            raise SystemExit('App Store Connect did not take the build. Read the error above.')
    print(f"Version {train} has build {build['attributes']['version']}.")
    leave_out_territories(api)

    submissions = api.all('/reviewSubmissions' + query(**{'filter[app]': APP_ID, 'filter[platform]': 'IOS'}))
    submission = next((item for item in submissions if item['attributes'].get('state') == 'READY_FOR_REVIEW'), None)
    if not submission:
        code, document = api.call('POST', '/reviewSubmissions', {'data': {
            'type': 'reviewSubmissions', 'attributes': {'platform': 'IOS'},
            'relationships': {'app': {'data': {'type': 'apps', 'id': APP_ID}}}}})
        if code != 201:
            raise SystemExit('App Store Connect did not start a submission. Read the error above.')
        submission = document['data']
    items = api.all(f"/reviewSubmissions/{submission['id']}/items" + query(include='appStoreVersion'))
    if not any(related(item, 'appStoreVersion') == version['id'] for item in items):
        code, _ = api.call('POST', '/reviewSubmissionItems', {'data': {
            'type': 'reviewSubmissionItems', 'relationships': {
                'reviewSubmission': {'data': {'type': 'reviewSubmissions', 'id': submission['id']}},
                'appStoreVersion': {'data': {'type': 'appStoreVersions', 'id': version['id']}}}}})
        if code != 201:
            raise SystemExit('App Store Connect did not add the version to the submission. Read the errors above.')
    code, document = api.call('PATCH', f"/reviewSubmissions/{submission['id']}", {'data': {
        'type': 'reviewSubmissions', 'id': submission['id'], 'attributes': {'submitted': True}}})
    if code != 200:
        raise SystemExit('App Store Connect did not send the version to App Review. Read the errors above.')
    attributes = document['data']['attributes']
    print(f"Sent version {train} ({build['attributes']['version']}) to App Review: {attributes.get('state')}, "
          f"{attributes.get('submittedDate')}. Apple releases it after approval.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['status', 'submit'])
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--issuer', required=True)
    parser.add_argument('--build', type=int, help='The build number. Without it: the newest build App Review takes.')
    args = parser.parse_args()
    api = Api(args.key, args.key_id, args.issuer)
    if args.action == 'status':
        status(api)
    elif args.action == 'submit':
        submit(api, args.build)


if __name__ == '__main__':
    main()
