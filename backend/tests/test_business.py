"""Business functions and projects retain their identities and work in GitHub, across agent and app restarts."""
import copy
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from coconut.business import GitHubBusiness, PROJECT_TOPIC, project_label
from coconut.providers import FixtureProvider
from coconut.service import Service
from coconut.service_base import ServiceError


class Connection:
    value = 'test-connection'
    login = 'person'
    def token(self): return self.value
    def account(self): return {'login': self.login}
    def refresh(self): pass
    def close(self): pass


class GitHub:
    def __init__(self):
        self.calls = []
        self.repos = {name: dict(name=name, full_name='antiwork/' + name, html_url='https://github.com/antiwork/' + name,
                               private=True, archived=False, has_issues=True,
                               description=name, topics=[PROJECT_TOPIC] if name == 'gumroad-private' else [])
                      for name in ('accounting', 'marketing', 'support', 'gumroad-private', 'helper')}
        self.issues = {'accounting': [], 'marketing': [], 'support': []}
        self.labels = set()
        self.observe = None

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        if self.observe: self.observe()
        parsed = urlsplit(path)
        parts = parsed.path.split('/')
        query = parse_qs(parsed.query)
        if parts == ['orgs', 'antiwork', 'repos']:
            values = list(self.repos.values())
        elif len(parts) >= 3 and parts[:2] == ['repos', 'antiwork']:
            name = parts[2]
            if name not in self.repos:
                raise ServiceError(404, 'Missing repository')
            if len(parts) == 3: return copy.deepcopy(self.repos[name])
            if parts[3] == 'topics' and method == 'PUT':
                self.repos[name]['topics'] = body['names']
                return {'names': body['names']}
            if parts[3] == 'labels':
                if method == 'POST':
                    self.labels.add((name, body['name'])); return dict(body)
                if (name, unquote(parts[4])) not in self.labels:
                    raise ServiceError(404, 'Missing label')
                return {'name': unquote(parts[4])}
            if parts[3] != 'issues': raise AssertionError(path)
            if method == 'POST':
                issue = dict(number=len(self.issues[name]) + 1, title=body['title'], body=body['body'], state='open',
                             html_url=f'https://github.com/antiwork/{name}/issues/{len(self.issues[name]) + 1}',
                             updated_at='2026-11-16T12:00:00Z', user={'login': 'person'},
                             labels=[{'name': value} for value in body.get('labels', [])])
                self.issues[name].append(issue)
                return copy.deepcopy(issue)
            if len(parts) == 5:
                issue = self.issues[name][int(parts[4]) - 1]
                if method == 'PATCH': issue.update(body)
                return copy.deepcopy(issue)
            values = self.issues[name]
            state = query.get('state', ['open'])[0]
            if state != 'all': values = [issue for issue in values if issue['state'] == state]
            if 'labels' in query:
                values = [issue for issue in values if query['labels'][0] in [label['name'] for label in issue['labels']]]
        else:
            raise AssertionError(path)
        size, page = int(query.get('per_page', [100])[0]), int(query.get('page', [1])[0])
        return copy.deepcopy(values[(page - 1) * size:page * size])


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.remote = GitHub()
        self.connection = Connection()
        self.business = GitHubBusiness(self.connection, request=self.remote)

    def create(self, function='accounting', project='gumroad-private', title='Close the month', request=None):
        return self.business.create_work(function, dict(title=title, body='Review the ledger.', project_key=project), request or str(uuid4()))

    def test_reserved_functions_and_projects_come_from_github(self):
        self.remote.repos['.github'] = dict(self.remote.repos['helper'], name='.github')
        catalog = self.business.catalog()
        self.assertEqual([item['key'] for item in catalog['functions']], ['accounting', 'marketing', 'support'])
        self.assertTrue(all(item['reserved'] for item in catalog['functions']))
        self.assertEqual([item['key'] for item in catalog['projects']], ['gumroad-private'])
        self.assertEqual(catalog['functions'][0]['repository']['full_name'], 'antiwork/accounting')
        for reserved in ('accounting', 'MARKETING', 'Support'):
            with self.assertRaises(ServiceError) as error: self.business.add_project({'key': reserved})
            self.assertEqual(error.exception.status, 409)

    def test_project_registration_preserves_other_github_topics_and_survives_a_new_client(self):
        self.remote.repos['helper']['topics'] = ['support']
        self.business.add_project({'key': 'helper'})
        self.assertEqual(self.remote.repos['helper']['topics'], ['support', PROJECT_TOPIC])
        other = GitHubBusiness(self.connection, request=self.remote)
        self.assertEqual([item['key'] for item in other.catalog()['projects']], ['gumroad-private', 'helper'])

    def test_one_function_serves_multiple_projects_and_project_views_aggregate_functions(self):
        self.business.add_project({'key': 'helper'})
        self.create()
        self.create('marketing', title='Launch creator campaign')
        self.create(project='helper', title='Reconcile support revenue')
        self.assertEqual(len(self.business.work('accounting')['items']), 2)
        self.assertEqual({item['function_key'] for item in self.business.work(project_key='gumroad-private')['items']}, {'accounting', 'marketing'})
        writes = [path for method, path, _, _ in self.remote.calls if method == 'POST' and path.endswith('/issues')]
        self.assertTrue(all('/accounting/' in path or '/marketing/' in path for path in writes))
        self.assertFalse(any('/gumroad-private/issues' in path for path in writes))

    def test_an_accepted_github_write_is_recovered_after_a_lost_local_receipt(self):
        request = str(uuid4())
        first = self.create(request=request)
        other = GitHubBusiness(self.connection, request=self.remote)
        self.assertEqual(other.create_work('accounting', dict(title='Close the month', body='Review the ledger.', project_key='gumroad-private'), request), first)
        self.assertEqual(len(self.remote.issues['accounting']), 1)
        self.assertNotIn('coconut-work:', first['body'])
        with self.assertRaises(ServiceError) as error: self.create(title='Different work', request=request)
        self.assertEqual(error.exception.status, 409)

    def test_complete_and_reopen_write_the_same_github_issue(self):
        work = self.create()
        self.assertEqual(self.business.update_work('accounting', str(work['number']), {'state': 'closed'})['state'], 'closed')
        self.assertEqual(self.business.work()['items'], [])
        self.assertEqual(len(self.business.work(state='closed')['items']), 1)
        self.assertEqual(self.business.update_work('accounting', str(work['number']), {'state': 'open'})['id'], work['id'])

    def test_pagination_keeps_all_issues_and_excludes_pull_requests(self):
        for number in range(53): self.create(title=f'Work {number}', project=None)
        self.remote.issues['accounting'][0]['pull_request'] = {'url': 'a pull request'}
        self.assertEqual(len(self.business.work('accounting')['items']), 52)
        with self.assertRaises(ServiceError): self.business.update_work('accounting', '1', {'state': 'closed'})

    def test_long_repository_keys_fit_github_label_limits_and_keep_their_identity(self):
        name = 'a-project-' + 'x' * 60
        self.remote.repos[name] = dict(self.remote.repos['helper'], name=name, full_name='antiwork/' + name, topics=[PROJECT_TOPIC])
        work = self.create(project=name)
        self.assertLessEqual(len(project_label(name)), 50)
        self.assertEqual(work['project_key'], name)
        self.assertEqual(len(self.business.work(project_key=name)['items']), 1)

    def test_authentication_and_repository_failures_are_not_an_empty_business(self):
        self.connection.value = ''
        with self.assertRaises(ServiceError) as error: self.business.catalog()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.remote.calls, [])
        self.connection.value = 'different-account'
        self.business.catalog()
        self.remote.repos.pop('gumroad-private')
        self.connection.value = 'third-account'
        self.assertEqual(self.business.catalog()['projects'], [])
        self.remote.repos['accounting']['has_issues'] = False
        with self.assertRaises(ServiceError): self.create(project=None)
        self.remote.repos['accounting']['has_issues'] = True
        self.remote.repos['accounting']['archived'] = True
        with self.assertRaises(ServiceError): self.create(project=None)

    def test_invalid_scope_and_titles_fail_before_creating_work(self):
        for function, project, title in [('gumroad-private', None, 'Work'), ('accounting', 'accounting', 'Work'),
                                          ('accounting', 'helper', 'Work'), ('accounting', None, 'bad\ntitle')]:
            with self.subTest(function=function, project=project, title=title), self.assertRaises(ServiceError):
                self.create(function=function, project=project, title=title)
        self.assertEqual(self.remote.issues['accounting'], [])


class BusinessServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(self.temp.name), start_workers=False)
        self.addCleanup(self.service.close)
        self.remote = GitHub()
        self.service.github = Connection()
        self.service.business = GitHubBusiness(self.service.github, request=self.remote)

    def test_service_retries_do_not_duplicate_work_and_keys_cannot_be_reused(self):
        key = str(uuid4())
        route = '/v1/business/functions/accounting/work'
        data = dict(title='Close the month', body='Review the ledger.', project_key='gumroad-private')
        first = self.service.mutate('POST', route, data, key, 'phone')
        count = len(self.remote.calls)
        self.assertEqual(self.service.mutate('POST', route, data, key, 'phone'), first)
        self.assertEqual(len(self.remote.calls), count)
        with self.assertRaises(ServiceError) as error: self.service.mutate('POST', route, dict(data, title='Changed'), key, 'phone')
        self.assertEqual(error.exception.status, 409)
        self.service.github.login = 'another-person'
        with self.assertRaises(ServiceError): self.service.mutate('POST', route, data, key, 'phone')

    def test_github_requests_do_not_hold_the_service_writer_lock(self):
        observed = []
        def observe():
            def write():
                acquired = self.service.lock.acquire(timeout=.5)
                observed.append(acquired)
                if acquired: self.service.lock.release()
            worker = threading.Thread(target=write)
            worker.start(); worker.join(2)
        self.remote.observe = observe
        self.service.mutate('POST', '/v1/business/projects', {'key': 'helper'}, str(uuid4()), 'phone')
        self.assertTrue(observed)
        self.assertTrue(all(observed))
