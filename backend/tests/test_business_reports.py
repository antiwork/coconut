import base64
import json
import unittest

from coconut.business import GitHubBusiness
from coconut.business_reports import report, ledger
from coconut.service_base import ServiceError


class Connection:
    value = 'test-only'
    def refresh(self): pass
    def token(self): return self.value


class BusinessReportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.payload = {'generated_at': '2026-11-16T12:00:00Z', 'ledger': {'ltm_revenue': None},
                        'support': {'open': [{'subject': 'Synthetic support record'} for _ in range(500)]}}
        self.text = json.dumps(self.payload)
        self.connection = Connection()
        self.business = GitHubBusiness(self.connection, request=self.remote)

    def remote(self, method, path, token, body):
        self.calls.append(path)
        if '/contents/' in path:
            return dict(encoding='base64', sha='source-version', html_url='https://github.com/example/source',
                        content=base64.b64encode(self.text.encode()).decode())
        if '/git/trees/ledger-tree' in path:
            return dict(tree=[dict(type='blob', path='2026/11.beancount', sha='month'), dict(type='tree', path='2026', sha='y')])
        if '/git/trees/records-tree' in path:
            raise ServiceError(502, 'GitHub returned too much data. Narrow the view and try again.')
        if '/git/trees/' in path:
            return dict(tree=[dict(type='tree', path='ledger', sha='ledger-tree'),
                              dict(type='tree', path='records', sha='records-tree'),
                              dict(type='blob', path='main.beancount', sha='main'),
                              dict(type='blob', path='README.md', sha='readme')])
        return dict(default_branch='main')

    def test_reports_preserve_every_record_missing_value_and_source_version(self):
        result = report(self.business, 'accounting')
        self.assertEqual(result['data'], self.payload)
        self.assertEqual(result['source']['sha'], 'source-version')
        self.assertEqual(result['generated_at'], self.payload['generated_at'])
        self.assertIn('antiwork/accounting', result['ledger_url'])
        self.assertIn('/dashboards/contents/docs/0825/home.json', self.calls[0])

    def test_readers_accept_only_fixed_report_names(self):
        for name in ('../accounting', 'https://example.com', 'home', ''):
            with self.assertRaises(ServiceError): report(self.business, name)
        self.assertEqual(self.calls, [])

    def test_malformed_reports_fail_instead_of_showing_zeroes(self):
        for text in ('invalid', '[]', 'null'):
            self.text = text
            with self.assertRaises(ServiceError) as error: report(self.business, 'support')
            self.assertEqual(error.exception.status, 502)

    def test_beancount_is_read_from_accounting_without_modification(self):
        index = ledger(self.business)
        self.assertEqual([v['path'] for v in index['files']], ['main.beancount', 'ledger/2026/11.beancount'])
        # records/ is too large to list, as in the real repository, and the index still loads.
        self.assertEqual(index['skipped'], ['records'])
        self.text = '2026-11-01 open Assets:Example USD\n'
        result = ledger(self.business, 'main.beancount')
        self.assertEqual(result['content'], self.text)
        self.assertIn('/accounting/contents/main.beancount', self.calls[-1])
        for path in ('../secret.beancount', '/main.beancount', 'README.md', 'ledger\\test.beancount'):
            with self.assertRaises(ServiceError): ledger(self.business, path)

    def test_disconnected_reader_never_uses_another_github_login(self):
        self.connection.value = None
        with self.assertRaises(ServiceError) as error: report(self.business, 'support')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.calls, [])


if __name__ == '__main__': unittest.main()
