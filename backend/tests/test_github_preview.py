"""A GitHub address in a chat becomes a small normalized preview, read with the connected GitHub account only."""
import base64
import http.client
import json
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from coconut.business import GitHubBusiness
from coconut.github_preview import GitHubPreviews, MAX_ENTRIES, MAX_LINES, parse
from coconut.providers import FixtureProvider
from coconut.server import TunnelServer
from coconut.service import Service
from coconut.service_base import ServiceError


class Connection:
    value = 'test-connection'
    def token(self): return self.value
    def refresh(self): pass
    def close(self): pass


def person(login, number=1):
    return dict(login=login, avatar_url=f'https://avatars.githubusercontent.com/u/{number}?v=4')


SOURCE = '\n'.join(f'line {number}' for number in range(1, 61)) + '\n'
BASE = 'repos/antiwork/gumroad'
PULL = dict(number=2314, title='Retry failed Ping webhooks with backoff', state='open', draft=False, merged=False,
            user=person('slavingia'), additions=184, deletions=37, changed_files=9, comments=2, review_comments=3,
            updated_at='2026-11-16T14:02:11Z', head=dict(sha='a' * 40), requested_reviewers=[person('gianfrancopiana', 2)])
ISSUE = dict(number=2291, title='Friday payouts are sent twice for creators in two currencies', state='open',
             state_reason=None, user=person('slavingia'), comments=14, updated_at='2026-11-15T09:30:00Z',
             labels=[dict(name='bug', color='D73A4A'), dict(name='payouts', color='0e8a16'), dict(name='odd', color='red')],
             assignees=[person('gianfrancopiana', 2)])
COMMIT = dict(sha='9f2c4e1' + 'b' * 33, author=person('slavingia'), stats=dict(additions=12, deletions=4, total=16),
              files=[{}, {}, {}],
              commit=dict(message='Hold a payout while its refund is pending\n\nThe job read the old balance.',
                          author=dict(name='Sahil Lavingia', date='2026-11-14T18:00:00Z'),
                          committer=dict(name='GitHub', date='2026-11-14T18:05:00Z')))
REPOSITORY = dict(full_name='antiwork/gumroad', description='Sell stuff and see what sticks', stargazers_count=7421,
                  forks_count=1502, language='Ruby', private=False, archived=False, pushed_at='2026-11-16T12:00:00Z')


class GitHub:
    """The GitHub of the tests: fixed answers for the paths of the cards, and a record of each request."""

    def __init__(self):
        self.calls = []
        self.answers = {
            BASE: REPOSITORY,
            f'{BASE}/pulls/2314': PULL,
            f'{BASE}/commits/{"a" * 40}/check-runs': dict(check_runs=[
                dict(status='completed', conclusion='success'), dict(status='completed', conclusion='skipped'),
                dict(status='in_progress', conclusion=None)]),
            f'{BASE}/commits/{"a" * 40}/status': dict(statuses=[dict(state='success')]),
            f'{BASE}/pulls/2314/reviews': [dict(user=person('gianfrancopiana', 2), state='CHANGES_REQUESTED'),
                                           dict(user=person('gianfrancopiana', 2), state='COMMENTED'),
                                           dict(user=person('gianfrancopiana', 2), state='APPROVED')],
            f'{BASE}/issues/2291': ISSUE,
            f'{BASE}/issues/2314': dict(number=2314, title=PULL['title'], pull_request={}),
            f'{BASE}/commits/9f2c4e1': COMMIT,
            f'{BASE}/contents/app/services/payouts_processor.rb': dict(
                type='file', encoding='base64', content=base64.encodebytes(SOURCE.encode()).decode()),
            f'{BASE}/contents/payouts/app/models/payment.rb': dict(
                type='file', encoding='base64', content=base64.b64encode(b'class Payment\nend\n').decode()),
            f'{BASE}/issues/comments/991': dict(
                user=person('gianfrancopiana', 2), updated_at='2026-11-16T10:00:00Z',
                body='<!-- bot -->\n> Is this safe?\n\nYes. The job [locks the balance](https://example.com) before it pays,\nso a second run finds nothing.'),
            f'{BASE}/pulls/comments/55': dict(user=person('slavingia'), path='app/models/payment.rb', body='Use `with_lock` here.',
                                              created_at='2026-11-16T11:00:00Z'),
        }

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token))
        answer = self.answers.get(urlsplit(path).path)
        if isinstance(answer, ServiceError):
            raise ServiceError(answer.status, str(answer))
        if answer is None:
            raise ServiceError(404, 'Missing')
        return answer


