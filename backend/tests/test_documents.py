"""Documents of the bots: the page rules, the end dates, the tools of a gateway turn and the routes of the phone."""
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import unittest
from uuid import uuid4

from coconut.documents import KEPT_VERSIONS, end_time, page_problems
from coconut.library_replies import document_of, is_report
from coconut.service import Service, ServiceError
from test_gateway_chat import Adapter, Provider

TREEMAP = '''<h1>Revenue</h1><svg id="chart"></svg>
<script>
const root = d3.hierarchy({children: [{name: "Courses", value: 41200}, {name: "Ebooks", value: 18300}]}).sum(d => d.value);
d3.select("#chart").selectAll("rect").data(root.leaves()).join("rect").attr("fill", (d, i) => coconut.palette[i]);
</script>
<style>h1 { color: var(--text); } #add { margin: 0 }</style>'''

REPORT = ('## Profit report, October\n\nNet profit rose 12% to $41,200 before income tax.\n\n### Revenue\n\n'
          + ''.join(f'- Product {n}: ${n * 1000:,} in sales, up {n}% on September.\n' for n in range(1, 13))
          + '\n| Month | Profit |\n|---|---|\n| Sep | $36,800 |\n| Oct | $41,200 |\n\n'
          + '![Profit by month](coconut-file://4f6c1d1e-6d2b-4a59-9d55-6d3c3f0e1a2b)\n\n'
          + '### Costs\n\nCosts fell on lower fees and fewer refunds. ' * 4)

KEY = document_of(REPORT, 'Task')['key']


