"""Documents of the bots: the page rules, the end dates, the tools of a gateway turn and the routes of the phone."""
from datetime import datetime, timedelta, timezone
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from coconut import documents
from coconut.documents import KEPT_VERSIONS, MAX_CONTENT, end_time, page_problems
from coconut.library_replies import document_of, is_report, report_key
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


def share_with_active_room(service, document: str) -> None:
    """The person shares the document with an active room, as the share route leaves the store. The caller holds the
    transaction."""
    service.team_store.add_room(room='a' * 64, relay='https://relay.example', name='Example Team', member='m',
                                admin='m', state='active', epoch=0, profile='{}', created_at='2026-11-11T00:00:00Z')
    service.db.execute('INSERT INTO team_page_shares (room, document) VALUES (?, ?)', ('a' * 64, document))


class PageRuleTests(unittest.TestCase):
    def test_the_save_tool_names_the_metrics_and_the_record_form_of_a_table(self):
        # page.js makes metrics only of a row of numbers, and names a record by the first cell of its row.
        description = documents.SAVE_SCHEMA['description']
        self.assertIn('a table with one row of numbers shows them large', description)
        self.assertIn('with the first cell as its name and a short last cell at its end', description)
        # The two content rules that the real pages broke most: long headers and a list in a callout.
        self.assertIn('a header of at most 16 characters', description)
        self.assertIn('A callout is only for one decision or one warning, in one or two sentences', description)
        # The recipe in its order, the reference of the skill, and the notes of a save.
        recipe = ['First, one or two sentences', 'Then the key numbers', 'Then sections', 'A callout is only']
        self.assertEqual(sorted(recipe, key=description.index), recipe)
        self.assertIn('references/pages.md', description)
        self.assertIn('The answer of a save can have notes', description)

    def test_the_texts_of_each_session_stay_short(self):
        # Each bot session pays for these texts. BLOCKS had 197 words before the recipe, and may have 20 more.
        from coconut.card_tools import LIBRARY_INSTRUCTIONS
        self.assertLessEqual(len(documents.BLOCKS.split()), 217)
        self.assertLessEqual(len(LIBRARY_INSTRUCTIONS.split()), 368)

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

    def test_the_rules_check_a_hostile_page_of_512_kb_in_a_short_time(self):
        # A team page of another Mac goes through these rules. Each of these pages took more than 30 seconds with
        # a pattern that read to the end of the text again from each start. The limit is wide for a slow CI Mac.
        def fill(start, unit, end=''):
            return start + unit * ((MAX_CONTENT - len(start) - len(end)) // len(unit)) + end
        for page in (fill('<style>p{', 'color:'), fill('<p style="', 'border-top:', '">'),
                     fill('<script>`', '${', '`'), fill('<script>`${', '/*', '}`')):
            started = time.monotonic()
            self.assertEqual(page_problems(page), [])
            self.assertLess(time.monotonic() - started, 10, page[:20])
        # The rule of a color by name keeps its meaning: the value of a color property up to ; { or }.
        self.assertTrue(page_problems('<style>p{border: 1px solid; color: x red}</style>'))
        self.assertEqual(page_problems('<style>p{margin: 0; x: red} .a{color: var(--text)}</style>'), [])
        self.assertTrue(page_problems('<p style="background-color:\n navy">x</p>'))

    def test_html_that_the_parser_cannot_read_is_a_problem_of_the_page(self):
        # Some versions of Python raise AssertionError for this marked section, and others read it.
        self.assertIn(page_problems('<p>x</p><![foo]>'), ([], [documents._UNREADABLE]))
        for error in (AssertionError('unknown status keyword'), ValueError('x')):
            with self.subTest(error=error), patch.object(documents._PageCode, 'feed', side_effect=error):
                self.assertEqual(page_problems('<p>x</p>'), [documents._UNREADABLE])
                with self.assertRaises(ServiceError) as raised:
                    documents.page_to_save(dict(key='page', title='Page', format='html', content='<p>x</p>'))
                self.assertEqual(raised.exception.status, 400)
                self.assertIn('HTML that Coconut cannot read', str(raised.exception))

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

    def test_a_save_answers_with_the_notes_of_the_page_and_saves_it(self):
        weak = 'The shop had 412 orders and 3 refunds.\n\n| Product | Orders |\n|---|---|\n| Ink | 131 |\n| Paint | 168 |'
        self.assertEqual(self.save(content=weak), {'status': 'saved', 'key': 'signups-daily', 'version': 1,
                                                   'expires_at': None, 'notes': [documents.NOTE_METRICS]})
        self.assertEqual(self.tool('read_document', {'key': 'signups-daily'})['content'], weak)
        # A page without a note has no key "notes", and an HTML page gets no notes.
        self.assertNotIn('notes', self.save())
        self.assertNotIn('notes', self.save(format='html', content='<p>412 orders and 3 refunds.</p><table></table>'))

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

    def test_a_bot_cannot_share_a_page_and_a_save_keeps_the_shares(self):
        # Only the person shares a page (test_team_pages.py). Each audience gets the same error, so the bot saves
        # again without it, and the tool has no such argument.
        self.assertNotIn('audience', documents.SAVE_SCHEMA['parameters']['properties'])
        for audience in ('team', 'private', 'everyone', ''):
            with self.subTest(audience=audience), self.assertRaises(ServiceError) as refused:
                self.save(audience=audience)
            self.assertEqual((refused.exception.status, str(refused.exception)), (400, documents.BOT_SHARE))
        self.assertEqual(self.service.documents()['documents'], [])
        self.save()
        self.assertEqual(self.tool('read_document', {})['documents'][0]['audience'], 'private')
        listed = self.service.documents()['documents'][0]
        self.assertEqual((listed['audience'], listed['teams']), ('private', []))
        # A share of the person makes the page a team page. A save of the bot keeps it so.
        with self.service.transaction():
            share_with_active_room(self.service, listed['id'])
        self.assertEqual(self.save(content='# Signups\n\n430 today.')['version'], 2)
        self.assertEqual(self.tool('read_document', {'key': 'signups-daily'})['audience'], 'team')
        self.assertNotIn('audience', json.loads(self.service.db.execute('SELECT data FROM documents').fetchone()[0]))

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

    def pin(self, document, pinned=True):
        return self.write('PATCH', f'/v1/documents/{document["id"]}', {'pinned': pinned})

    def listed(self, key):
        return next(d for d in self.service.documents()['documents'] if d['key'] == key)

    def test_the_person_pins_a_document_and_the_library_shows_it_first(self):
        for key in ('signups-daily', 'launch-checklist', 'weekly-summary'):
            self.save(key=key)

        def keys():
            return [d['key'] for d in self.service.documents()['documents']]
        self.assertEqual(keys(), ['weekly-summary', 'launch-checklist', 'signups-daily'])
        cursor = self.service._cursor()
        oldest = self.listed('signups-daily')
        # A pin is not a change of the page: the version and the update time stay.
        self.assertEqual(self.pin(oldest), dict(oldest, pinned=True))
        self.assertEqual(keys(), ['signups-daily', 'weekly-summary', 'launch-checklist'])
        # The pinned documents have their newest first, as the other documents do.
        self.pin(self.listed('launch-checklist'))
        self.assertEqual(keys(), ['launch-checklist', 'signups-daily', 'weekly-summary'])
        self.assertFalse(self.pin(oldest, False)['pinned'])
        self.assertEqual(keys(), ['launch-checklist', 'weekly-summary', 'signups-daily'])
        # A pin adds no event, so no phone loads its state again.
        self.assertEqual(self.service._cursor(), cursor)

    def test_a_save_and_a_restart_keep_the_pin(self):
        self.save()
        self.pin(self.listed('signups-daily'))
        self.assertEqual(self.save(content='# Signups\n\n430 today.', summary='430 today')['version'], 2)
        document = self.listed('signups-daily')
        self.assertEqual((document['version'], document['summary'], document['pinned']), (2, '430 today', True))
        # The pin belongs to the person. The bot does not read it.
        self.assertNotIn('pinned', self.tool('read_document', {'key': 'signups-daily'}))
        # A save brings back an ended document with its pin.
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        self.save()
        self.assertTrue(self.listed('signups-daily')['pinned'])
        self.finish()
        self.service.close()
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.assertTrue(self.listed('signups-daily')['pinned'])

    def test_a_pin_needs_a_document_of_the_library_and_true_or_false(self):
        self.save()
        document = self.listed('signups-daily')
        for data in ({}, {'pinned': 'yes'}, {'pinned': 1}):
            with self.subTest(data=data), self.assertRaisesRegex(ServiceError, 'true or false'):
                self.write('PATCH', f'/v1/documents/{document["id"]}', data)
        with self.assertRaisesRegex(ServiceError, 'not found'):
            self.pin({'id': 'missing'})
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        with self.assertRaisesRegex(ServiceError, 'ended'):
            self.pin(document)
        self.assertNotIn('pinned', self.service._get('documents', document['id']))

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

    def replays(self, cursor):
        """The update after the cursor for each version of the feed, as the server reads X-Coconut-Updates. Version 1
        is a request without the header. Versions 3 and 4 follow the native outcomes of Hermes."""
        # The feed reads the clock at the time of the last event that the phone has. No time passed, so only the
        # events send rows.
        timestamp = self.service.db.execute('SELECT created_at FROM events WHERE seq=?', (cursor,)).fetchone()[0]
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at=timestamp)), \
             patch('coconut.service.now', return_value=timestamp):
            pages = [self.service.updates(cursor, collection_deltas=version >= 2, native_events=version >= 3,
                                          compact_history=version == 4) for version in (1, 2, 3, 4)]
        for page in pages:
            # A reset sends the full state to each phone.
            self.assertNotIn('reset', page)
            self.assertEqual((page['after'], page['cursor']), (cursor, self.service._cursor()))
            # A document changes no Work row and no card, so the update does not send them again.
            self.assertNotIn('artifacts', page)
            self.assertNotIn('decisions', page)
        return pages

    def test_a_save_in_a_turn_replays_with_its_card(self):
        cursor = self.service._cursor()
        self.save()
        document = self.listed('signups-daily')
        for page in self.replays(cursor):
            self.assertEqual([(m['kind'], m['text'], m['document_id']) for m in page['messages']],
                             [('document', 'Signups', document['id'])])
            self.assertEqual([t['id'] for t in page['tasks']], [self.task_id])
        # An update with a new title changes the card of the turn.
        cursor = self.service._cursor()
        self.save(title='Signups today', content='# Signups\n\n430 today.')
        for page in self.replays(cursor):
            self.assertEqual([(m['kind'], m['text'], m['document_id']) for m in page['messages']],
                             [('document', 'Signups today', document['id'])])
        # An update with the same title changes only the Library.
        cursor = self.service._cursor()
        self.save(title='Signups today', content='# Signups\n\n441 today.')
        for page in self.replays(cursor):
            self.assertEqual((page['messages'], [t['id'] for t in page['tasks']]), ([], [self.task_id]))

    def test_a_delete_of_a_document_replays(self):
        self.save()
        self.save(key='launch-checklist', title='Launch checklist')
        cursor = self.service._cursor()
        self.tool('delete_document', {'key': 'signups-daily'})
        for page in self.replays(cursor):
            self.assertEqual((page['messages'], page['removed_messages']), ([], []))
        cursor = self.service._cursor()
        self.write('DELETE', f'/v1/documents/{self.listed("launch-checklist")["id"]}')
        for page in self.replays(cursor):
            self.assertEqual((page['tasks'], page['messages'], page['removed_messages']), ([], [], []))

    def test_a_kept_report_replays_with_its_reply_and_its_card(self):
        cursor = self.service._cursor()
        self.reply(REPORT)
        with patch.object(self.service, 'kanban', SimpleNamespace(tasks={}, observed_at='2099-01-01T00:00:00Z')):
            page = self.service.updates(cursor, collection_deltas=True, native_events=True, compact_history=True)
        self.assertNotIn('reset', page)
        self.assertEqual([m['kind'] for m in page['messages']], ['text', 'document'])

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
            unasked = self.service._write_document(task, self.bot, report_key('What changed'), 'What changed',
                                                   'markdown', 'Text.', None, expires=None)
            # A bot saved its own page with a key that starts with reply- in the same turn. It stays.
            self.service._write_document(task, self.bot, 'reply-notes', 'Reply notes', 'markdown', 'Text.', None,
                                         expires=None)
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
                         sorted([asked['key'], 'reply-notes', 'signups-daily']))
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


