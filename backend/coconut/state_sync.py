"""Read current content separately from native settings and older history."""
import time

# A person who opens the settings gets a live look at the model lists of the providers, at most this often.
MODEL_REFRESH_SECONDS = 300
# A search answers with this many messages, the newest first.
SEARCH_MESSAGES = 30


def search_snippet(text, start, before=40, after=160):
    """A line of the text around a match: whole words, the spaces collapsed, with an ellipsis where it is cut."""
    begin, end = max(0, start - before), min(len(text), max(start, 0) + after)
    if begin:
        space = text.find(' ', begin, max(start, begin))
        begin = space + 1 if space >= 0 else begin
    if end < len(text):
        space = text.rfind(' ', max(start, 0) + 1, end)
        end = space if space > 0 else end
    line = ' '.join(text[begin:end].split())
    return ('…' if begin else '') + line + ('…' if end < len(text) else '')


def work_items(cards):
    """Group the Work cards of one item: the copies of a Work item or of a pull request.

    The key is the Work outcome, or the URL of a pull request.
    """
    parent = {}
    def root(key):
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    keys = {}
    for card in cards:
        outcome = card.get('work_outcome_id')
        keys[card['id']] = (['work:' + (outcome or card['id'])] if card['kind'] == 'work' or outcome else []) + (
            [card['url'].lower()] if card.get('url') else []) or [card['id']]
        for key in keys[card['id']][1:]:
            parent[root(key)] = root(keys[card['id']][0])
    items = {}
    for card in cards:
        items.setdefault(root(keys[card['id']][0]), []).append(card)
    return list(items.values())


def finished(item):
    """Return True when every card of a Work item finished."""
    return all(card.get('work_state') in ('completed', 'cancelled') for card in item)


# An exchange row can appear in the conversation of its sender and of each recipient, besides its own bot.
EXCHANGE = "(json_extract({data},'$.kind')='bot_exchange' OR json_array_length({data},'$.exchange_recipient_bot_ids')>0)"


def exchange_inserts(source, data, position):
    """The statements that list each exchange row of `source` for its sender and for each of its recipients."""
    exchange = EXCHANGE.format(data=data)
    return ([f"INSERT OR IGNORE INTO message_exchange_audience SELECT json_extract({data},'$.sender_bot_id'), {position} "
             f"FROM {source} WHERE json_type({data},'$.sender_bot_id')='text' AND {exchange}"]
            + [f"INSERT OR IGNORE INTO message_exchange_audience SELECT j.value, {position} FROM {source}, "
               f"json_each({data},'$.{field}') j WHERE j.type='text' AND {exchange}"
               for field in ('recipient_bot_ids', 'exchange_recipient_bot_ids')])


NEW_EXCHANGE = ''.join(statement + ';\n' for statement in exchange_inserts('(SELECT 1)', 'NEW.data', 'NEW.rowid'))
# Service.__init__ creates these. message_exchange_audience lists, for each bot, the positions (rowids) of the exchange
# rows that its conversation shows, in rowid order. So a page of one bot reads only its newest exchange rows, without a
# sort. Triggers keep the list with the messages. A full VACUUM can renumber rowids, so maintenance builds it again.
# messages_client_request_bot holds only the messages with a confirmed request, each with its request and its bot.
# CONFIRMED_REQUESTS reads both from the index and opens no message. The index before it held only the request, so
# the read opened each message for its bot: 40 ms for 14,000 sent messages.
READ_INDEXES = f"""
DROP INDEX IF EXISTS messages_client_request;
CREATE INDEX IF NOT EXISTS messages_client_request_bot ON messages
    (json_extract(data,'$.client_request_id'), json_extract(data,'$.bot_id'))
    WHERE json_type(data,'$.client_request_id')='text';
CREATE TABLE IF NOT EXISTS message_exchange_audience (
    bot_id TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY (bot_id, position)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS message_exchange_audience_position ON message_exchange_audience (position);
CREATE TRIGGER IF NOT EXISTS message_exchange_insert AFTER INSERT ON messages BEGIN
{NEW_EXCHANGE}END;
CREATE TRIGGER IF NOT EXISTS message_exchange_update AFTER UPDATE OF data ON messages
WHEN json_extract(OLD.data,'$.kind') IS NOT json_extract(NEW.data,'$.kind')
  OR json_extract(OLD.data,'$.sender_bot_id') IS NOT json_extract(NEW.data,'$.sender_bot_id')
  OR json_extract(OLD.data,'$.recipient_bot_ids') IS NOT json_extract(NEW.data,'$.recipient_bot_ids')
  OR json_extract(OLD.data,'$.exchange_recipient_bot_ids') IS NOT json_extract(NEW.data,'$.exchange_recipient_bot_ids')
BEGIN
DELETE FROM message_exchange_audience WHERE position=OLD.rowid;
{NEW_EXCHANGE}END;
CREATE TRIGGER IF NOT EXISTS message_exchange_delete AFTER DELETE ON messages BEGIN
DELETE FROM message_exchange_audience WHERE position=OLD.rowid;
END;
"""


