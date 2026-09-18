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
                                model_catalog=catalog, model_configured=bool(catalog and catalog.get('default_configured')), **{k: native.get(k) for k in ('approvals', 'parallel_work', 'api_fallback', 'mcp_servers', 'routines')})
        from .service import ServiceError
        raise ServiceError(503, 'Settings changed during this request. Try again.')

    def updates(self, after, wait=0, *, collection_deltas=False, native_events=False):
        from .service import ServiceError
        if after < 0:
            raise ServiceError(400, 'The event cursor must be zero or greater.')
        deadline = time.monotonic() + min(max(wait, 0), 25)
        with self._read_view() as view:
            while view._cursor() == after and not self.stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.stopping.wait(min(remaining, .1))
            with view._read_transaction():
                result = view._replay_changes(after, collection_deltas=collection_deltas, native_events=native_events)
            if result is not None:
                return result
            state = view._snapshot(content_only=True)
            return dict(after=after, cursor=state['cursor'], server_time=state['server_time'], reset=state)

    def _replay_changes(self, after, *, collection_deltas=False, native_events=False):
        import json
        from .service import now
        from .work_capacity import state as capacity_state
        cursor = self._cursor()
        rows = self.db.execute('SELECT seq,type,task_id,data FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT 1001',
                               (after, cursor)).fetchall()
        known = set("""message.created message.updated task.created task.updated task.cancelled task.recovered task.steered
            decision.created decision.updated decision.answered artifact.created artifact.updated context.queued context.cancelled
            context.completed context.failed execution.started execution.completed execution.uncertain work.updated work.recovery_queued
            bot.read bot.updated bot.settings_pending bot_message.delivered bots.addressed settings.updated settings.pending
            settings.update_unconfirmed model_catalog.updated routine.created routine.updated routine.deleted routine.triggered
            routine.reconciliation_required routines.unavailable google.updated device.paired""".split())
        if (after > cursor or len(rows) > 1000 or len(rows) != cursor - after
                or any(row[0] != after + index + 1 for index, row in enumerate(rows))
                or any(r[1] not in known for r in rows)):
            return None
        from datetime import datetime
        server_time = now()
        native = native_events and self.kanban is not None and getattr(self.provider, 'supports_native_outcomes', False)
        previous = None if native else self._read_one('SELECT created_at FROM events WHERE seq=?', (after,))
        refresh_activity = not native and (previous is None or
            int(datetime.fromisoformat(previous[0].replace('Z', '+00:00')).timestamp() // 15) !=
            int(datetime.fromisoformat(server_time.replace('Z', '+00:00')).timestamp() // 15))
        freshness = {'native_work_observed_at': self.kanban.observed_at} if native else {}
        if not rows and collection_deltas and not refresh_activity:
            return dict(after=after, cursor=cursor, server_time=server_time, collection_deltas=True, **freshness)
        bots = [self._bot_view(b, None) for b in self._all('bots') if not b.get('_deleted')]
        visible = {b['id'] for b in bots}
        result = dict(after=after, cursor=cursor, server_time=server_time, bots=bots, **freshness)
        task_ids, message_ids, decision_ids, artifact_ids = set(), set(), set(), set()
        if collection_deltas:
            result['collection_deltas'] = True
        for _, kind, task_id, data in rows:
            value = json.loads(data)
            if task_id:
                task_ids.add(task_id)
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
        if rows:
            tasks = []
            for identity in sorted(task_ids):
                row = self._read_one('SELECT data FROM tasks WHERE id=?', (identity,))
                value = json.loads(row[0]) if row else None
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
                          settings_changed=any(r[1].startswith(('settings.', 'model_catalog.', 'routine.', 'routines.')) for r in rows))
            if any(m.get('client_request_id') for m in messages):
                receipts = self.db.execute("SELECT json_group_array(DISTINCT json_extract(data,'$.client_request_id')) FROM messages "
                    "WHERE json_type(data,'$.client_request_id')='text' AND EXISTS(SELECT 1 FROM bots "
                    "WHERE id=json_extract(messages.data,'$.bot_id') AND NOT COALESCE(json_extract(bots.data,'$._deleted'),0))").fetchone()
                result['confirmed_request_ids'] = json.loads(receipts[0])
        # Native task changes publish work.updated. Age alone changes no native state.
        structural = not rows or refresh_activity or any(r[1] not in ('message.created', 'message.updated', 'bot.read', 'bot.updated') for r in rows)
        if structural:
            tasks = {t['id']: t for t in self._all('tasks') if visible_row(t)}
            def visible_owned(value):
                return visible_row(value) and (not value.get('task_id') or value['task_id'] in tasks)
            stored_artifacts = self._all('artifacts')
            artifacts = [a for a in stored_artifacts if visible_owned(a)
                         and (not collection_deltas or a['kind'] in ('pr', 'work') or a['id'] in artifact_ids)]
            if collection_deltas:
                present = {a['id'] for a in stored_artifacts if visible_owned(a)}
                result['removed_artifacts'] = sorted((artifact_ids - present) |
                    {a['id'] for a in stored_artifacts if not visible_owned(a)})
            result['artifacts'] = [self._public(a) for a in self._work_projection(artifacts)]
            result['work_capacity'] = capacity_state(self._setting('work_capacity'))
            if rows:
                replying = {c['decision_id'] for c in self._all('contexts') if c['status'] in ('queued', 'working') and not c.get('_decision_refresh')}
                decisions, removed_decisions = [], set(decision_ids)
                for decision in self._all('decisions'):
                    if collection_deltas and decision['id'] not in decision_ids and decision['task_id'] not in task_ids:
                        continue
                    if not visible_owned(decision):
                        removed_decisions.add(decision['id'])
                        continue
                    removed_decisions.discard(decision['id'])
                    decision = self._public(decision)
                    decision['reply_pending'] = decision['id'] in replying
                    if not decision.get('request_text') and decision.get('task_id'):
                        decision.update(self.decision_origin(tasks[decision['task_id']]))
                    decisions.append(decision)
                result['decisions'] = decisions
                if collection_deltas:
                    result['removed_decisions'] = sorted(removed_decisions)
        return result