class GatewayPageTests(unittest.TestCase):
    """The tooling of the Mac keeps a page of a bot, without a turn and without a card in the chat."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.service.gateway_enabled = lambda: True
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.home = str(self.service.profiles.home(self.bot))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def publish(self, **arguments):
        page = dict(home=self.home, key='weekly-report', title='Weekly report', format='markdown',
                    content='3 tasks are open.', summary='3 open tasks')
        return self.service.gateway_document(dict(page, **arguments))

    # These helpers of the turn tests need only the service and `write`, so the tests of the tooling use them too.
    pin, listed, replays = DocumentServiceTests.pin, DocumentServiceTests.listed, DocumentServiceTests.replays

    def rows(self, table):
        return self.service.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_the_tooling_keeps_a_page_of_a_bot_without_a_turn(self):
        self.assertEqual(self.publish(), {'status': 'saved', 'key': 'weekly-report', 'version': 1,
                                          'expires_at': None})
        documents = self.service.documents()['documents']
        self.assertEqual([(d['key'], d['title'], d['bot_id'], d['version']) for d in documents],
                         [('weekly-report', 'Weekly report', self.bot['id'], 1)])
        self.assertEqual(self.service.document(documents[0]['id'])['content'],
                         '3 tasks are open.')
        # No turn asked for the page, so the chat shows nothing.
        self.assertEqual((self.service._all('tasks'), self.service._all('messages')), ([], []))

    def test_a_save_that_changes_nothing_writes_nothing(self):
        self.publish()
        self.assertEqual(self.publish()['version'], 1)
        self.assertEqual(self.rows('document_versions'), 1)
        self.assertEqual(self.publish(content='4 tasks are open.')['version'], 2)

    def test_a_save_that_changes_nothing_adds_no_event_and_keeps_the_update_time(self):
        self.publish()
        # An old update time shows a write.
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.updated_at','2026-01-01T00:00:00Z')")

        def stored():
            return self.listed('weekly-report'), self.rows('document_versions'), self.service._cursor()
        before = stored()
        self.assertEqual(self.publish(), {'status': 'unchanged', 'key': 'weekly-report', 'version': 1,
                                          'expires_at': None})
        self.assertEqual(stored(), before)

    def test_only_a_new_end_date_is_a_change(self):
        self.publish()
        # A new end date is a change, also when the content is the same.
        ends = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        saved = self.publish(expires_at=ends)
        self.assertEqual(saved, {'status': 'saved', 'key': 'weekly-report', 'version': 2,
                                 'expires_at': end_time(ends, datetime.now(timezone.utc))})
        # The same end date again, or no end date, keeps the page as it is.
        for arguments in ({'expires_at': ends}, {}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.publish(**arguments), dict(saved, status='unchanged'))
        # A date ends at the end of its day, so the same date gives the same end.
        day = (datetime.now() + timedelta(days=3)).date().isoformat()
        self.assertEqual(self.publish(expires_at=day)['version'], 3)
        self.assertEqual(self.publish(expires_at=day)['status'], 'unchanged')
        # An empty text removes the end date one time.
        self.assertEqual(self.publish(expires_at=''), {'status': 'saved', 'key': 'weekly-report', 'version': 4,
                                                       'expires_at': None})
        self.assertEqual(self.publish(expires_at='')['status'], 'unchanged')
        self.assertEqual(self.rows('document_versions'), 4)

    def test_a_save_of_the_tooling_keeps_the_pin(self):
        self.publish()
        self.pin(self.listed('weekly-report'))
        self.assertEqual(self.publish()['status'], 'unchanged')
        self.assertTrue(self.listed('weekly-report')['pinned'])
        self.assertEqual(self.publish(content='4 tasks are open.')['status'], 'saved')
        page = self.listed('weekly-report')
        self.assertEqual((page['version'], page['pinned']), (2, True))

    def test_a_save_of_the_tooling_replays_without_a_task_and_a_card(self):
        for content in ('3 tasks are open.', '4 tasks are open.'):
            cursor = self.service._cursor()
            self.assertEqual(self.publish(content=content)['status'], 'saved')
            self.assertEqual(self.service._cursor(), cursor + 1)
            # No turn asked for the page, so its event sends no task and no message. The phone reads the Library.
            for page in self.replays(cursor):
                self.assertEqual((page['tasks'], page['messages'], page['removed_messages']), ([], [], []))

    def test_an_archived_a_deleted_or_an_unknown_bot_keeps_no_page(self):
        page = dict(key='weekly-report', title='Weekly report', format='markdown', content='3 tasks are open.')
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True})
        with self.assertRaisesRegex(ServiceError, 'archived or deleted') as refused:
            self.publish()
        self.assertEqual(refused.exception.status, 409)
        # The route finds no profile for a deleted bot (test_gateway_document_route.py). A bot that goes after the
        # route found its profile is refused in the transaction of the save.
        self.write('DELETE', f'/v1/bots/{self.bot["id"]}')
        with self.assertRaisesRegex(ServiceError, 'archived or deleted'):
            self.service.publish_page(self.bot['id'], page)
        with self.assertRaisesRegex(ServiceError, 'Bot was not found'):
            self.service.publish_page('no-such-bot', page)
        self.assertEqual(self.rows('documents'), 0)

    def test_a_delete_of_the_tooling_refuses_a_bot_that_goes_after_the_route_found_it(self):
        # The route finds no profile for a deleted bot (test_gateway_document_route.py). A bot that goes after the
        # route found its profile is refused in the transaction of the delete, and its key is checked first.
        self.publish()
        self.write('DELETE', f'/v1/bots/{self.bot["id"]}')
        with self.assertRaisesRegex(ServiceError, 'archived or deleted') as refused:
            self.service.unpublish_page(self.bot['id'], {'key': 'weekly-report'})
        self.assertEqual(refused.exception.status, 409)
        with self.assertRaisesRegex(ServiceError, 'lowercase') as refused:
            self.service.unpublish_page(self.bot['id'], {'key': 'Weekly Report'})
        self.assertEqual(refused.exception.status, 400)
        with self.assertRaisesRegex(ServiceError, 'Bot was not found'):
            self.service.unpublish_page('no-such-bot', {'key': 'weekly-report'})

    def test_a_page_of_the_tooling_comes_back_when_it_has_ended(self):
        self.publish(expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
        self.service.db.execute("UPDATE documents SET data=json_set(data,'$.expires_at','2026-01-01T00:00:00Z')")
        self.assertEqual(self.service.documents()['documents'], [])
        # The same content brings it back: an ended page is never what the tooling would write, and its old end
        # date would hide it again at once.
        self.assertEqual(self.publish(), {'status': 'saved', 'key': 'weekly-report', 'version': 2,
                                          'expires_at': None})
        self.assertEqual([d['key'] for d in self.service.documents()['documents']], ['weekly-report'])

    def test_a_save_of_the_tooling_answers_with_the_notes_of_the_page(self):
        weak = 'The shop had 412 orders and 3 refunds.\n\n| Product | Orders |\n|---|---|\n| Ink | 131 |\n| Paint | 168 |'
        expected = {'key': 'weekly-report', 'version': 1, 'expires_at': None, 'notes': [documents.NOTE_METRICS]}
        self.assertEqual(self.publish(content=weak), dict(expected, status='saved'))
        # A script on a timer saves the same page again. The answer still names what to improve.
        self.assertEqual(self.publish(content=weak), dict(expected, status='unchanged'))
        # A delete answers with no notes.
        self.assertEqual(self.service.unpublish_page(self.bot['id'], {'key': 'weekly-report'}),
                         {'status': 'deleted', 'key': 'weekly-report'})
        self.assertEqual(self.publish(content=weak), dict(expected, status='saved'))
        self.assertEqual(self.publish(), {'status': 'saved', 'key': 'weekly-report', 'version': 2,
                                          'expires_at': None})
        self.assertEqual(self.publish(), {'status': 'unchanged', 'key': 'weekly-report', 'version': 2,
                                          'expires_at': None})

    def test_a_home_that_is_not_the_profile_of_a_bot_keeps_no_page(self):
        with self.assertRaisesRegex(ServiceError, 'not the profile of a bot'):
            self.publish(home=f'{self.temp.name}/somewhere-else')

    def test_a_page_that_the_tooling_keeps_follows_the_page_rules(self):
        for arguments, reason in (({'key': 'Weekly Report'}, 'lowercase'), ({'format': 'pdf'}, 'markdown or html'),
                                  ({'content': ' '}, 'empty'), ({'content': 'x' * (512 * 1024 + 1)}, '512 KB'),
                                  ({'expires_at': 'soon'}, 'ISO 8601')):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ServiceError, reason):
                self.publish(**arguments)
        with self.assertRaisesRegex(ServiceError, 'did not save the page. The page sets a font'):
            self.publish(format='html', content='<p style="font-family: serif">x</p>')
        self.assertEqual(self.service.documents()['documents'], [])


class OutsideTurnPageTests(unittest.TestCase):
    """The document tools in a conversation of a bot that is not a Coconut chat turn, for example a chat of another
    messaging app. The plugin names the Hermes home of the running profile. A save goes to POST /v1/gateway/document,
    and a read and a delete go to POST /v1/gateway/tools with the home in place of a turn."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, Provider(), start_workers=False)
        self.service.gateway_enabled = lambda: True
        self.bot = self.write('POST', '/v1/bots', {'name': 'Ada'})
        self.other = self.write('POST', '/v1/bots', {'name': 'Lin'})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def write(self, method, path, data=None):
        return self.service.mutate(method, path, data or {}, str(uuid4()), 'phone')

    def home(self, bot):
        return str(self.service.profiles.home(bot))

    def save(self, bot, **arguments):
        page = dict(key='weekly-report', title='Weekly report', format='markdown', content='3 tasks are open.')
        return self.service.gateway_document(dict(page, **arguments, home=self.home(bot)))

    def tool(self, bot, name, arguments=None, home=None):
        return self.service.gateway_tool({'home': home or self.home(bot), 'tool': name, 'arguments': arguments or {}})

    def test_each_bot_reads_and_deletes_only_its_own_pages(self):
        self.save(self.bot, content='Ada has 3 open tasks.')
        self.save(self.other, content='Lin has 5 open tasks.')
        for bot, text in ((self.bot, 'Ada has 3 open tasks.'), (self.other, 'Lin has 5 open tasks.')):
            with self.subTest(bot=bot['name']):
                listed = self.tool(bot, 'read_document')['documents']
                self.assertEqual([(d['key'], d['version'], d['audience']) for d in listed],
                                 [('weekly-report', 1, 'private')])
                self.assertEqual(self.tool(bot, 'read_document', {'key': 'weekly-report'})['content'], text)
        cursor = self.service._cursor()
        self.assertEqual(self.tool(self.bot, 'delete_document', {'key': 'weekly-report'}),
                         {'status': 'deleted', 'key': 'weekly-report'})
        # The page of the other bot with the same key stays.
        self.assertEqual([(d['bot_id'], d['key']) for d in self.service.documents()['documents']],
                         [(self.other['id'], 'weekly-report')])
        with self.assertRaisesRegex(ServiceError, 'no document with this key') as missing:
            self.tool(self.bot, 'read_document', {'key': 'weekly-report'})
        self.assertEqual(missing.exception.status, 404)
        with self.assertRaisesRegex(ServiceError, 'no document with this key'):
            self.tool(self.bot, 'delete_document', {'key': 'weekly-report'})
        # No turn asked for the delete, so its event names no task, as a delete from the phone.
        (event,) = self.service.db.execute('SELECT type, task_id FROM events WHERE seq>?', (cursor,)).fetchall()
        self.assertEqual(event, ('document.deleted', None))

    def test_a_save_outside_a_turn_shows_no_card_and_writes_no_message(self):
        self.assertEqual(self.save(self.bot)['status'], 'saved')
        self.assertEqual(self.save(self.bot, content='4 tasks are open.')['version'], 2)
        self.assertEqual((self.service._all('tasks'), self.service._all('messages')), ([], []))

    def test_a_bot_outside_a_turn_cannot_share_a_page(self):
        with self.assertRaises(ServiceError) as refused:
            self.save(self.bot, audience='team')
        self.assertEqual((refused.exception.status, str(refused.exception)), (400, documents.BOT_SHARE))
        self.save(self.bot)
        document = self.service._document_of_key(self.bot['id'], 'weekly-report')
        with self.service.transaction():
            share_with_active_room(self.service, document['id'])
        # A new version of a shared page wakes the sync, which sends it to the team.
        with patch.object(self.service, 'team_wake') as wake:
            self.assertEqual(self.save(self.bot, content='4 tasks are open.')['status'], 'saved')
        wake.assert_called()
        self.assertEqual(self.tool(self.bot, 'read_document', {'key': 'weekly-report'})['audience'], 'team')

    def test_a_home_that_is_not_the_profile_of_a_bot_gets_a_clear_refusal(self):
        for name, arguments in (('read_document', {}), ('delete_document', {'key': 'weekly-report'})):
            with self.subTest(tool=name), self.assertRaisesRegex(ServiceError, 'not the profile of a bot') as refused:
                self.tool(None, name, arguments, home=f'{self.temp.name}/somewhere-else')
            self.assertEqual(refused.exception.status, 404)

    def test_the_other_tools_and_a_save_refuse_without_a_turn(self):
        # A save without a turn goes to POST /v1/gateway/document, which follows the rules of a page of the tooling.
        for name in ('ask', 'track_pr', 'message_bot', 'kanban_list', 'save_document', 'nonsense'):
            with self.subTest(tool=name), self.assertRaisesRegex(ServiceError, 'only during a Coconut chat') as refused:
                self.tool(self.bot, name, {'key': 'weekly-report'})
            self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.service._all('decisions'), [])

    def test_an_archived_bot_deletes_no_page(self):
        self.save(self.bot)
        self.write('PATCH', f'/v1/bots/{self.bot["id"]}', {'archived': True})
        with self.assertRaisesRegex(ServiceError, 'archived or deleted'):
            self.tool(self.bot, 'delete_document', {'key': 'weekly-report'})
        self.assertEqual(len(self.service.documents()['documents']), 1)


if __name__ == '__main__':
    unittest.main()
