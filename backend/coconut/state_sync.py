"""Read current content separately from native settings and older history."""
import time


class StateSync:
    def _recent_messages(self, visible, limit=60):
        messages, before = {}, {}
        for bot_id in sorted(visible):
            page = self._message_page(bot_id, limit=limit)
            messages.update((m['id'], m) for m in page['messages'])
            if page['before'] is not None:
                before[bot_id] = page['before']
        pending = [d['id'] for d in self._all('decisions') if d.get('status') == 'pending' and not d.get('_hidden') and not d.get('_deleted')]
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
            raise ServiceError(404, 'This bot was removed.')
        # Decision rows stay in their stored conversation. Exchange rows can appear for both bots.
        # Row IDs give pagination a stable boundary when timestamps are equal.
        row = self.db.execute("SELECT json_group_array(json_array(position,json(data))) FROM (SELECT rowid AS position,data FROM messages WHERE (? IS NULL OR rowid<?) "
            "AND EXISTS(SELECT 1 FROM bots WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0)) "
            "AND NOT COALESCE(json_extract(data,'$._hidden'),0) "
            "AND NOT COALESCE(json_extract(data,'$._deleted'),0) "
            "AND (json_extract(data,'$.bot_id')=? OR ((json_extract(data,'$.kind')='bot_exchange' "
            "OR json_array_length(data,'$.exchange_recipient_bot_ids')>0) AND ("
            "json_extract(data,'$.sender_bot_id')=? OR EXISTS(SELECT 1 FROM json_each(messages.data,'$.recipient_bot_ids') WHERE value=?) "
            "OR EXISTS(SELECT 1 FROM json_each(messages.data,'$.exchange_recipient_bot_ids') WHERE value=?)))) "
            "ORDER BY rowid DESC LIMIT ?)", (before, before, bot_id, bot_id, bot_id, bot_id, limit + 1)).fetchone()
        rows = json.loads(row[0])
        page = rows[:limit]
        return {'messages': [self._public(row[1]) for row in reversed(page)],
                'before': page[-1][0] if len(rows) > limit else None}

    def message_page(self, bot_id, before=None):
        with self._read_view() as view, view._read_transaction():
            page = view._message_page(bot_id, before=before)
            identities = {m.get('task_id') for m in page['messages']}
            page['tasks'] = [view._public(t) for t in view._all('tasks') if t['id'] in identities]
            return page

    def search_conversations(self, query):
        query = query.strip()[:200]
        if not query:
            return {'bot_ids': []}
        with self._read_view() as view, view._read_transaction():
            visible = {b['id'] for b in view._all('bots') if not b.get('_deleted')}
            return {'bot_ids': sorted({m['bot_id'] for m in view._all('messages')
                if m['bot_id'] in visible and not m.get('_hidden') and not m.get('_deleted')
                and m['kind'] == 'text' and query.casefold() in m['text'].casefold()})}

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
            return dict(decision=public, task=view._public(task), bot=view._public(bot),
                        owner=view._public(owner), cursor=view._cursor(), server_time=now(),
                        messages=view._card_messages([identity], {b['id'] for b in view._all('bots') if not b.get('_deleted')}))

    def native_snapshot(self):
        with self._read_view() as view:
            for _ in range(3):
                cursor = view._cursor()
                bots = [b for b in view._all('bots') if not b.get('_deleted')]
                catalog = view._model_catalog()
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
                        native['routines'] = [dict(view._public(r), last_task_id=previous.get(r['id']))
                                              for r in native['routines'] if r['bot_id'] in visible and not r.get('_deleted')]
                    return dict(cursor=view._cursor(), push_configured=self.push.configured, bots=[view._bot_view(dict(b, **observed.get(b['id'], {})), catalog) for b in bots],
                                model_catalog=catalog, model_configured=bool(catalog and catalog.get('default_configured')), **{k: native.get(k) for k in ('approvals', 'parallel_work', 'mcp_servers', 'routines')})
        from .service import ServiceError
        raise ServiceError(503, 'Settings changed during this request. Try again.')

    def updates(self, after, wait=0):
        from .service import ServiceError, now
        if after < 0:
            raise ServiceError(400, 'The event cursor must be zero or greater.')
        deadline = time.monotonic() + min(max(wait, 0), 25)
        with self._read_view() as view:
            while view._cursor() == after and not self.stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.stopping.wait(min(remaining, .1))
            return view._snapshot(content_only=True, after=after)

    def _state_changes(self, state, after):
        import json
        rows = self.db.execute('SELECT seq,type,task_id,data FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT 1001',
                               (after, state['cursor'])).fetchall()
        if after == state['cursor']:
            return dict(after=after, cursor=after, server_time=state['server_time'], bots=state['bots'],
                        artifacts=state['artifacts'], work_capacity=state['work_capacity'])
        known = set("""message.created message.updated task.created task.updated task.cancelled task.recovered task.steered
            decision.created decision.updated decision.answered artifact.created artifact.updated context.queued context.cancelled
            context.completed context.failed execution.started execution.completed execution.uncertain work.updated work.recovery_queued
            bot.read bot.updated bot.settings_pending bot_message.delivered bots.addressed settings.updated settings.pending
            settings.update_unconfirmed model_catalog.updated routine.created routine.updated routine.deleted routine.triggered
            routine.reconciliation_required routines.unavailable google.updated device.paired""".split())
        if (after > state['cursor'] or len(rows) > 1000 or len(rows) != state['cursor'] - after
                or any(row[0] != after + index + 1 for index, row in enumerate(rows))
                or any(r[1] not in known for r in rows)):
            return dict(after=after, cursor=state['cursor'], server_time=state['server_time'], reset=state)
        task_ids, message_ids = set(), set()
        for _, kind, task_id, data in rows:
            value = json.loads(data)
            if task_id:
                task_ids.add(task_id)
            if kind.startswith('task.') and value.get('id'):
                task_ids.add(value['id'])
            if kind.startswith('message.') and value.get('id'):
                message_ids.add(value['id'])
        messages, removed = [], []
        visible = {b['id'] for b in state['bots']}
        for identity in sorted(message_ids):
            row = self._read_one('SELECT data FROM messages WHERE id=?', (identity,))
            value = json.loads(row[0]) if row else None
            if value is None or value.get('_hidden') or value.get('_deleted') or value['bot_id'] not in visible:
                removed.append(identity)
            else:
                messages.append(self._public(value))
        return dict(after=after, cursor=state['cursor'], server_time=state['server_time'], bots=state['bots'],
                    tasks=[t for t in state['tasks'] if t['id'] in task_ids], messages=sorted(messages, key=lambda m: (m['created_at'], m['id'])), removed_messages=removed,
                    decisions=state['decisions'], artifacts=state['artifacts'], work_capacity=state['work_capacity'],
                    confirmed_request_ids=state['confirmed_request_ids'],
                    settings_changed=any(r[1].startswith(('settings.', 'model_catalog.', 'routine.', 'routines.')) for r in rows))
