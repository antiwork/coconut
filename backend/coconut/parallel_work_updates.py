"""Save and read the limits of parallel work. Hermes config holds each one (work_limits)."""
import hmac
import json

from .work_limits import board_limits, validate_limit

# The state key of each limit of Coconut's board.
LIMIT_KEYS = {'total': 'work_capacity', 'per_agent': 'agent_limit'}


class ParallelWorkUpdates:
    def _work_limits(self):
        """The total and per-agent limits. The Hermes config of Coconut's board holds them, and each pass of the
        board reads them (kanban_runtime.Board.limits). Fixture mode has no board, so Coconut keeps its total."""
        if self.kanban is None:
            return board_limits({'kanban': {'max_in_progress': self._setting('work_capacity')}})
        return getattr(self.kanban, 'limits', None) or board_limits({})

    def _work_limit_state(self):
        limits = self._work_limits()
        return {'work_capacity': limits['total'], 'agent_limit': limits['per_agent'],
                'queues': getattr(self.kanban, 'queues', None) or {}}

    def _migrate_work_capacity(self):
        """Earlier versions kept the total in Coconut's settings. Move it once into the Hermes config of the board."""
        if getattr(self, '_work_capacity_moved', False):
            return
        saved = self._setting('work_capacity')
        limits = getattr(self.kanban, 'limits', None)
        if saved is not None and limits is None:
            return  # The board has not reported its limits yet. The next pass moves the total.
        if saved is not None and limits['total']['limit'] is None:
            self.kanban.limits = self.kanban.call({'op': 'set_limit', 'name': 'total', 'limit': saved})['limits']
        if saved is not None:
            with self.transaction():
                self.db.execute("DELETE FROM settings WHERE key='work_capacity'")
        self._work_capacity_moved = True

    def mutate_board_limit(self, name, data, key, fingerprint):
        """Save the total or the per-agent limit in the Hermes config of Coconut's board."""
        from .service import ServiceError, encoded
        if not isinstance(data, dict) or set(data) != {'limit'}:
            raise ServiceError(400, 'Specify a limit, or null for Automatic.')
        try:
            validate_limit(data['limit'])
        except ValueError as error:
            raise ServiceError(400, str(error)) from None
        with self.transaction():
            previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
            if previous:
                if not hmac.compare_digest(previous[0], fingerprint):
                    raise ServiceError(409, 'This idempotency key belongs to a different request.')
                return json.loads(previous[1])
        # The same value saves again on a retry, so a lost response needs no pending record.
        try:
            limits = self.kanban.call({'op': 'set_limit', 'name': name, 'limit': data['limit']})['limits']
        except Exception:
            raise ServiceError(503, 'The Mac could not confirm the limit. Retry with the same request key.') from None
        self.kanban.limits = limits
        result = limits[name]
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO requests(key,fingerprint,response) VALUES (?,?,?)',
                            (key, fingerprint, encoded(result)))
            self._event('settings.updated', {LIMIT_KEYS[name]: result})
        return result

    def mutate_parallel_work(self, data, key, fingerprint):
        from .service import ServiceError, encoded
        if not isinstance(data, dict) or set(data) != {'limit'}:
            raise ServiceError(400, 'Specify a worker limit, or null for Automatic.')
        try:
            validate_limit(data['limit'])
        except ValueError as error:
            raise ServiceError(400, str(error)) from None
        with self.model_settings_lock:
            current = self.provider.native_settings()['parallel_work']
            with self.transaction():
                for pending_key, raw in self.db.execute("SELECT key,response FROM requests WHERE response LIKE '%\"_parallel_pending\"%'").fetchall():
                    pending = json.loads(raw)
                    if set(pending) != {'_parallel_pending'}:
                        continue
                    result = current if current['limit'] == pending['_parallel_pending'] else {'_parallel_error': 'The Mac could not confirm the worker limit. Check the current setting.'}
                    self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), pending_key))
                previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
                if previous:
                    if not hmac.compare_digest(previous[0], fingerprint):
                        raise ServiceError(409, 'This idempotency key belongs to a different request.')
                    result = json.loads(previous[1])
                    if result.get('_parallel_error'):
                        raise ServiceError(409, result['_parallel_error'])
                    return result
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_parallel_pending': data['limit']})))
            try:
                result = self.provider.native_settings('set_parallel_work', limit=data['limit'])
            except Exception:
                raise ServiceError(503, 'The Mac could not confirm the worker limit. Retry with the same request key.') from None
            with self.transaction():
                self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), key))
                self._event('settings.updated', {'parallel_work': result})
            return result
