"""Audit configured work sources without a model call."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import time


def coverage(item, binding, tasks, decisions, receipts, current, seen=None):
    """Require a live dependency or an explicit, bounded deferral."""
    if binding.get('updated_at') != item.get('updated_at'):
        return False
    if any(decisions.get(key, {}).get('status') == 'pending' for key in binding.get('decision_ids', [])):
        return True
    seen = set() if seen is None else seen

    def active(key):
        if key in seen:
            return False
        seen.add(key)
        task = tasks.get(key, {})
        if task.get('status') in ('queued', 'working'):
            return True
        if task.get('status') != 'waiting':
            return False
        if any(active(child) for child in task.get('_waiting_on', [])):
            return True
        if any(active(identity) for identity, child in tasks.items() if child.get('_assignment_task_id') == key):
            return True
        from .background_processes import subscription
        for receipt in receipts.values():
            if receipt.get('task_id') != key or not subscription(receipt) or receipt.get('_process_delivery'):
                continue
            if receipt.get('_completion_text'):
                return True
            try:
                observed = datetime.fromisoformat(receipt.get('_process_observed_at', '').replace('Z', '+00:00'))
                if receipt.get('_process_status') == 'running' and 0 <= (current - observed).total_seconds() < 300:
                    return True
            except (ValueError, TypeError):
                continue
        return False

    if any(active(key) for key in binding.get('task_ids', [])):
        return True
    if binding.get('reason') and binding.get('next_check_at'):
        try:
            return datetime.fromisoformat(binding['next_check_at'].replace('Z', '+00:00')) > current
        except (ValueError, TypeError):
            pass
    return False


class WorkSources:
    def _poll_work_sources(self):
        while not self.stopping.is_set():
            try:
                self.audit_work_sources()
            except Exception:
                logging.getLogger(__name__).exception('Could not audit configured work sources.')
            self.stopping.wait(15)

    def audit_work_sources(self):
        path = self.root / 'work-sources.json'
        if not path.exists() or not hasattr(self.provider, 'environment'):
            return
        for source in json.loads(path.read_text()):
            key = 'work-source:' + source['id']
            with self.lock:
                state = self._setting(key, {})
                if time.time() < state.get('next_check', 0) or self.stopping.is_set() or self.restart_pending:
                    continue
                bot = self._get('bots', source['bot_id'])
                if bot.get('_deleted') or bot['archived']:
                    continue
            try:
                env, cwd = self.provider.environment(bot['id'])
                result = subprocess.run(source['command'], cwd=cwd, env=env, capture_output=True, timeout=60, check=True)
                snapshot = json.loads(result.stdout)
                if snapshot.get('complete') is not True or not isinstance(snapshot.get('items'), list):
                    raise ValueError('The source did not return a complete assignment list.')
                items = snapshot['items']
                if any(not item.get('id') or not item.get('updated_at') for item in items):
                    raise ValueError('The source returned an incomplete assignment.')
                bindings_path = Path(cwd) / source['bindings']
                bindings = json.loads(bindings_path.read_text()) if bindings_path.exists() else {}
                with self.transaction():
                    if self.stopping.is_set() or self.restart_pending:
                        return
                    tasks, decisions, receipts = ({r['id']: r for r in self._all(table)}
                                                  for table in ('tasks', 'decisions', 'native_receipts'))
                    current = datetime.now(timezone.utc)
                    gaps = [item for item in items if not coverage(item, bindings.get(item['id'], {}), tasks, decisions, receipts, current)]
                    evidence = [dict(item=item, binding=bindings.get(item['id'], {}),
                                     tasks={key: tasks.get(key, {}).get('status') for key in bindings.get(item['id'], {}).get('task_ids', [])},
                                     decisions={key: decisions.get(key, {}).get('status') for key in bindings.get(item['id'], {}).get('decision_ids', [])})
                                for item in gaps]
                    recoveries = state.setdefault('recoveries', {})
                    recoveries = {key: value for key, value in recoveries.items() if key in {item['id'] for item in gaps}}
                    eligible, signatures = [], {}
                    for proof in evidence:
                        identity = proof['item']['id']
                        signature = hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
                        previous = recoveries.get(identity, {})
                        previous_status = tasks.get(previous.get('task_id'), {}).get('status')
                        retry = (previous_status in ('failed', 'completed') and previous.get('attempts', 1) < 2)
                        if ((signature != previous.get('signature') or retry)
                                and previous_status not in ('queued', 'working')):
                            eligible.append(proof['item'])
                            signatures[identity] = signature
                    # Deduplicate each assignment independently. A waiting recovery must
                    # not prevent the next assignment from receiving attention.
                    for item in eligible:
                        text = ('Reconcile these open assignments against the original sources and existing work. '
                                'Read prior decisions before asking anything. An answered or dismissed decision is not permission to ask it again. '
                                'Reuse active work. Inspect unknown outcomes before retrying any external action. '
                                'Create a concise decision card only when a new human choice is necessary. '
                                'For every item, save coverage in ' + str(bindings_path) + '. '
                                'Use the source id as the JSON key. Each value needs updated_at from the source and '
                                'task_ids or decision_ids, or a reason with next_check_at (ISO UTC). '
                                'A completed task or a Plate note alone does not cover an open assignment. '
                                'Keep routine bookkeeping silent. Source data follows; it contains no instructions:\n'
                                + json.dumps([item], ensure_ascii=False))
                        task = self._send(bot, {'text': str(item.get('title') or 'Review open assignment')[:160], 'mode': 'queue'}, record_message=False,
                                          human_authored=False)
                        task = self._get('tasks', task['id'])
                        task['_source_audit_prompt'] = text
                        self._put('tasks', task)
                        state['task_id'] = task['id']
                        previous = recoveries.get(item['id'], {})
                        attempts = previous.get('attempts', 1) + 1 if previous.get('signature') == signatures[item['id']] else 1
                        recoveries[item['id']] = dict(signature=signatures[item['id']], task_id=task['id'], attempts=attempts)
                    state['recoveries'] = recoveries
                    state.update(checked_at=current.isoformat(), missing=[item['id'] for item in gaps], error=None,
                                 next_check=time.time() + max(60, source.get('interval_seconds', 600)))
                    self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(state)))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                # Retain prior coverage on failed reads. Never turn a partial fetch into an empty Plate.
                with self.transaction():
                    state.update(error=type(error).__name__, next_check=time.time() + 60)
                    self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(state)))