# The requests of the stored messages of the bots that exist, each once, in the order of the index. The inner SELECT
# names the two expressions of messages_client_request_bot, so SQLite takes their values from the index. As the
# argument of json_group_array, SQLite read the request from the message.
CONFIRMED_REQUESTS = (
    "SELECT json_group_array(request) FROM (SELECT DISTINCT json_extract(data,'$.client_request_id') AS request "
    "FROM messages WHERE json_type(data,'$.client_request_id')='text' AND EXISTS(SELECT 1 FROM bots "
    "WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0)))")


def index_exchange_audience(db, rebuild=False):
    """Fill message_exchange_audience from the stored messages once, or again after a full VACUUM."""
    if not rebuild and db.execute("SELECT 1 FROM settings WHERE key='message_exchange_audience_indexed'").fetchone():
        return
    db.execute('DELETE FROM message_exchange_audience')
    for statement in exchange_inserts('messages m', 'm.data', 'm.rowid'):
        db.execute(statement)
    db.execute("INSERT OR REPLACE INTO settings VALUES ('message_exchange_audience_indexed','true')")


class StateSync:
    @staticmethod
    def _visible(row, visible, task_ids=None):
        """Return True when the phone shows a row: its bots exist, it is not hidden, and its task is visible."""
        return (row['bot_id'] in visible and row.get('conversation_bot_id', row['bot_id']) in visible
                and not row.get('_deleted') and not row.get('_hidden')
                and (task_ids is None or not row.get('task_id') or row['task_id'] in task_ids))

    def _visible_task_ids(self, visible, among=None):
        """Return the IDs of the tasks whose bots exist, from all tasks or from the IDs in `among`."""
        import json
        # The unary plus keeps SQLite from the tasks_bot index, so a list in `among` reads its rows by ID.
        query = ("SELECT id FROM tasks WHERE +json_extract(data,'$.bot_id') IN (SELECT value FROM json_each(?)) "
                 "AND json_extract(data,'$.conversation_bot_id') IN (SELECT value FROM json_each(?))")
        values = (json.dumps(sorted(visible)),) * 2
        if among is not None:
            query += ' AND id IN (SELECT value FROM json_each(?))'
            values += (json.dumps(sorted(among)),)
        return {row[0] for row in self.db.execute(query, values)}

    def _current_work(self, artifacts, messages):
        """Leave out finished Work, except the cards that the messages show."""
        shown = {m.get('artifact_id') for m in messages}
        left_out = {card['id'] for item in work_items([a for a in artifacts if a['kind'] in ('work', 'pr')])
                    if finished(item) and not any(c['id'] in shown for c in item) for card in item}
        return [a for a in artifacts if a['id'] not in left_out]

    def _recent_messages(self, visible, limit=60):
        messages, before = {}, {}
        for bot_id in sorted(visible):
            page = self._message_page(bot_id, limit=limit)
            messages.update((m['id'], m) for m in page['messages'])
            if page['before'] is not None:
                before[bot_id] = page['before']
        pending = [row[0] for row in self.db.execute(
            "SELECT id FROM decisions WHERE json_extract(data,'$.status')='pending' "
            "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")]
        messages.update((m['id'], m) for m in self._card_messages(pending, visible))
        return sorted(messages.values(), key=lambda m: (m['created_at'], m['id'])), before

    def _card_messages(self, identities, visible):
        import json
        if not identities:
            return []
        row = self.db.execute("SELECT json_group_array(json(data)) FROM (SELECT data FROM messages WHERE "
            "json_extract(data,'$.decision_id') IN (SELECT value FROM json_each(?)) "
            "AND json_extract(data,'$.bot_id') IN (SELECT value FROM json_each(?)) "
            "AND json_extract(data,'$.kind')='text' AND NOT COALESCE(json_extract(data,'$._hidden'),0) "
            "AND NOT COALESCE(json_extract(data,'$._deleted'),0) ORDER BY rowid)",
            (json.dumps(identities), json.dumps(sorted(visible)))).fetchone()
        return [self._public(m) for m in json.loads(row[0])]

    def _message_page(self, bot_id, *, before=None, limit=60):
        import json
        from .service import ServiceError
        bot = self._get('bots', bot_id)
        if bot.get('_deleted'):
            raise ServiceError(404, 'This agent was removed.')
        # Decision rows stay in their stored conversation. Exchange rows can appear for both bots.
        # Row IDs give pagination a stable boundary when timestamps are equal.
        # The bot's own rows come from the messages_chat index, and its exchange rows from message_exchange_audience.
        # Both are in rowid order for one bot, so each part stops at the newest limit + 1 rows without a sort.
        visible = "AND NOT COALESCE(json_extract(m.data,'$._hidden'),0) AND NOT COALESCE(json_extract(m.data,'$._deleted'),0) "
        row = self.db.execute("SELECT json_group_array(json_array(position,json(data))) FROM (SELECT rowid AS position,data "
            "FROM messages WHERE rowid IN (SELECT position FROM (SELECT m.rowid AS position FROM messages m "
            "WHERE json_extract(m.data,'$.bot_id')=:bot AND (:before IS NULL OR m.rowid<:before) " + visible +
            "ORDER BY m.rowid DESC LIMIT :rows) "
            "UNION ALL SELECT position FROM (SELECT a.position FROM message_exchange_audience a JOIN messages m ON m.rowid=a.position "
            "WHERE a.bot_id=:bot AND (:before IS NULL OR a.position<:before) " + visible +
            "AND EXISTS(SELECT 1 FROM bots WHERE id=json_extract(m.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0)) "
            "ORDER BY a.position DESC LIMIT :rows)) ORDER BY rowid DESC LIMIT :rows)",
            dict(bot=bot_id, before=before, rows=limit + 1)).fetchone()
        rows = json.loads(row[0])
        page = rows[:limit]
        return {'messages': [self._public(row[1]) for row in reversed(page)],
                'before': page[-1][0] if len(rows) > limit else None}

    def _page_decisions(self, messages, *, pending=False):
        import json
        identities = sorted({m['decision_id'] for m in messages if m.get('decision_id')})
        rows = [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM decisions WHERE id IN (SELECT value FROM json_each(?)) "
            "OR (? AND json_extract(data,'$.status')='pending') ORDER BY rowid", (json.dumps(identities), pending))]
        # Keep the recorded earlier choice available without loading unrelated cards.
        selected = {d['id']: d for d in rows}
        for decision in rows:
            previous = decision.get('previous_decision_id')
            if previous and previous not in selected:
                row = self._read_one('SELECT data FROM decisions WHERE id=?', (previous,))
                if row:
                    value = json.loads(row[0])
                    selected[previous] = value
                    rows.append(value)
        return rows

    def message_page(self, bot_id, before=None):
        with self._read_view() as view, view._read_transaction():
            page = view._message_page(bot_id, before=before)
            import json
            visible = {b['id'] for b in view._all('bots') if not b.get('_deleted')}
            page['decisions'] = [view._public(d) for d in view._page_decisions(page['messages'])
                if not d.get('_hidden') and not d.get('_deleted') and d['bot_id'] in visible
                and view._get('tasks', d['task_id']).get('conversation_bot_id', d['bot_id']) in visible]
            identities = {m.get('task_id') for m in page['messages'] + page['decisions']} - {None}
            page['tasks'] = [view._public(json.loads(row[0])) for row in view.db.execute(
                'SELECT data FROM tasks WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(sorted(identities)),))]
            # Older messages can show cards that the recent state leaves out.
            cards = [json.loads(row[0]) for row in view.db.execute(
                'SELECT data FROM artifacts WHERE id IN (SELECT value FROM json_each(?)) ORDER BY rowid',
                (json.dumps(sorted({m['artifact_id'] for m in page['messages'] if m.get('artifact_id')})),))]
            task_ids = view._visible_task_ids(visible, {c['task_id'] for c in cards if c.get('task_id')}) if cards else set()
            page['artifacts'] = [view._public(a) for a in view._work_projection([c for c in cards if view._visible(c, visible, task_ids)])]
            return page

    def search_conversations(self, query):
        """Find the text messages that hold every word of the query.

        The answer names every bot with a match, for apps older than the omnibar, and the newest matches with a
        line of text around the first word.
        """
        words = query.strip()[:200].casefold().split()
        if not words:
            return {'bot_ids': [], 'messages': []}
        with self._read_view() as view, view._read_transaction():
            visible = {b['id'] for b in view._all('bots') if not b.get('_deleted')}
            # SQLite gives only the fields of the answer. A read of every message as JSON took 125 ms for 28,000.
            hits = []
            for identity, bot_id, role, created_at, text in view.db.execute(
                    "SELECT id, json_extract(data,'$.bot_id'), json_extract(data,'$.role'), json_extract(data,'$.created_at'), "
                    "json_extract(data,'$.text') FROM messages WHERE json_extract(data,'$.kind')='text' "
                    "AND NOT COALESCE(json_extract(data,'$._hidden'),0) AND NOT COALESCE(json_extract(data,'$._deleted'),0)"):
                folded = (text or '').casefold()
                if bot_id in visible and all(word in folded for word in words):
                    hits.append((created_at or '', identity, bot_id, role, text, folded.find(words[0])))
            hits.sort(reverse=True)
            return {'bot_ids': sorted({hit[2] for hit in hits}),
                    'messages': [dict(id=identity, bot_id=bot_id, role=role, created_at=created_at, snippet=search_snippet(text, start))
                                 for created_at, identity, bot_id, role, text, start in hits[:SEARCH_MESSAGES]]}

    def card(self, identity):
        from .service import ServiceError, now
        with self._read_view() as view, view._read_transaction():
            decision = view._get('decisions', identity)
            task = view._get('tasks', decision['task_id'])
            bot = view._get('bots', task.get('conversation_bot_id') or decision['bot_id'])
            owner = view._get('bots', decision['bot_id'])
            if decision.get('_hidden') or decision.get('_deleted') or bot.get('_deleted') or owner.get('_deleted'):
                raise ServiceError(404, 'This card was removed.')
            public = view._public(decision)
            public['reply_pending'] = any(c['decision_id'] == identity and c['status'] in ('queued', 'working') and not c.get('_decision_refresh')
                                          for c in view._all('contexts'))
            if not public.get('request_text'):
                public.update(view.decision_origin(task))
            visible = {b['id'] for b in view._all('bots') if not b.get('_deleted')}
            related = [view._public(d) for d in view._page_decisions([{'decision_id': identity}])
                if not d.get('_hidden') and not d.get('_deleted') and d['bot_id'] in visible
                and view._get('tasks', d['task_id']).get('conversation_bot_id', d['bot_id']) in visible]
            related = [public if d['id'] == identity else d for d in related]
            owners = {d['task_id']: view._public(view._get('tasks', d['task_id'])) for d in related}
            return dict(decision=public, decisions=related, tasks=list(owners.values()),
                        task=view._public(task), bot=view._public(bot),
                        owner=view._public(owner), cursor=view._cursor(), server_time=now(),
                        messages=view._card_messages([identity], {b['id'] for b in view._all('bots') if not b.get('_deleted')}))

    def native_snapshot(self):
        with self._read_view() as view:
            for _ in range(3):
                cursor = view._cursor()
                bots = [b for b in view._all('bots') if not b.get('_deleted')]
                # The app asks for these settings when a person opens them. That is the time to look for new models.
                catalog = view._model_catalog(refresh_after=MODEL_REFRESH_SECONDS)
                if hasattr(view.provider, 'observe_state'):
                    observed, native = view.provider.observe_state(bots)
                else:
                    observed = (view.provider.observe_bots(bots) if hasattr(view.provider, 'observe_bots')
                                else {b['id']: dict(b, instructions=view.profiles.read_instructions(b)) for b in bots})
                    native = view.provider.native_settings() if hasattr(view.provider, 'native_settings') else {}
                    native = dict(native or {})
                    if hasattr(view.provider, 'native_settings'):
                        native['routines'] = view.provider.native_settings('routines_read', bots)['routines']
                with view._read_transaction():
                    if view.db.execute("SELECT 1 FROM events WHERE seq>? AND "
                        "(type LIKE 'settings.%' OR type IN ('bot.created','bot.deleted') OR type LIKE 'routine.%' OR type='model_catalog.updated') LIMIT 1", (cursor,)).fetchone():
                        continue
                    visible = {b['id'] for b in bots}
                    stored = [r for r in view._all('routines') if r['bot_id'] in visible and not r.get('_deleted')]
                    previous = {r['id']: r.get('last_task_id') for r in stored}
                    native = dict(native)
                    if native.get('routines') is None:
                        native['routines'] = stored
                    if native.get('routines') is not None:
                        native['routines'] = [dict(r) for r in native['routines']
                                              if r['bot_id'] in visible and not r.get('_deleted')]
                        # The settings read Hermes directly. The pause of a routine whose profile no gateway serves
                        # shows here too (RoutineCoordinator._pause_unserved_routines).
                        view._pause_unserved_routines(native['routines'])
                        native['routines'] = [dict(view._public(r), last_task_id=previous.get(r['id']))
                                              for r in native['routines']]
                    return dict(cursor=view._cursor(), push_configured=self.push.configured, auto_archive_hours=view.auto_archive_hours(), bots=[view._bot_view(dict(b, **observed.get(b['id'], {})), catalog) for b in bots],
                                model_catalog=catalog, model_configured=bool(catalog and catalog.get('default_configured')), **{k: native.get(k) for k in ('approvals', 'parallel_work', 'fallback', 'mcp_servers', 'routines')})
        from .service import ServiceError
        raise ServiceError(503, 'Settings changed during this request. Try again.')

    def updates(self, after, wait=0, *, collection_deltas=False, native_events=False, compact_history=False, recent_work=False):
        from .service import ServiceError
        if after < 0:
            raise ServiceError(400, 'The event cursor must be zero or greater.')
        deadline = time.monotonic() + min(max(wait, 0), 25)
        # An app that reads the updates shows the business too, so GitHub and Gmail are watched for the next minute.
        watch = getattr(self, 'business_watch', None)
        if watch is not None:
            watch.watched()
        with self._read_view() as view:
            # The number is read before the cursor, so a commit between the read of the cursor and the wait ends the wait.
            seen = self.commits.count
            while view._cursor() == after and not self.stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                seen = self.commits.wait(seen, min(remaining, .1))
            with view._read_transaction():
                result = view._replay_changes(after, collection_deltas=collection_deltas, native_events=native_events)
            if result is not None:
                return result
            state = view._snapshot(content_only=True, compact_history=compact_history, recent_work=recent_work)
            if compact_history:
                # Every app of update version 4 keeps the routines of its native settings over a reset (AppStore.apply).
                del state['routines']
            return dict(after=after, cursor=state['cursor'], server_time=state['server_time'], reset=state)

    def _replay_changes(self, after, *, collection_deltas=False, native_events=False):
        import json
        from .service import now
        cursor = self._cursor()
        rows = self.db.execute('SELECT seq,type,task_id,data FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT 1001',
                               (after, cursor)).fetchall()
        known = set("""message.created message.updated task.created task.updated task.cancelled task.recovered task.steered
            decision.created decision.updated decision.answered artifact.created artifact.updated artifact.observed context.queued context.cancelled
            context.completed context.failed execution.started execution.completed execution.uncertain work.updated work.recovery_queued
            bot.read bot.updated bot.settings_pending bot_message.delivered bots.addressed settings.updated settings.pending
            settings.update_unconfirmed model_catalog.updated routine.created routine.updated routine.deleted routine.triggered
            routine.reconciliation_required routines.unavailable google.updated github.updated device.paired maintenance.withdrawal_corrected
            document.created document.updated document.deleted team.updated team_messages.updated
            team_directs.updated team_bot_directs.updated team_typing.updated activity.updated business.updated""".split())
        # The state holds no document, because the phone reads the Library with GET /v1/documents. A document event
        # sends only its task. The save of a turn also adds the message of its card, which has its own event.
        if (after > cursor or len(rows) > 1000 or len(rows) != cursor - after
                or any(row[0] != after + index + 1 for index, row in enumerate(rows))
                or any(r[1] not in known for r in rows)):
            return None
        from datetime import datetime
        server_time = now()
        native = native_events and self.kanban is not None and getattr(self.provider, 'supports_native_outcomes', False)
        previous = self._read_one('SELECT created_at FROM events WHERE seq=?', (after,))
        new_interval = (previous is None or
            int(datetime.fromisoformat(previous[0].replace('Z', '+00:00')).timestamp() // 15) !=
            int(datetime.fromisoformat(server_time.replace('Z', '+00:00')).timestamp() // 15))
        # Only retained legacy work changes with time alone. Within one interval, a native update does not look for it.
        retained = self._retained_work_observations() if native and new_interval else []
        refresh_activity = new_interval and (not native or bool(retained))
        if not rows and collection_deltas and (native or not refresh_activity):
            result = dict(after=after, cursor=cursor, server_time=server_time, collection_deltas=True)
            if refresh_activity:
                result['artifacts'] = [self._public(a) for a in self._work_projection(retained)]
            return result
        bots = [self._bot_view(b, None) for b in self._all('bots') if not b.get('_deleted')]
        visible = {b['id'] for b in bots}
        result = dict(after=after, cursor=cursor, server_time=server_time, bots=bots)
        task_ids, message_ids, decision_ids, artifact_ids = set(), set(), set(), set()
        # A task's status shows in the attempts of its work. Only task events change it.
        changed_task_ids = set()
        native_work_ids = set() if native and collection_deltas else None
        # Time alone changes only retained legacy work. Native rows change through events.
        timed_ids = {a['id'] for a in retained} if refresh_activity else set()
        # Decision and Work item events change the cards of some outcomes. _changed_work finds them.
        work_events = []
        if collection_deltas:
            result['collection_deltas'] = True
        for _, kind, task_id, data in rows:
            value = json.loads(data)
            if native_work_ids is not None:
                if kind == 'work.updated' and isinstance(value.get('native_task_ids'), list):
                    native_work_ids.update(value['native_task_ids'])
                elif kind.startswith(('decision.', 'artifact.')) and kind != 'artifact.observed':
                    # New check times (artifact.observed) change only their own row.
                    work_events.append((kind, task_id, value))
                elif kind.startswith('work.'):
                    native_work_ids = None
            if task_id:
                task_ids.add(task_id)
            if kind.startswith('task.'):
                changed_task_ids.update(identity for identity in (task_id, value.get('id')) if identity)
            if kind.startswith('task.') and value.get('id'):
                task_ids.add(value['id'])
            if kind.startswith('message.') and value.get('id'):
                message_ids.add(value['id'])
            if kind.startswith('decision.') and value.get('id'):
                decision_ids.add(value['id'])
            if value.get('decision_id'):
                decision_ids.add(value['decision_id'])
            if kind.startswith('artifact.') and value.get('id'):
                artifact_ids.add(value['id'])
            if kind.startswith('context.') and value.get('id'):
                context = self._read_one('SELECT data FROM contexts WHERE id=?', (value['id'],))
                if context:
                    decision_ids.add(json.loads(context[0])['decision_id'])
        def visible_row(value):
            return (value and not value.get('_hidden') and not value.get('_deleted')
                    and value.get('bot_id') in visible and value.get('conversation_bot_id', value.get('bot_id')) in visible)
        stored_tasks = {}
        def stored_task(identity):
            # An update reads the tasks that it names or that own its rows, not the whole task history.
            if identity not in stored_tasks:
                row = self._read_one('SELECT data FROM tasks WHERE id=?', (identity,))
                stored_tasks[identity] = json.loads(row[0]) if row else None
            return stored_tasks[identity]
        def visible_owned(value):
            return visible_row(value) and (not value.get('task_id') or visible_row(stored_task(value['task_id'])))
        if rows:
            tasks = []
            for identity in sorted(task_ids):
                value = stored_task(identity)
                if identity in changed_task_ids and value and native_work_ids is not None and value.get('_native_outcome_id'):
                    native_work_ids.add(value['_native_outcome_id'])
                if visible_row(value):
                    tasks.append(self._public(value))
            messages, removed = [], []
            for identity in sorted(message_ids):
                row = self._read_one('SELECT data FROM messages WHERE id=?', (identity,))
                value = json.loads(row[0]) if row else None
                if not visible_row(value):
                    removed.append(identity)
                else:
                    messages.append(self._public(value))
            result.update(tasks=tasks, messages=sorted(messages, key=lambda m: (m['created_at'], m['id'])), removed_messages=removed,
                          settings_changed=any(r[1].startswith(('settings.', 'model_catalog.', 'routine.', 'routines.')) for r in rows),
                          # The Library reads GET /v1/documents again: a local document or a team page changed.
                          documents_changed=any(r[1].startswith('document.') for r in rows),
                          # The Team screen reads GET /v1/team/rooms again: a room, a member or a team bot changed.
                          team_changed=any(r[1] == 'team.updated' for r in rows),
                          # The chat of each of these rooms changed: a message, an edit, a removal or the read position.
                          team_messages_changed=sorted({json.loads(r[3])['team'] for r in rows
                                                        if r[1] == 'team_messages.updated'}),
                          # The direct chats that changed, each as its room and its other member.
                          team_directs_changed=[{'team': team, 'member': member} for team, member in sorted(
                              {(value['team'], value['member']) for value in
                               (json.loads(r[3]) for r in rows if r[1] == 'team_directs.updated')})],
                          # The direct chats with a bot that changed, each as its room and its team bot ID.
                          team_bot_directs_changed=[{'team': team, 'bot': bot} for team, bot in sorted(
                              {(value['team'], value['bot']) for value in
                               (json.loads(r[3]) for r in rows if r[1] == 'team_bot_directs.updated')})],
                          # The rooms where who types changed: a person or a bot started, typed on, or ended.
                          team_typing_changed=sorted({json.loads(r[3])['team'] for r in rows
                                                      if r[1] == 'team_typing.updated'}),
                          # The tasks whose live activity changed (turn_activity.py). An app with the live panel of
                          # such a task open reads GET /v1/tasks/<task>/activity again.
                          activity_changed=sorted({json.loads(r[3])['task'] for r in rows
                                                   if r[1] == 'activity.updated'}),
                          # What changed on GitHub or in Gmail (business_watch.py): issues, reports or mail. An app reads
                          # the pages of the business that it shows again.
                          business_changed=sorted({source for r in rows if r[1] == 'business.updated'
                                                   for source in json.loads(r[3]).get('sources', [])}))
            if any(m.get('client_request_id') for m in messages):
                receipts = self.db.execute(CONFIRMED_REQUESTS).fetchone()
                result['confirmed_request_ids'] = json.loads(receipts[0])
        # Native task changes publish work.updated. Age alone changes no native state. A document event, a team event
        # or a change on GitHub or in Gmail changes no Work row and no card.
        structural = not rows or refresh_activity or any(r[1] not in ('message.created', 'message.updated', 'bot.read', 'bot.updated',
            'document.created', 'document.updated', 'document.deleted', 'team.updated', 'team_messages.updated',
            'team_directs.updated', 'team_bot_directs.updated', 'team_typing.updated', 'activity.updated',
            'business.updated')
            for r in rows)
        if structural:
            work_outcomes = set()
            if not collection_deltas:
                stored_artifacts = self._all('artifacts')
            else:
                # Read the changed rows, and the Work rows only when some outcomes changed. A task event changes the
                # outcomes of its task, so it reads them only when that task has a row.
                stored = {}
                task_work = changed_task_ids and self.db.execute(
                    "SELECT 1 FROM artifacts WHERE json_extract(data,'$.task_id') IN (SELECT value FROM json_each(?)) LIMIT 1",
                    (json.dumps(sorted(changed_task_ids)),)).fetchone()
                if native_work_ids is None or native_work_ids or work_events or task_work:
                    stored.update((row[0], json.loads(row[1])) for row in self.db.execute(
                        "SELECT rowid,data FROM artifacts WHERE json_extract(data,'$.kind') IN ('work','pr')"))
                if work_events and native_work_ids is not None:
                    changed = self._changed_work(work_events, list(stored.values()), after)
                    if changed is None:
                        native_work_ids = None
                    else:
                        natives, work_outcomes = changed
                        native_work_ids |= natives
                stored.update((row[0], json.loads(row[1])) for row in self.db.execute(
                    'SELECT rowid,data FROM artifacts WHERE id IN (SELECT value FROM json_each(?))',
                    (json.dumps(sorted(artifact_ids | timed_ids)),)))
                stored_artifacts = [stored[rowid] for rowid in sorted(stored)]
            outcomes = {}
            def changed_artifact(artifact):
                if artifact['id'] in artifact_ids or artifact['id'] in timed_ids or not collection_deltas:
                    return True
                if artifact['kind'] not in ('pr', 'work'):
                    return False
                if native_work_ids is None:
                    return True
                # Rows with one work_outcome_id share their outcome.
                key = artifact.get('work_outcome_id') or artifact['id']
                if key not in outcomes:
                    outcome = self._work_outcome_artifact(artifact)
                    outcomes[key] = (outcome.get('_native_outcome_id') in native_work_ids or outcome['id'] in work_outcomes
                                     or outcome.get('task_id') in changed_task_ids)
                return outcomes[key]
            # The cheap test comes first, so an update reads the tasks of the rows that it sends.
            artifacts = [a for a in stored_artifacts if changed_artifact(a) and visible_owned(a)]
            if collection_deltas:
                present = {a['id'] for a in stored_artifacts if a['id'] in artifact_ids and visible_owned(a)}
                removed = artifact_ids - present
                # The phone holds only rows that were visible. A row leaves through its own event or through its task.
                hidden_tasks = [identity for identity in task_ids if not visible_row(stored_task(identity))]
                if hidden_tasks:
                    removed.update(row[0] for row in self.db.execute(
                        "SELECT id FROM artifacts WHERE json_extract(data,'$.task_id') IN (SELECT value FROM json_each(?))",
                        (json.dumps(hidden_tasks),)))
                result['removed_artifacts'] = sorted(removed)
            result['artifacts'] = [self._public(a) for a in self._work_projection(artifacts)]
            result.update(self._work_limit_state())
            if rows:
                replying = {c['decision_id'] for c in (json.loads(row[0]) for row in self.db.execute(
                    "SELECT data FROM contexts WHERE json_extract(data,'$.status') IN ('queued','working')"))
                    if not c.get('_decision_refresh')}
                decisions, removed_decisions = [], set(decision_ids)
                stored_decisions = self._all('decisions') if not collection_deltas else [json.loads(row[0]) for row in self.db.execute(
                    "SELECT data FROM decisions WHERE id IN (SELECT value FROM json_each(?)) "
                    "OR json_extract(data,'$.task_id') IN (SELECT value FROM json_each(?)) ORDER BY rowid",
                    (json.dumps(list(decision_ids)), json.dumps(list(task_ids))))]
                for decision in stored_decisions:
                    if not visible_owned(decision):
                        removed_decisions.add(decision['id'])
                        continue
                    removed_decisions.discard(decision['id'])
                    decision = self._public(decision)
                    decision['reply_pending'] = decision['id'] in replying
                    if not decision.get('request_text') and decision.get('task_id'):
                        decision.update(self.decision_origin(stored_task(decision['task_id'])))
                    decisions.append(decision)
                included = {d['id'] for d in decisions}
                for earlier in self._page_decisions([{'decision_id': identity} for identity in included]):
                    if earlier['id'] in included or not visible_owned(earlier):
                        continue
                    previous = self._public(earlier)
                    previous['reply_pending'] = earlier['id'] in replying
                    previous.update(self.decision_origin(stored_task(earlier['task_id'])))
                    decisions.append(previous)
                    included.add(earlier['id'])
                    removed_decisions.discard(earlier['id'])
                    if not any(t['id'] == earlier['task_id'] for t in result['tasks']):
                        result['tasks'].append(self._public(stored_task(earlier['task_id'])))
                result['decisions'] = decisions
                if collection_deltas:
                    result['removed_decisions'] = sorted(removed_decisions)
        return result

    def _retained_work_observations(self):
        import json
        result = []
        visible = {row[0] for row in self.db.execute(
            "SELECT id FROM bots WHERE NOT coalesce(json_extract(data,'$._deleted'),0)")}
        # A row without links is its own outcome. SQLite skips such rows when they have a native task, or when they are
        # merged or closed pull requests. Most other rows link to an outcome with work_outcome_id, so each is checked once.
        finished = {}
        for row in self.db.execute("SELECT data FROM artifacts WHERE json_extract(data,'$.kind') IN ('work','pr') "
                "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0) "
                # IS never returns NULL, so a missing field cannot hide a row.
                "AND NOT (json_extract(data,'$.work_outcome_id') IS NULL AND json_extract(data,'$._work_replaced_by') IS NULL "
                "AND json_extract(data,'$._same_outcome_as') IS NULL AND ((json_type(data,'$._native_outcome_id') IS 'text' "
                "AND json_extract(data,'$._native_outcome_id')<>'') OR (json_extract(data,'$.kind') IS 'pr' "
                "AND (json_extract(data,'$.status') IS 'merged' OR json_extract(data,'$.pr_state') IS 'merged' "
                "OR json_extract(data,'$.pr_state') IS 'closed'))))"):
            original = json.loads(row[0])
            if original['bot_id'] not in visible:
                continue
            # Rows of native or finished outcomes stop here, before their task is read.
            key = original.get('work_outcome_id') or original['id']
            if key not in finished:
                item = self._work_outcome_artifact(original)
                finished[key] = bool(item.get('_native_outcome_id') or self._retained_terminal_work(item))
            if finished[key]:
                continue
            owner = self._get('tasks', original['task_id']) if original.get('task_id') else None
            if owner and owner.get('conversation_bot_id', owner['bot_id']) not in visible:
                continue
            result.append(original)
        return result

    def _changed_work(self, events, items, after):
        """Find the Work cards that decision and Work item events can change.

        Return the native task IDs and the outcome row IDs of those cards, or None when every card must be sent again.
        A card belongs to the outcome that _work_outcome_artifact finds. _native_work_view shows the first pending
        decision of the outcome: a decision of a task with the same native task, of the root task of the outcome, or for
        the URL of one of its pull requests. A changed row can also leave an outcome, so the last version that an event
        recorded before this update counts too.
        """
        import json
        by_id = {item['id']: item for item in items}
        by_task, by_url, by_pause = {}, {}, {}
        for item in items:
            by_task.setdefault(item.get('task_id'), []).append(item)
            by_pause.setdefault(item.get('paused_by_decision_id'), []).append(item)
            if item['kind'] == 'pr':
                by_url.setdefault(item.get('url'), []).append(item)
        def outcome(item):
            return self._work_outcome_artifact(item)['id']
        def stored(table, identity):
            row = self._read_one(f'SELECT data FROM {table} WHERE id=?', (identity,))
            return json.loads(row[0]) if row else None
        changed = {'decision': {}, 'artifact': {}}
        for kind, task_id, value in events:
            table = kind.split('.')[0]
            if kind == 'decision.answered' and task_id:
                # The answer names no decision. Every decision of its task counts.
                for row in self.db.execute("SELECT id FROM decisions WHERE json_extract(data,'$.task_id')=?", (task_id,)):
                    changed[table].setdefault(row[0], (task_id, []))
            elif value.get('id') and task_id:
                changed[table].setdefault(value['id'], (task_id, []))[1].append(value)
            else:
                return None
        created = {value.get('id') for kind, _, value in events if kind in ('decision.created', 'artifact.created')}
        def versions(table, identity, task_id, values, current):
            # The events of this update, the stored row, and the version of the last earlier event.
            result = [*values, current]
            if identity not in created:
                # Events usually carry the task ID of their row. When the task index finds no earlier version of a
                # changed row, the row cannot be followed, so every card is sent.
                row = self._read_one("SELECT data FROM events WHERE task_id=? AND seq<=? AND type LIKE ? "
                                     "AND json_extract(data,'$.id')=? ORDER BY seq DESC LIMIT 1",
                                     (task_id, after, table + '.%', identity))
                if row is None and values:
                    return None
                result.append(json.loads(row[0]) if row else None)
            return [v for v in result if v]
        natives, outcomes, decisions = set(), set(), []
        for identity, (task_id, values) in changed['decision'].items():
            found = versions('decision', identity, task_id, values, stored('decisions', identity))
            if found is None:
                return None
            decisions.extend(found)
            outcomes.update(item['id'] for item in by_pause.get(identity, []))
        for decision in decisions:
            if decision.get('task_id'):
                task = stored('tasks', decision['task_id'])
                if task and task.get('_native_outcome_id'):
                    natives.add(task['_native_outcome_id'])
                outcomes.update(item['id'] for item in by_task.get(decision['task_id'], []))
            if decision.get('merge_pr_url'):
                outcomes.update(outcome(item) for item in by_url.get(decision['merge_pr_url'], []))
        urls = {row[0] for row in self.db.execute(
            "SELECT json_extract(data,'$.merge_pr_url') FROM decisions WHERE json_extract(data,'$.status')='pending' "
            "AND NOT coalesce(json_extract(data,'$._hidden'),0) AND NOT coalesce(json_extract(data,'$._deleted'),0)")}
        urls.update(d.get('merge_pr_url') for d in decisions)
        for identity, (task_id, values) in changed['artifact'].items():
            current = by_id.get(identity)
            outcomes.add(identity)
            if current:
                outcomes.add(outcome(current))
            # A pull request that left an outcome can leave its pending merge decision on that outcome's card.
            if not any(v and v.get('kind') == 'pr' and v.get('url') and v['url'] in urls for v in [*values, current]):
                continue
            found = versions('artifact', identity, task_id, values, current)
            if found is None:
                return None
            outcomes.update(outcome({'work_outcome_id': v['work_outcome_id']}) for v in found if v.get('work_outcome_id'))
        return natives, outcomes