class PageRuleTests(unittest.TestCase):
    def test_a_page_with_tokens_and_the_given_libraries_passes(self):
        self.assertEqual(page_problems(TREEMAP), [])
        self.assertEqual(page_problems('<style>#bad { margin: 0 } #add, #fed .x:hover { padding: 0 }</style>'), [])
        self.assertEqual(page_problems('<style>p { color: var(--text) } .red { margin: 0 }</style>'), [])
        # A comment and an address in a script are not colors, and a private field read is not a color.
        self.assertEqual(page_problems('<script>// Fixes #377\nconst total = this.#add + 1\n'
                                       'const link = "https://github.com/antiwork/coconut"\n'
                                       'const label = `${ /* PR #377 */ count } items`</script>'), [])
        # The text of a page is not code.
        self.assertEqual(page_problems('<p>Fixed in PR #377 (see #12, #345). Last fetch: 10:00. The lab (room 2) is '
                                       'red. Use style="color:red" here.</p><a href="#add">Up</a>'), [])
        # A link that the person opens is not a load.
        self.assertEqual(page_problems('<a href="https://github.com/antiwork/coconut/pull/1">PR</a>'), [])

    def test_each_rule_names_its_fix(self):
        cases = {'<p style="font-family: Comic Sans">x</p>': 'font',
                 '<p style="color:#ff0000">x</p>': 'hex value',
                 '<script>bar.attr("fill", "#1D9E75")</script>': 'hex value',
                 '<p style="color: rgb(1, 2, 3)">x</p>': 'function',
                 '<script src="https://cdn.example.com/d3.js"></script>': 'another file',
                 '<link rel="stylesheet" href="x.css">': 'another file',
                 '<img src="https://example.com/a.png">': 'network',
                 '<div style="background: url(//example.com/a.png)"></div>': 'network',
                 '<script>fetch("/data.json")</script>': 'network',
                 '<p style="font: 12px serif">x</p>': 'font',
                 '<style>p { color: red }</style>': 'by name',
                 '<script>bar.style("fill", "steelblue")</script>': 'by name',
                 '<script>const color = d3.scaleOrdinal(d3.schemeTableau10)</script>': 'scheme of d3',
                 '<script>const c = `#fff`</script>': 'hex value',
                 '<rect fill="#ff0000"></rect>': 'color attribute',
                 '<button onclick="this.style.color = \'#000\'">x</button>': 'hex value',
                 '<p style=color:red>x</p>': 'by name',
                 '<style>.card { border: 1px solid #fff }</style>': 'hex value',
                 '<style>@media (min-width: 400px) { .card { box-shadow: 0 0 2px #000 } }</style>': 'hex value',
                 '<div style="background: linear-gradient(#fff, #000)"></div>': 'hex value',
                 '<script>bar.style("border", "1px solid #ccc")</script>': 'hex value',
                 '<svg><image href="https://example.com/a.png"/></svg>': 'network',
                 '<script>Object.assign(document.body.style, {font: "16px serif"})</script>': 'font',
                 '<script>el.style.backgroundImage = "url(https://example.com/chart.png)"</script>': 'network',
                 '<style>.badge::after { content: "}"; color:#fff }</style>': 'hex value',
                 '<script>el.style.color = `\n#fff\n`</script>': 'hex value',
                 '<script>const label = "A // B"; el.style.color = "#fff";</script>': 'hex value',
                 '<script>const label = \'/* x */\'; fetch("/a")</script>': 'network'}
        for page, reason in cases.items():
            with self.subTest(page=page):
                self.assertIn(reason, ' '.join(page_problems(page)))

    def test_a_large_stylesheet_is_read_in_one_pass(self):
        import time
        started = time.perf_counter()
        self.assertEqual(page_problems('<style>' + '#abc, ' * 40000 + '#def { margin: 0 }</style>'), [])
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_an_end_date_is_the_end_of_that_day_and_must_come_within_a_year(self):
        current = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        day = end_time('2026-11-06', current)
        local = datetime.combine(datetime(2026, 11, 6).date(), datetime.max.time()).astimezone()
        self.assertEqual(day, local.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'))
        self.assertEqual(end_time('2026-11-04T09:00:00Z', current), '2026-11-04T09:00:00Z')
        for value, reason in (('2026-11-01', 'future'), ('2028-01-01', 'one year'), ('Friday', 'ISO 8601')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, reason):
                end_time(value, current)


class DocumentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.service.gateway_enabled = lambda: True
        self.bot = self.write('POST', '/v1/bots', {'name': 'Tuner'})
        self.other = self.write('POST', '/v1/bots', {'name': 'Desk'})
        self.turn, self.finish = self.start_turn(self.bot)
        self.task_id = self.task['id']

    def tearDown(self):
        self.finish()
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def start_turn(self, bot):
        """A running gateway turn of the bot, and the function that ends it."""
        task = self.task = self.write('POST', f'/v1/bots/{bot["id"]}/messages', {'text': 'Track the signups.'})
        bridge = self.service.gateway_bridge
        adapter = Adapter(bridge, self.service.profiles.home(bot), [])
        adapter.start()
        runner = threading.Thread(target=self.service._run_task, args=(task['id'],), daemon=True)
        runner.start()
        adapter.join(5)
        turn = adapter.turns[0]['id']

        def finish():
            if runner.is_alive():
                bridge.event({'turn_id': turn, 'kind': 'done', 'outcome': 'success'})
                runner.join(5)
        return turn, finish

    def tool(self, name, arguments, turn=None):
        return self.service.gateway_tool({'turn_id': turn or self.turn, 'tool': name, 'arguments': arguments})

    def save(self, **arguments):
        return self.tool('save_document', dict(dict(key='signups-daily', title='Signups', format='markdown',
                                                    content='# Signups\n\n412 today.'), **arguments))

    def test_a_save_with_the_same_key_replaces_the_document_and_adds_a_version(self):
        self.assertEqual(self.save(summary='412 today')['version'], 1)
        self.assertEqual(self.save(content='# Signups\n\n430 today.', summary='430 today')['version'], 2)
        listed = self.service.documents()['documents']
        self.assertEqual([(d['key'], d['version'], d['summary'], d['bot_id']) for d in listed],
                         [('signups-daily', 2, '430 today', self.bot['id'])])
        self.assertEqual(self.service.document(listed[0]['id'])['content'], '# Signups\n\n430 today.')
        self.assertEqual(self.tool('read_document', {'key': 'signups-daily'})['content'], '# Signups\n\n430 today.')
        self.assertEqual([d['key'] for d in self.tool('read_document', {})['documents']], ['signups-daily'])

    def test_a_bot_keeps_only_its_latest_versions(self):
        for number in range(KEPT_VERSIONS + 3):
            self.save(content=f'Version {number + 1}.')
        kept = self.service.db.execute('SELECT version FROM document_versions ORDER BY version').fetchall()
        self.assertEqual([row[0] for row in kept], list(range(4, KEPT_VERSIONS + 4)))

    def test_coconut_refuses_a_page_that_breaks_the_rules_and_names_the_reason(self):
        with self.assertRaisesRegex(ServiceError, 'Coconut did not save the page. The page sets a font'):
            self.save(format='html', content='<p style="font-family: serif">x</p>')
        for arguments, reason in (({'key': 'Signups Daily'}, 'lowercase'), ({'format': 'pdf'}, 'markdown or html'),
                                  ({'content': 'x' * (512 * 1024 + 1)}, '512 KB'),
                                  ({'expires_at': 'soon'}, 'ISO 8601')):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ServiceError, reason):
                self.save(**arguments)
        self.assertEqual(self.service.documents()['documents'], [])
        self.assertEqual(self.save(format='html', content=TREEMAP)['version'], 1)

    def test_a_cancelled_turn_changes_no_document(self):
        from coconut.service import YieldForDecision
        self.save()
        self.write('POST', f'/v1/tasks/{self.task_id}/cancel')
        for write in (lambda: self.service.save_document(self.task_id, dict(
                key='signups-daily', title='Signups', format='markdown', content='Late.')),
                      lambda: self.service.delete_bot_document(self.task_id, {'key': 'signups-daily'})):
            with self.assertRaises(YieldForDecision):
                write()
        document = self.service.documents()['documents'][0]
        self.assertEqual((document['version'], self.service.document(document['id'])['content']),
                         (1, '# Signups\n\n412 today.'))

    def test_the_key_belongs_to_the_bot(self):
        self.save()
        self.finish()
        turn, finish = self.start_turn(self.other)
        try:
            with self.assertRaisesRegex(ServiceError, 'no document'):
                self.tool('read_document', {'key': 'signups-daily'}, turn)
            with self.assertRaisesRegex(ServiceError, 'no document'):
                self.tool('delete_document', {'key': 'signups-daily'}, turn)
            self.assertEqual(self.tool('save_document', dict(key='signups-daily', title='Desk signups',
                                                             format='markdown', content='x'), turn)['version'], 1)
        finally:
            finish()
        self.assertEqual(sorted(d['title'] for d in self.service.documents()['documents']),
                         ['Desk signups', 'Signups'])

    def test_a_temporary_document_leaves_the_library_after_its_end(self):
        ends = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.assertTrue(self.save(expires_at=ends)['expires_at'])
        # An update without an end date keeps the end. An empty text removes it.
        self.assertTrue(self.save()['expires_at'])
        self.assertIsNone(self.save(expires_at='')['expires_at'])
        self.save(expires_at=ends)
        document = self.service.documents()['documents'][0]
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        self.assertEqual(self.service.documents()['documents'], [])
        with self.assertRaisesRegex(ServiceError, 'ended'):
            self.service.document(document['id'])
        self.assertEqual(self.tool('read_document', {})['documents'], [])

    def test_a_save_brings_back_an_ended_document_and_spaces_remove_an_end(self):
        self.save(expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        self.assertIsNone(self.save()['expires_at'])
        self.assertEqual(len(self.service.documents()['documents']), 1)
        self.save(expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
        self.assertIsNone(self.save(expires_at='   ')['expires_at'])
        self.assertEqual(len(self.service.documents()['documents']), 1)

    def test_the_bot_and_the_phone_delete_a_document(self):
        self.save()
        self.save(key='launch-checklist', title='Launch checklist')
        self.assertEqual(self.tool('delete_document', {'key': 'signups-daily'})['status'], 'deleted')
        remaining = self.service.documents()['documents']
        self.assertEqual([d['key'] for d in remaining], ['launch-checklist'])
        self.assertEqual(self.write('DELETE', f'/v1/documents/{remaining[0]["id"]}'), {'ok': True})
        self.assertEqual(self.write('DELETE', f'/v1/documents/{remaining[0]["id"]}'), {'ok': True})
        self.assertEqual(self.service.documents()['documents'], [])
        self.assertEqual(self.service.db.execute('SELECT count(*) FROM document_versions').fetchone()[0], 0)
        events = [row[0] for row in self.service.db.execute("SELECT type FROM events WHERE type LIKE 'document.%'")]
        self.assertEqual(events, ['document.created', 'document.created', 'document.deleted', 'document.deleted'])

    def test_the_library_leaves_out_the_documents_of_a_deleted_bot(self):
        self.save()
        self.finish()
        self.write('DELETE', f'/v1/bots/{self.bot["id"]}')
        self.assertEqual(self.service.documents()['documents'], [])
        # The rows and their versions go with the bot.
        for table in ('documents', 'document_versions'):
            self.assertEqual(self.service.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)


    def cards(self):
        return [(m['text'], m['document_id']) for m in self.service._task_messages(self.task_id)
                if m['kind'] == 'document']

    def reply(self, text):
        self.service.gateway_bridge.event({'turn_id': self.turn, 'kind': 'send', 'message_id': 'm:1', 'text': text})
        self.finish()

    def test_a_new_document_shows_one_card_in_the_chat(self):
        self.save()
        self.save(content='# Signups\n\n430 today.')
        document = self.service.documents()['documents'][0]
        self.assertEqual(self.cards(), [('Signups', document['id'])])
        card = next(m for m in self.service._task_messages(self.task_id) if m['kind'] == 'document')
        self.assertEqual(card['bot_id'], self.bot['id'])

    def test_a_report_in_the_chat_goes_to_the_library_after_the_reply(self):
        self.reply(REPORT)
        documents = self.service.documents()['documents']
        self.assertEqual([(d['key'], d['title'], d['bot_id']) for d in documents],
                         [(KEY, 'Profit report, October', self.bot['id'])])
        content = self.service.document(documents[0]['id'])['content']
        self.assertTrue(content.startswith('Net profit rose'))
        self.assertIn('![Profit by month](coconut-file://', content)
        messages = [m['kind'] for m in self.service._task_messages(self.task_id) if m['role'] == 'assistant']
        self.assertEqual(messages[-2:], ['text', 'document'])
        self.assertEqual(self.cards(), [('Profit report, October', documents[0]['id'])])

    def test_a_short_answer_or_a_saved_document_adds_no_report(self):
        self.reply('Signups are 412 today, up 9%.')
        self.assertEqual(self.service.documents()['documents'], [])
        self.turn, self.finish = self.start_turn(self.bot)
        self.task_id = self.task['id']
        self.save()
        self.reply(REPORT)
        self.assertEqual([d['key'] for d in self.service.documents()['documents']], ['signups-daily'])

    def test_the_same_report_later_adds_a_version(self):
        self.reply(REPORT)
        self.turn, self.finish = self.start_turn(self.bot)
        self.reply(REPORT.replace('rose 12%', 'rose 14%'))
        documents = self.service.documents()['documents']
        self.assertEqual([(d['key'], d['version']) for d in documents], [(KEY, 2)])


    def test_a_report_with_a_queued_follow_up_goes_to_the_library(self):
        # The person writes for the next turn while this one runs, so the task queues it when the turn ends.
        steered = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages', {'text': 'Then check refunds.', 'mode': 'auto'})
        self.assertEqual(steered['id'], self.task_id)
        self.reply(REPORT)
        self.assertEqual(self.service._get('tasks', self.task_id)['status'], 'queued')
        self.assertEqual([d['key'] for d in self.service.documents()['documents']], [KEY])

    def test_an_update_in_a_later_turn_of_the_task_shows_a_new_card(self):
        self.save()
        task = self.service._get('tasks', self.task_id)
        task['_turn_started_at'] = '9999-01-01T00:00:00Z'
        self.service._put('tasks', task)
        self.save(content='# Signups\n\n430 today.')
        self.assertEqual(len(self.cards()), 2)

    def test_a_report_that_no_person_asked_for_stays_in_the_chat(self):
        # Work intake, a pull request gate or another bot starts the turn: no message of a person is in it.
        for message in self.service._task_messages(self.task_id):
            if message['role'] == 'user':
                message.pop('_human_source_kind', None)
                self.service._put('messages', message)
        self.reply(REPORT)
        self.assertEqual(self.service.documents()['documents'], [])
        self.assertEqual(self.cards(), [])
        # A turn that another bot sent is not a person's request, until a person joins it.
        from coconut.library_replies import asked_by_person
        task = self.service._get('tasks', self.task_id)
        self.assertFalse(asked_by_person(self.service, dict(task, source_bot_id=self.other['id'])))
        message = next(m for m in self.service._task_messages(self.task_id) if m['role'] == 'user')
        message['_human_source_kind'] = 'chat'
        self.service._put('messages', message)
        self.assertTrue(asked_by_person(self.service, dict(task, source_bot_id=self.other['id'],
                                                           _turn_message_ids=[message['id']])))

    def test_a_person_asks_the_bot_about_a_page(self):
        self.save()
        self.finish()
        page = self.service.documents()['documents'][0]
        task = self.write('POST', f'/v1/bots/{self.bot["id"]}/messages',
                          {'text': 'Is this number right?', 'about_document_id': page['id']})
        message = next(m for m in self.service._task_messages(task['id']) if m['role'] == 'user')
        self.assertEqual((message['reply_to']['role'], message['reply_to']['text'], message['reply_to']['document_id']),
                         ('document', 'Signups', page['id']))
        from coconut.native_conversation import message_content
        self.assertIn('About your Library page "Signups" (key signups-daily)', message_content(message))
        # Only a page of this bot.
        with self.assertRaises(ServiceError):
            self.write('POST', f'/v1/bots/{self.other["id"]}/messages',
                       {'text': 'And this?', 'about_document_id': page['id']})

    def test_coconut_removes_once_the_pages_that_no_person_asked_for(self):
        from coconut.library_replies import remove_unasked_reports
        self.reply(REPORT)
        asked = self.service.documents()['documents'][0]
        # A page of build 642 from a turn without a person's message, and its card.
        self.turn, self.finish = self.start_turn(self.bot)
        self.task_id = self.task['id']
        for message in self.service._task_messages(self.task_id):
            if message['role'] == 'user':
                message.pop('_human_source_kind', None)
                self.service._put('messages', message)
        task = self.service._get('tasks', self.task_id)
        with self.service.transaction():
            unasked = self.service._write_document(task, self.bot, 'reply-what-changed-1234abcd', 'What changed',
                                                   'markdown', 'Text.', None, expires=None)
        self.save()
        # A routine later updated the page that a person asked for. It stays.
        task = self.service._get('tasks', self.task_id)
        with self.service.transaction():
            self.service._write_document(task, self.bot, asked['key'], asked['title'], 'markdown', 'Later.', None,
                                         expires=None)
        with self.service.transaction():
            # The service ran the cleanup at its start, before these pages.
            self.service.db.execute("DELETE FROM settings WHERE key='unasked_reports_removed'")
            remove_unasked_reports(self.service)
        self.assertEqual(sorted(d['key'] for d in self.service.documents()['documents']),
                         sorted([asked['key'], 'signups-daily']))
        card = next(m for m in self.service._task_messages(self.task_id) if m.get('document_id') == unasked['id'])
        self.assertTrue(card.get('_hidden'))
        self.assertTrue(self.service._setting('unasked_reports_removed'))

class ReportShapeTests(unittest.TestCase):
    def test_a_report_has_structure_and_length_or_photos(self):
        self.assertTrue(is_report(REPORT))
        self.assertFalse(is_report('Signups are 412 today.'))
        self.assertFalse(is_report('A long answer without structure. ' * 80))
        self.assertTrue(is_report('**Revenue**\n\n' + '- An item with some words in it.\n' * 40))
        self.assertTrue(is_report('## Sales\n\n' + 'Sales rose this week on two launches. ' * 10
                                  + '\n\n![Sales](<coconut-file://a b>)'))

    def test_the_title_comes_from_the_first_heading_or_line(self):
        self.assertEqual(document_of(REPORT, 'Task')['title'], 'Profit report, October')
        self.assertEqual(document_of('**Weekly sales:**\n\nSales rose.', 'Task')['title'], 'Weekly sales')
        plain = document_of('Sales rose on two launches.\n\n- One', 'Task')
        self.assertEqual((plain['title'], plain['key'][:-9]), ('Sales rose on two launches.', 'reply-sales-rose-on-two-launches'))
        self.assertTrue(document_of('# !!!\n\nText', 'Task')['key'].startswith('reply-report-'))
        # The first heading names the report, also after an introduction.
        intro = document_of("Here is today's report.\n\n## Weekly sales\n\nSales rose.", 'Task')
        self.assertEqual(intro['title'], 'Weekly sales')
        # A comment in fenced code is not a heading.
        code = document_of("Deploy notes for the API.\n\n```sh\n# Setup\nmake\n```\n\n- One", 'Task')
        self.assertEqual(code['title'], 'Deploy notes for the API.')
        self.assertFalse(is_report('Run this.\n\n```python\n# Setup\n# Load\n' + 'x = 1\n' * 400 + '```'))
        self.assertTrue(intro['content'].startswith("Here is today's report."))
        # Titles in other scripts, or with the same start, keep their own documents.
        keys = {document_of(f'# {title}\n\nText', 'Task')['key'] for title in
                ('销售报告', '费用分析', 'A' * 60 + ' one', 'A' * 60 + ' two')}
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(len(key) <= 64 for key in keys))


if __name__ == '__main__':
    unittest.main()
