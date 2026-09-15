"""Save and reconcile the native default for parallel work."""
import hmac
import json

from .parallel_work import validate_limit


class ParallelWorkUpdates:
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