class ParseTests(unittest.TestCase):
    def test_each_kind_of_address_is_recognized(self):
        self.assertEqual(parse('https://github.com/antiwork/gumroad'), dict(owner='antiwork', repository='gumroad', kind='repository'))
        self.assertEqual(parse('https://github.com/antiwork/gumroad/pull/2314/files')['kind'], 'pull')
        self.assertEqual(parse('https://github.com/antiwork/gumroad/issues/2291')['number'], 2291)
        self.assertEqual(parse('https://github.com/antiwork/gumroad/commit/9F2C4E1')['sha'], '9f2c4e1')
        file = parse('https://github.com/antiwork/gumroad/blob/main/app/models/payment.rb#L20-L12')
        self.assertEqual((file['segments'], file['start'], file['end']), (['main', 'app', 'models', 'payment.rb'], 12, 20))
        self.assertEqual(parse('https://github.com/antiwork/gumroad/blob/main/a.rb#L7')['end'], 7)
        comment = parse('https://github.com/antiwork/gumroad/pull/2314#discussion_r55')
        self.assertEqual((comment['comment'], comment['comment_id']), ('discussion_r', 55))

    def test_other_addresses_are_refused(self):
        for address in ('http://github.com/antiwork/gumroad', 'https://github.com.evil.example/antiwork/gumroad',
                        'https://user@github.com/antiwork/gumroad', 'https://github.com/settings/tokens',
                        'https://github.com/antiwork', 'https://github.com/antiwork/gumroad/pull/0',
                        'https://github.com/antiwork/gumroad/pull/12abc', 'https://github.com/antiwork/..%2f..%2fuser',
                        'https://github.com/antiwork/gumroad/blob/main/../../../user', 'https://github.com/antiwork/gumroad/commit/xyz',
                        'https://github.com/antiwork/gumroad/actions', 'https://gist.github.com/a/b', 'https://github.com:8443/a/b',
                        'https://github.com/' + 'a' * 3000, '', None, 7):
            self.assertIsNone(parse(address), address)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.remote = GitHub()
        self.connection = Connection()
        self.time = 1000.0
        business = GitHubBusiness(self.connection, request=self.remote)
        self.previews = GitHubPreviews(lambda: business, clock=lambda: self.time)

    def test_a_pull_request_has_its_state_size_checks_and_review(self):
        url = 'https://github.com/antiwork/gumroad/pull/2314'
        card = self.previews.preview(url)
        self.assertEqual(card, dict(
            kind='pull', url=url, repository='antiwork/gumroad', number=2314, title='Retry failed Ping webhooks with backoff',
            state='open', author=person('slavingia'), additions=184, deletions=37, changed_files=9, comments=5,
            checks=dict(state='pending', passed=3, failed=0, pending=1), review='approved', updated_at='2026-11-16T14:02:11Z'))
        self.assertTrue(all(call[2] == 'test-connection' and call[0] == 'GET' for call in self.remote.calls))

    def test_the_states_of_a_pull_request(self):
        base = f'{BASE}/pulls/2314'
        for change, state in ((dict(draft=True), 'draft'), (dict(state='closed'), 'closed'), (dict(state='closed', merged=True), 'merged')):
            self.remote.answers[base] = dict(PULL, **change)
            self.time += 1000
            card = self.previews.preview('https://github.com/antiwork/gumroad/pull/2314')
            self.assertEqual(card['state'], state)
            if state in ('closed', 'merged'):
                self.assertIsNone(card['checks'])

    def test_a_pull_request_without_checks_or_reviews_still_shows(self):
        for path in ('check-runs', 'status'):
            self.remote.answers[f'{BASE}/commits/{"a" * 40}/{path}'] = ServiceError(403, 'Refused')
        self.remote.answers[f'{BASE}/pulls/2314/reviews'] = []
        card = self.previews.preview('https://github.com/antiwork/gumroad/pull/2314')
        self.assertIsNone(card['checks'])
        self.assertEqual(card['review'], 'review_requested')

    def test_failing_checks_and_requested_changes(self):
        self.remote.answers[f'{BASE}/commits/{"a" * 40}/status'] = dict(statuses=[dict(state='failure')])
        self.remote.answers[f'{BASE}/pulls/2314/reviews'] = [dict(user=person('a'), state='APPROVED'), dict(user=person('b'), state='CHANGES_REQUESTED')]
        card = self.previews.preview('https://github.com/antiwork/gumroad/pull/2314')
        self.assertEqual((card['checks']['state'], card['review']), ('failing', 'changes_requested'))

    def test_an_issue_has_labels_assignees_and_comments(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad/issues/2291')
        self.assertEqual((card['kind'], card['state'], card['comments']), ('issue', 'open', 14))
        self.assertEqual(card['labels'], [dict(name='bug', color='d73a4a'), dict(name='payouts', color='0e8a16'), dict(name='odd', color=None)])
        self.assertEqual(card['assignees'], [person('gianfrancopiana', 2)])
        self.remote.answers[f'{BASE}/issues/2291'] = dict(ISSUE, state='closed', state_reason='not_planned',
                                                          labels=[dict(name=f'label {n}', color='ffffff') for n in range(9)])
        self.time += 1000
        card = self.previews.preview('https://github.com/antiwork/gumroad/issues/2291')
        self.assertEqual((card['state'], len(card['labels']), card['more_labels']), ('not_planned', 6, 3))

    def test_an_issue_address_of_a_pull_request_shows_the_pull_request(self):
        self.assertEqual(self.previews.preview('https://github.com/antiwork/gumroad/issues/2314')['kind'], 'pull')

    def test_a_commit_has_its_first_line_and_size(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad/commit/9f2c4e1')
        self.assertEqual((card['title'], card['additions'], card['deletions'], card['changed_files'], card['author']['login']),
                         ('Hold a payout while its refund is pending', 12, 4, 3, 'slavingia'))
        self.assertEqual(len(card['sha']), 40)
        self.remote.answers[f'{BASE}/commits/9f2c4e1'] = dict(COMMIT, author=None)
        self.time += 1000
        self.assertEqual(self.previews.preview('https://github.com/antiwork/gumroad/commit/9f2c4e1')['author'],
                         dict(login='Sahil Lavingia', avatar_url=None))

    def test_a_repository_has_its_description_stars_and_language(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad')
        self.assertEqual((card['repository'], card['stars'], card['language'], card['private']), ('antiwork/gumroad', 7421, 'Ruby', False))

    def test_a_file_with_a_line_range_has_a_bounded_snippet(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/main/app/services/payouts_processor.rb#L12-L15')
        self.assertEqual((card['path'], card['ref'], card['start_line'], card['end_line'], card['lines'], card['more_lines']),
                         ('app/services/payouts_processor.rb', 'main', 12, 15, ['line 12', 'line 13', 'line 14', 'line 15'], 0))
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/main/app/services/payouts_processor.rb#L5-L900')
        self.assertEqual((len(card['lines']), card['end_line'], card['more_lines']), (MAX_LINES, 60, 56 - MAX_LINES))
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/main/app/services/payouts_processor.rb')
        self.assertEqual((card['lines'], card['start_line']), ([], None))
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/main/app/services/payouts_processor.rb#L400')
        self.assertEqual(card['lines'], [])

    def test_a_branch_name_with_a_slash_is_found(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/fix/payouts/app/models/payment.rb#L1')
        self.assertEqual((card['ref'], card['path'], card['lines']), ('fix', 'payouts/app/models/payment.rb', ['class Payment']))
        del self.remote.answers[f'{BASE}/contents/payouts/app/models/payment.rb']
        self.remote.answers[f'{BASE}/contents/app/models/payment.rb'] = dict(
            type='file', encoding='base64', content=base64.b64encode(b'class Payment\nend\n').decode())
        self.time += 1000
        card = self.previews.preview('https://github.com/antiwork/gumroad/blob/fix/payouts/app/models/payment.rb#L1')
        self.assertEqual((card['ref'], card['path']), ('fix/payouts', 'app/models/payment.rb'))
        self.assertEqual(parse_qs(urlsplit(self.remote.calls[-1][1]).query), dict(ref=['fix/payouts']))

    def test_a_comment_has_its_text_its_author_and_the_title_of_its_page(self):
        card = self.previews.preview('https://github.com/antiwork/gumroad/issues/2291#issuecomment-991')
        self.assertEqual((card['kind'], card['on'], card['number'], card['title'], card['author']['login']),
                         ('comment', 'issue', 2291, ISSUE['title'], 'gianfrancopiana'))
        self.assertEqual(card['excerpt'], 'Yes. The job locks the balance before it pays, so a second run finds nothing.')
        card = self.previews.preview('https://github.com/antiwork/gumroad/pull/2314#discussion_r55')
        self.assertEqual((card['on'], card['path'], card['excerpt']), ('pull', 'app/models/payment.rb', 'Use `with_lock` here.'))

    def test_a_long_text_is_cut(self):
        self.remote.answers[f'{BASE}/issues/2291'] = dict(ISSUE, title='t' * 5000)
        self.remote.answers[f'{BASE}/issues/comments/991'] = dict(user=person('a'), body='word ' * 5000)
        self.assertEqual(len(self.previews.preview('https://github.com/antiwork/gumroad/issues/2291')['title']), 300)
        self.assertLessEqual(len(self.previews.preview('https://github.com/antiwork/gumroad/issues/2291#issuecomment-991')['excerpt']), 400)

    def test_a_preview_is_kept_for_a_short_time(self):
        url = 'https://github.com/antiwork/gumroad/issues/2291'
        first = self.previews.preview(url)
        count = len(self.remote.calls)
        self.assertEqual(self.previews.preview(url), first)
        self.assertEqual(len(self.remote.calls), count)
        self.time += 91
        self.previews.preview(url)
        self.assertEqual(len(self.remote.calls), count + 1)

    def test_a_private_or_missing_page_fails_and_is_not_asked_again_at_once(self):
        url = 'https://github.com/antiwork/secret/pull/3'
        for _ in range(2):
            with self.assertRaises(ServiceError) as error: self.previews.preview(url)
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(len(self.remote.calls), 1)
        self.time += 301
        with self.assertRaises(ServiceError): self.previews.preview(url)
        self.assertEqual(len(self.remote.calls), 2)

    def test_a_rate_limit_fails_and_waits_a_minute(self):
        self.remote.answers[BASE] = ServiceError(403, 'GitHub refused this request.')
        for _ in range(3):
            with self.assertRaises(ServiceError) as error: self.previews.preview('https://github.com/antiwork/gumroad')
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.remote.calls), 1)
        self.remote.answers[BASE] = REPOSITORY
        self.time += 61
        self.assertEqual(self.previews.preview('https://github.com/antiwork/gumroad')['stars'], 7421)

    def test_a_malformed_answer_is_an_error_not_a_crash(self):
        self.remote.answers[f'{BASE}/pulls/2314'] = dict(title=None)
        with self.assertRaises(ServiceError) as error: self.previews.preview('https://github.com/antiwork/gumroad/pull/2314')
        self.assertEqual(error.exception.status, 502)

    def test_another_address_never_reaches_github(self):
        with self.assertRaises(ServiceError) as error: self.previews.preview('https://example.com/antiwork/gumroad/pull/1')
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.remote.calls, [])

    def test_without_a_connected_account_nothing_is_read_or_kept(self):
        self.connection.value = None
        with self.assertRaises(ServiceError) as error: self.previews.preview('https://github.com/antiwork/gumroad')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.remote.calls, [])
        self.connection.value = 'test-connection'
        self.assertEqual(self.previews.preview('https://github.com/antiwork/gumroad')['kind'], 'repository')

    def test_another_account_does_not_get_the_kept_preview(self):
        self.previews.preview('https://github.com/antiwork/gumroad')
        self.connection.value = 'another-connection'
        self.previews.preview('https://github.com/antiwork/gumroad')
        self.assertEqual([call[2] for call in self.remote.calls], ['test-connection', 'another-connection'])

    def test_the_cache_has_a_size_limit(self):
        for number in range(1, MAX_ENTRIES + 40):
            self.remote.answers[f'{BASE}/issues/{number}'] = dict(ISSUE, number=number)
            self.previews.preview(f'https://github.com/antiwork/gumroad/issues/{number}')
        self.assertLessEqual(len(self.previews.cache), MAX_ENTRIES)


class PreviewServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.service = Service(temp.name, FixtureProvider(temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.remote = GitHub()
        self.service.github = Connection()
        self.service.business = GitHubBusiness(self.service.github, request=self.remote)

    def test_the_service_reads_previews_with_its_github_connection(self):
        self.assertEqual(self.service.github_previews.preview('https://github.com/antiwork/gumroad')['kind'], 'repository')
        self.assertEqual(self.remote.calls, [('GET', BASE, 'test-connection')])

    def test_a_paired_phone_reads_a_preview_and_an_unpaired_one_does_not(self):
        peer = 'ab' * 32
        token = self.service.pair({'code': self.service.pairing_code(tunnel=True)}, peer)['token']
        server = TunnelServer(0, self.service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        def read(address, headers):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request('GET', '/v1/github/preview?url=' + quote(address, safe=''), headers=headers)
            response = connection.getresponse()
            return response.status, json.load(response)
        paired = {'Authorization': 'Bearer ' + token, 'X-Coconut-Peer': peer}
        address = 'https://github.com/antiwork/gumroad/blob/main/app/services/payouts_processor.rb#L12-L13'
        status, body = read(address, paired)
        self.assertEqual((status, body['kind'], body['lines'], body['url']), (200, 'file', ['line 12', 'line 13'], address))
        self.assertEqual(read('https://github.com/antiwork/secret', paired)[0], 404)
        self.assertEqual(read('https://example.com/a/b', paired)[0], 400)
        count = len(self.remote.calls)
        self.assertEqual(read(address, {'X-Coconut-Peer': peer})[0], 401)
        self.assertEqual(len(self.remote.calls), count)


if __name__ == '__main__':
    unittest.main()
