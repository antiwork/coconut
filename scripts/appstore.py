#!/usr/bin/env python3
"""The App Store version of the iPhone app, and its submission to App Review.

  appstore.py status            --key AuthKey.p8 --key-id KEY --issuer ISSUER
  appstore.py screenshots       --key AuthKey.p8 --key-id KEY --issuer ISSUER
  appstore.py submit            --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]
  appstore.py submit-when-ready --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]
  appstore.py public-testflight --key AuthKey.p8 --key-id KEY --issuer ISSUER [--build 315]
  appstore.py rename            --key AuthKey.p8 --key-id KEY --issuer ISSUER --name Coconut

status only reads App Store Connect: the listing, the build, the age rating, the price, the availability, the review
details, the TestFlight groups with their public link and beta review, and the submissions. screenshots puts the
images of app-store/screenshots into the listing, in place of the ones it has. submit gives the iOS version its build
(the newest one that App Review takes, unless --build names one),
keeps the app out of China mainland, and sends the version to App Review. Apple releases it after approval. The rest
of the listing (text, review notes, age rating) lives in App Store Connect. submit-when-ready does the same, but ends
without an error while the App Privacy answers are unpublished, so a routine can run it until they are.
public-testflight waits until Apple has processed the build, puts it into the public TestFlight group, and sends it
to TestFlight App Review. The TestFlight workflow runs it after each public upload.
rename gives the app a new name in each language of its listing. TestFlight shows the same name.

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


# App Review refuses a version until a signed-in person publishes the App Privacy answers on the App Store Connect
# website. The API cannot read or write them.
PRIVACY_REQUIRED = 'STATE_ERROR.APP_DATA_USAGES_REQUIRED'


def associated_codes(document: dict) -> set[str]:
    """The codes behind a refused request: the associated errors of each error, else the error itself."""
    codes = set()
    for error in document.get('errors') or []:
        associated = [item for items in ((error.get('meta') or {}).get('associatedErrors') or {}).values()
                      for item in items]
        if associated:
            codes.update(item.get('code', '') for item in associated)
        else:
            codes.add(error.get('code', ''))
    return codes


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

    print('\nTestFlight groups:')
    link = ['publicLinkEnabled', 'publicLink', 'publicLinkLimitEnabled', 'publicLinkLimit']
    for group in api.all(f'/apps/{APP_ID}/betaGroups' + query(**{
            'fields[betaGroups]': ','.join(['name', 'isInternalGroup'] + link), 'limit': 50})):
        values = group['attributes']
        external = not values.get('isInternalGroup')
        # A count only: the testers of a group are people, and this log is public.
        testers = api.get(f"/betaGroups/{group['id']}/relationships/betaTesters" + query(limit=1), quiet=True)
        print('  ', values.get('name'), 'external' if external else 'internal',
              {key: values.get(key) for key in link} if external else '',
              'testers:', (testers.get('meta') or {}).get('paging', {}).get('total'))
        if not external:
            continue
        # An external tester gets a build only after Apple's beta review approves it.
        for build in api.all(f"/betaGroups/{group['id']}/builds" + query(**{'fields[builds]': 'version,expired',
                                                                            'limit': 50})):
            review = (api.get(f"/builds/{build['id']}/betaAppReviewSubmission", quiet=True).get('data') or {})
            detail = (api.get(f"/builds/{build['id']}/buildBetaDetail", quiet=True).get('data') or {})
            print('      build', build['attributes'].get('version'), 'expired' if build['attributes'].get('expired')
                  else '', 'beta review:', (review.get('attributes') or {}).get('betaReviewState'),
                  (review.get('attributes') or {}).get('submittedDate'),
                  'external state:', (detail.get('attributes') or {}).get('externalBuildState'))

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
IN_REVIEW = ('WAITING_FOR_REVIEW', 'IN_REVIEW')
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


def version_state(version: dict) -> str:
    return version['attributes'].get('appVersionState') or version['attributes'].get('appStoreState') or ''


def editable_version(api: Api) -> dict:
    """The iOS version that can still change and go to App Review."""
    versions = api.all(f'/apps/{APP_ID}/appStoreVersions' + query(**{'filter[platform]': 'IOS', 'include': 'build'}))
    version = next((version for version in versions if version_state(version) in EDITABLE), None)
    if not version:
        states = ', '.join(f"{item['attributes'].get('versionString')} {version_state(item)}" for item in versions)
        raise SystemExit(f'No iOS version can change now ({states}). Make one in App Store Connect first.')
    return version


def submit(api: Api, number: int | None, wait: bool = False) -> None:
    """Send the iOS version to App Review with the build, and let Apple release it after approval. With `wait`, a version
    that only lacks published App Privacy answers ends the run without an error, so a routine can try again later."""
    versions = api.all(f'/apps/{APP_ID}/appStoreVersions' + query(**{'filter[platform]': 'IOS'}))
    waiting = [version for version in versions if version_state(version) in IN_REVIEW]
    if waiting:
        print(f"Version {waiting[0]['attributes']['versionString']} is {version_state(waiting[0])} already.")
        return
    version = editable_version(api)
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
        code, document = api.call('POST', '/reviewSubmissionItems', {'data': {
            'type': 'reviewSubmissionItems', 'relationships': {
                'reviewSubmission': {'data': {'type': 'reviewSubmissions', 'id': submission['id']}},
                'appStoreVersion': {'data': {'type': 'appStoreVersions', 'id': version['id']}}}}})
        if code == 409 and wait and associated_codes(document) == {PRIVACY_REQUIRED}:
            print('Waiting: the App Privacy answers are not published yet. The next run tries again.')
            return
        if code != 201:
            raise SystemExit('App Store Connect did not add the version to the submission. Read the errors above.')
    code, document = api.call('PATCH', f"/reviewSubmissions/{submission['id']}", {'data': {
        'type': 'reviewSubmissions', 'id': submission['id'], 'attributes': {'submitted': True}}})
    if code != 200:
        raise SystemExit('App Store Connect did not send the version to App Review. Read the errors above.')
    attributes = document['data']['attributes']
    print(f"Sent version {train} ({build['attributes']['version']}) to App Review: {attributes.get('state')}, "
          f"{attributes.get('submittedDate')}. Apple releases it after approval.")


# The external TestFlight group "Public". Anyone can join it through https://testflight.apple.com/join/BftxmcBQ, and
# its testers get each build of the group that TestFlight App Review approved.
PUBLIC_GROUP = '5415faea-f406-477b-be44-5b3d068355a8'
# Apple processes an upload for 5 to 20 minutes before a group or a review takes it.
PROCESSING_MINUTES = 60
# Apple takes no new submission while another build of the version waits for TestFlight App Review. It answers 422
# with this error code and "Another build in the same train is already in beta review". The text changes, so the code
# decides: a match on older text failed the run of build 321.
ANOTHER_IN_REVIEW = 'ANOTHER_BUILD_IN_REVIEW'


def another_in_review(document: dict) -> bool:
    """True when App Store Connect refused a TestFlight App Review submission because another build waits for one."""
    return any(ANOTHER_IN_REVIEW in (error.get('code') or '') for error in document.get('errors') or [])


def patient(api: Api, method: str, path: str, body: dict | None = None, quiet: bool = False) -> tuple[int, dict]:
    """A call that tries again, with a new token, when App Store Connect refuses it with a 401 NOT_AUTHORIZED or a
    server error. Both come and go during an outage at Apple, and a later try often passes."""
    import time
    code, document = api.call(method, path, body, quiet=quiet)
    for pause in (30, 60, 90):
        if code != 401 and code < 500:
            break
        time.sleep(pause)
        api.made = 0.0
        code, document = api.call(method, path, body, quiet=quiet)
    return code, document


def beta_review(api: Api, build: str) -> dict | None:
    """The TestFlight App Review submission of a build, or None when the build has none."""
    code, document = patient(api, 'GET', f'/builds/{build}/betaAppReviewSubmission', quiet=True)
    if code == 200:
        return document.get('data') or None
    if code == 404:
        return None
    raise SystemExit(f'App Store Connect did not say whether build {build} is in TestFlight App Review ({code}).')


def processed_build(api: Api, number: int) -> dict:
    """The build once Apple has processed it. An upload is listed before its processing ends."""
    import time
    deadline = time.time() + PROCESSING_MINUTES * 60
    while True:
        code, document = patient(api, 'GET', '/builds' + query(**{
            'filter[app]': APP_ID, 'filter[version]': str(number), 'limit': 1,
            'fields[builds]': 'version,processingState,expired,buildAudienceType'}))
        build = next(iter(document.get('data') or []), None) if code == 200 else None
        state = build['attributes'].get('processingState') if build else None
        if build is not None and state == 'VALID':
            return build
        if state in ('FAILED', 'INVALID'):
            raise SystemExit(f'App Store Connect could not process build {number}: {state}.')
        if time.time() > deadline:
            raise SystemExit(f'Build {number} is {state or "not listed"} after {PROCESSING_MINUTES} minutes, so it '
                             'stays out of the public group.')
        time.sleep(30)


def public_testflight(api: Api, number: int | None) -> None:
    """Put the build into the public TestFlight group and send it to TestFlight App Review. Its testers get the build
    after approval: the first build of a version (the UTC date) waits about a day, and later builds of the version
    often pass at once. While another build waits for that review, Apple takes no submission, so the run ends without
    one and the next release sends its own build."""
    if number is None:
        number = int(review_build(api, None)[0]['attributes']['version'])
    build = processed_build(api, number)
    attributes = build['attributes']
    if attributes.get('expired'):
        raise SystemExit(f'Build {number} has expired.')
    if attributes.get('buildAudienceType') != 'APP_STORE_ELIGIBLE':
        raise SystemExit(f'Build {number} is for internal testers only. A TestFlight run with public uploads a build '
                         'for the public group.')

    code, _ = patient(api, 'POST', f'/betaGroups/{PUBLIC_GROUP}/relationships/builds',
                      {'data': [{'type': 'builds', 'id': build['id']}]})
    if code != 204:
        # A refused call can still have changed the group, so the group decides.
        members = api.all(f'/betaGroups/{PUBLIC_GROUP}/relationships/builds' + query(limit=200))
        if not any(item.get('id') == build['id'] for item in members):
            raise SystemExit(f'App Store Connect did not put build {number} into the public group. Read the error '
                             'above.')
    print(f'Build {number} is in the public TestFlight group.')

    review = beta_review(api, build['id'])
    if review is None:
        code, document = patient(api, 'POST', '/betaAppReviewSubmissions', {'data': {
            'type': 'betaAppReviewSubmissions',
            'relationships': {'build': {'data': {'type': 'builds', 'id': build['id']}}}}})
        if code == 201:
            review = document.get('data')
        elif another_in_review(document):
            print(f'::notice::Build {number} waits in the public group without a review: another build is in '
                  'TestFlight App Review, so Apple took no submission. The next release sends its own build.')
            return
        else:
            # A refused call can still have made the submission, so the build decides.
            review = beta_review(api, build['id'])
            if review is None:
                raise SystemExit(f'App Store Connect did not send build {number} to TestFlight App Review. Read the '
                                 'error above.')
    values = (review or {}).get('attributes') or {}
    print(f"Build {number} is in TestFlight App Review: {values.get('betaReviewState')}, submitted "
          f"{values.get('submittedDate')}. Testers of the public link get it after approval.")


# The screenshots of the listing, by folder of app-store/screenshots, in the order of their file names. App Store
# Connect makes the smaller sizes from these two.
SCREENSHOTS = {'iphone': 'APP_IPHONE_67', 'ipad': 'APP_IPAD_PRO_3GEN_129'}
# The most screenshots that one size takes.
MOST_SCREENSHOTS = 10


def upload_screenshot(api: Api, screenshot_set: str, path: Path) -> str:
    """Upload one image into a set, and wait until App Store Connect has read it. Returns the ID of the screenshot."""
    import hashlib
    import time
    data = path.read_bytes()
    code, document = api.call('POST', '/appScreenshots', {'data': {
        'type': 'appScreenshots', 'attributes': {'fileName': path.name, 'fileSize': len(data)},
        'relationships': {'appScreenshotSet': {'data': {'type': 'appScreenshotSets', 'id': screenshot_set}}}}})
    if code != 201:
        raise SystemExit(f'App Store Connect did not take {path.name}. Read the error above.')
    identifier = document['data']['id']
    for operation in document['data']['attributes'].get('uploadOperations') or []:
        part = data[operation['offset']:operation['offset'] + operation['length']]
        headers = {header['name']: header['value'] for header in operation.get('requestHeaders') or []}
        request = urllib.request.Request(operation['url'], data=part, headers=headers, method=operation['method'])
        with urllib.request.urlopen(request, timeout=120):
            pass
    code, _ = api.call('PATCH', f'/appScreenshots/{identifier}', {'data': {
        'type': 'appScreenshots', 'id': identifier,
        'attributes': {'uploaded': True, 'sourceFileChecksum': hashlib.md5(data).hexdigest()}}})
    if code != 200:
        raise SystemExit(f'App Store Connect did not finish {path.name}. Read the error above.')
    for _ in range(60):
        attributes = api.get(f'/appScreenshots/{identifier}').get('data', {}).get('attributes', {})
        delivery = attributes.get('assetDeliveryState') or {}
        if delivery.get('state') == 'COMPLETE':
            return identifier
        if delivery.get('state') == 'FAILED':
            raise SystemExit(f"App Store Connect refused {path.name}: {delivery.get('errors')}")
        time.sleep(5)
    raise SystemExit(f'App Store Connect did not read {path.name} in five minutes.')


def screenshots(api: Api) -> None:
    """Put the images of app-store/screenshots into the listing of the version, in place of the ones it has."""
    version = editable_version(api)
    localization = next((item for item in api.all(f"/appStoreVersions/{version['id']}/appStoreVersionLocalizations")
                         if item['attributes'].get('locale') == 'en-US'), None)
    if not localization:
        raise SystemExit('The version has no en-US listing.')
    sets = {item['attributes'].get('screenshotDisplayType'): item['id'] for item in
            api.all(f"/appStoreVersionLocalizations/{localization['id']}/appScreenshotSets")}
    for folder, display in SCREENSHOTS.items():
        images = sorted(path for path in (ROOT / 'app-store/screenshots' / folder).glob('*')
                        if path.suffix.lower() in ('.png', '.jpg', '.jpeg'))
        if not images:
            continue
        if len(images) > MOST_SCREENSHOTS:
            raise SystemExit(f'{folder} has {len(images)} screenshots. App Store Connect takes {MOST_SCREENSHOTS}.')
        screenshot_set = sets.get(display)
        if not screenshot_set:
            code, document = api.call('POST', '/appScreenshotSets', {'data': {
                'type': 'appScreenshotSets', 'attributes': {'screenshotDisplayType': display},
                'relationships': {'appStoreVersionLocalization': {'data': {
                    'type': 'appStoreVersionLocalizations', 'id': localization['id']}}}}})
            if code != 201:
                raise SystemExit(f'App Store Connect did not make the {display} set. Read the error above.')
            screenshot_set = document['data']['id']
        old = [item['id'] for item in api.all(f'/appScreenshotSets/{screenshot_set}/appScreenshots')]
        # The listing keeps its old images until the new ones are in, when the set has room for both.
        first = old if len(old) + len(images) > MOST_SCREENSHOTS else []
        for identifier in first:
            api.call('DELETE', f'/appScreenshots/{identifier}')
        new = [upload_screenshot(api, screenshot_set, path) for path in images]
        for identifier in old:
            if identifier not in first:
                api.call('DELETE', f'/appScreenshots/{identifier}')
        code, _ = api.call('PATCH', f'/appScreenshotSets/{screenshot_set}/relationships/appScreenshots',
                           {'data': [{'type': 'appScreenshots', 'id': identifier} for identifier in new]})
        if code != 204:
            raise SystemExit(f'App Store Connect did not order the {display} screenshots. Read the error above.')
        print(f'{display}: {len(new)} screenshots from app-store/screenshots/{folder}, in place of {len(old)}.')


# The states of the app info of a live version. Its name changes only with the next version, which has an app info of
# its own.
LIVE_INFO = ('READY_FOR_DISTRIBUTION', 'REPLACED_WITH_NEW_INFO')


def rename(api: Api, name: str) -> None:
    """Gives the app a new name in each language of its listing, which TestFlight shows too. App Store Connect refuses
    a name that another app has, also an app that is not released, and the log then shows its reason."""
    name = name.strip()
    if not name or len(name) > 30:
        sys.exit('An App Store name has 1 to 30 characters.')
    for info in api.all(f'/apps/{APP_ID}/appInfos'):
        state = info['attributes'].get('state') or info['attributes'].get('appStoreState')
        if state in LIVE_INFO:
            print(f'  The live version keeps its name until the next version ({state}).')
            continue
        for localization in api.all(f"/appInfos/{info['id']}/appInfoLocalizations"):
            values = localization['attributes']
            before = values.get('name')
            if before == name:
                print(f"  {values.get('locale')}: the name is {name!r} already.")
                continue
            status, document = api.call('PATCH', f"/appInfoLocalizations/{localization['id']}", {'data': {
                'type': 'appInfoLocalizations', 'id': localization['id'], 'attributes': {'name': name}}})
            if status != 200:
                sys.exit(f"{values.get('locale')}: App Store Connect kept the name {before!r}.")
            print(f"  {values.get('locale')}: {before!r} is now {document['data']['attributes'].get('name')!r}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=['status', 'screenshots', 'submit', 'submit-when-ready', 'public-testflight',
                                           'rename'])
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--issuer', required=True)
    parser.add_argument('--build', type=int, help='The build number. Without it: the newest build App Review takes.')
    parser.add_argument('--name', help='For rename: the new name of the app, at most 30 characters.')
    args = parser.parse_args()
    if args.action == 'rename' and not args.name:
        parser.error('rename needs --name.')
    api = Api(args.key, args.key_id, args.issuer)
    if args.action == 'status':
        status(api)
    elif args.action == 'screenshots':
        screenshots(api)
    elif args.action in ('submit', 'submit-when-ready'):
        submit(api, args.build, wait=args.action == 'submit-when-ready')
    elif args.action == 'public-testflight':
        public_testflight(api, args.build)
    elif args.action == 'rename':
        rename(api, args.name)


if __name__ == '__main__':
    main()
