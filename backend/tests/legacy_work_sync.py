"""The legacy builder of automatic Work rows, for tests only.

Production never runs it. With Hermes, a Work row comes from a native outcome, and Service._sync_automatic_work
returned at once. Fixture mode and many tests still build their rows this way, so the code lives here. Import this
module in a test that calls Service._sync_automatic_work.
"""
from datetime import datetime, timezone
import json
import re

from coconut.service import Service


class LegacyWorkSync:
    def _work_title(self, task):
        parent = self._get('tasks', task['parent_task_id']) if task.get('parent_task_id') else None
        fallback = parent['title'] if parent and parent['status'] == 'failed' else task['title']
        return next((todo['content'] for todo in task.get('_work_todos', {}).get('todos', [])
                     if todo.get('id') == 'coconut-assignment' and todo.get('content')
                     and len(todo['content']) <= 120), fallback)

    def _sync_followup_work(self, tasks, artifacts):
        decisions = self._all('decisions')
        for item in sorted(artifacts, key=lambda a: (a['created_at'], a['id'])):
            if (item['kind'] != 'work' or item.get('_hidden') or item.get('_automatic_work')
                    or item.get('task_id') not in tasks or not item.get('_work_key', '').startswith('task-')
                    or item['_work_key'][5:] not in tasks):
                continue
            previous = self._work_for_followup(tasks[item['task_id']], artifacts, tasks, decisions)
            if not previous or previous['id'] == item['id']:
                continue
            # Keep the later report's ID so its links and any existing Undo still work.
            self._carry_work_history(item, previous)
            item['_work_previous_task_ids'] = list(dict.fromkeys(item['_work_previous_task_ids']
                + self._followup_task_ids(tasks[item['task_id']], decisions)))
            previous.update(_hidden=True, _work_replaced_by=item['id'])
            self._put('artifacts', previous)
            self._put('artifacts', item)
            self._event('artifact.updated', dict(self._public(previous), _hidden=True), previous['bot_id'], previous['task_id'])
            self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])

    def _resume_tracked_work(self, tasks, artifacts):
        """Keep a failed request's row when its owner continues the work."""
        from coconut.service import now
        for task in sorted(tasks.values(), key=lambda t: (t['created_at'], t['id'])):
            parent = tasks.get(task.get('parent_task_id'))
            targeted = parent and self._continued_work_task(parent, tasks) is task
            if (not parent or parent['status'] != 'failed'
                    or (not targeted and (task.get('source_bot_id') or task.get('_work_context_id')))
                    or task.get('_assignment_task_id')
                    or task['thread_id'] != parent['thread_id']
                    or any(task.get(k) != parent.get(k) for k in ('bot_id', 'conversation_bot_id', 'session_id'))):
                continue
            bot = self._get('bots', task['bot_id'])
            unstarted = task['status'] == 'queued' and '_conversation_epoch' not in task
            if (bot.get('_deleted') or bot.get('archived') or task['session_id'] != bot['session_id']
                    or parent.get('_conversation_epoch') != self._setting('conversation_epoch')
                    or (not unstarted and task.get('_conversation_epoch') != self._setting('conversation_epoch'))):
                continue
            items = [a for a in artifacts if a['kind'] == 'work' and not a.get('_hidden')]
            previous = next((a for a in items if parent['id'] in [a.get('task_id'), a.get('_work_origin_task_id')] + a.get('_work_previous_task_ids', [])
                             and a['status'] not in ('completed', 'cancelled')), None)
            if not previous:
                continue
            owner = tasks[previous['task_id']]
            if owner['id'] != parent['id'] and (owner['status'] != 'failed' or owner['created_at'] >= task['created_at']):
                continue
            history = list(dict.fromkeys(previous.get('_work_previous_task_ids', []) + [owner['id']]))
            copies = [a for a in items if a.get('task_id') == task['id']]
            reported = [a for a in copies if not a.get('_automatic_work')
                        or self._setting('work.dismissed.work:' + a['id'])]
            if reported:
                # Preserve existing report links and the artifact ID used by Undo.
                if previous.get('_automatic_work'):
                    current = max(reported, key=lambda a: (a['observed_at'], a['id']))
                    current.setdefault('_work_origin_task_id', previous.get('_work_origin_task_id', parent['id']))
                    current['_work_previous_task_ids'] = list(dict.fromkeys(history + current.get('_work_previous_task_ids', [])))
                    self._put('artifacts', current)
                    previous['_hidden'] = True
                    self._put('artifacts', previous)
                    self._event('artifact.updated', dict(self._public(previous), _hidden=True), previous['bot_id'], parent['id'])
                continue
            previous.setdefault('_work_origin_task_id', parent['id'])
            previous['_work_previous_task_ids'] = history
            previous.update(task_id=task['id'], status='waiting', activity='Queued', observed_at=now(), _work_resuming=True)
            self._put('artifacts', previous)
            self._event('artifact.updated', self._public(previous), previous['bot_id'], task['id'])
            for duplicate in copies:
                duplicate['_hidden'] = True
                self._put('artifacts', duplicate)
                self._event('artifact.updated', dict(self._public(duplicate), _hidden=True), duplicate['bot_id'], task['id'])

    def _automatic_work_state(self, root, tasks, records, timestamp):
        item = dict(id=root['id'], task_id=root['id'], bot_id=root['bot_id'], kind='work',
                    title=root['title'], status='working', activity='', _automatic_work=True)
        outcome = self._work_outcome(item, tasks, records, self._all('decisions'), self._all('contexts'), timestamp)
        return outcome['activity_state'], outcome['activity']

    def _sync_automatic_work(self):
        if self.kanban and getattr(self.provider, 'supports_native_outcomes', False):
            return
        from coconut.service import now, uid
        with self.transaction():
            tasks = {t['id']: t for t in self._all('tasks')}
            records = self._work_receipts()
            artifacts = self._all('artifacts')
            timestamp = datetime.now(timezone.utc)
            self._sync_followup_work(tasks, artifacts)
            self._resume_tracked_work(tasks, artifacts)
            self._sync_delegated_work(tasks, artifacts)
            covered = {a.get('task_id') for a in artifacts if a['kind'] in ('work', 'pr')}
            for task in tasks.values():
                if (task['id'] in covered or task.get('_chat_session') or task.get('_native_outcome_id') or task.get('source_bot_id')
                        or task.get('_native_job') or task.get('_source_audit_prompt')
                        or task.get('_assignment_task_id') or task.get('_work_context_id')
                        or task['status'] not in ('working', 'waiting', 'needs_you')):
                    continue
                bot = self._get('bots', task['bot_id'])
                if bot.get('_deleted') or bot.get('archived') or task.get('session_id') != bot.get('session_id'):
                    continue
                if task.get('_conversation_epoch') != self._setting('conversation_epoch'):
                    continue
                # Brief replies do not need a permanent Work entry.
                started = datetime.fromisoformat(task.get('_turn_started_at', task['created_at']).replace('Z', '+00:00'))
                if (timestamp - started).total_seconds() < 30:
                    continue
                human_request = self.db.execute("""SELECT 1 FROM messages
                    WHERE json_extract(data, '$.task_id')=? AND json_extract(data, '$.role')='user'
                    AND json_extract(data, '$.sender_bot_id') IS NULL
                    AND COALESCE(json_extract(data, '$.reaction_event'), 0)=0 LIMIT 1""", (task['id'],)).fetchone()
                if not human_request:
                    continue
                chain = self._work_chain(task, tasks, records, include_continuations=True)
                if covered.intersection(chain):
                    continue
                item = dict(id=uid(), bot_id=bot['id'], task_id=task['id'], kind='work', url=None, preview='',
                            title=self._work_title(task), activity='In progress', status='working',
                            created_at=now(), observed_at=now(), _work_key='task-' + task['id'], _automatic_work=True)
                artifacts.append(item)
                covered.add(task['id'])
                self._put('artifacts', item)
                self._event('artifact.created', self._public(item), bot['id'], task['id'])
            for item in artifacts:
                if not item.get('_automatic_work') or item.get('_hidden') or item.get('task_id') not in tasks:
                    continue
                task = tasks[item['task_id']]
                if not item.get('_work_resuming'):
                    title = self._work_title(task)
                    if title != item['title'] and item['title'] == task['title']:
                        item['title'] = title
                        self._put('artifacts', item)
                        self._event('artifact.updated', self._public(item), item['bot_id'], task['id'])
                if task.get('_source_audit_prompt'):
                    item['_hidden'] = True
                    self._put('artifacts', item)
                    self._event('artifact.updated', dict(self._public(item), _hidden=True), item['bot_id'], item['task_id'])
                    continue
                if item['title'].startswith('Continue this proposal using the recorded reply.'):
                    proposal = next((d for d in self._all('decisions')
                                     if d.get('_execution_task_id') == task['id']), None)
                    if proposal:
                        item['title'] = proposal['title'][:120]
                        self._put('artifacts', item)
                        self._event('artifact.updated', self._public(item), item['bot_id'], task['id'])
                chain = self._work_chain(task, tasks, records, include_continuations=True)
                if any(a['kind'] == 'pr' and a.get('task_id') in chain for a in artifacts):
                    item['_hidden'] = True
                    self._put('artifacts', item)
                    self._event('artifact.updated', dict(self._public(item), _hidden=True), item['bot_id'], item['task_id'])
                    continue
                state, activity = self._automatic_work_state(tasks[item['task_id']], tasks, records, timestamp)
                if (item['status'], item['activity']) == (state, activity):
                    continue
                item.update(status=state, activity=activity, observed_at=now())
                if state not in ('completed', 'cancelled'):
                    self.db.execute('DELETE FROM settings WHERE key=?', ('work.dismissed.work:' + item['id'],))
                self._put('artifacts', item)
                self._event('artifact.updated', self._public(item), item['bot_id'], item['task_id'])


for name in ('_work_title', '_sync_followup_work', '_resume_tracked_work', '_automatic_work_state', '_sync_automatic_work'):
    setattr(Service, name, getattr(LegacyWorkSync, name))
