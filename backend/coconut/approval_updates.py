"""Reconcile native access changes before a client retries them."""
import hmac
import json


class ApprovalUpdates:
    def reconcile_approval_updates(self):
        from .service import encoded
        current = self.provider.native_settings()['approvals']
        with self.transaction():
            for key, raw in self.db.execute("SELECT key,response FROM requests WHERE response LIKE '%\"_approval_pending\"%'").fetchall():
                value = json.loads(raw)
                if set(value) != {'_approval_pending'}:
                    continue
                response = current if current['default'] == value['_approval_pending'] else {'_approval_error': 'The Mac could not confirm that access change. Check the current Hermes setting.'}
                self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(response), key))
        return current

    def mutate_approval_default(self, data, key, fingerprint):
        from .service import ServiceError, encoded
        with self.model_settings_lock:
            current = self.reconcile_approval_updates()
            with self.lock:
                previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
            if previous:
                if not hmac.compare_digest(previous[0], fingerprint):
                    raise ServiceError(409, 'This idempotency key belongs to a different request.')
                response = json.loads(previous[1])
                if response.get('_approval_error'):
                    raise ServiceError(409, response['_approval_error'])
                return response
            if not isinstance(data, dict) or data.get('mode') not in {item['id'] for item in current['modes']}:
                raise ServiceError(400, 'Choose an approval mode from Hermes settings.')
            with self.transaction():
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_approval_pending': data['mode']})))
            try:
                result = self.provider.native_settings('set_default_approval', mode=data['mode'])
            except Exception:
                raise ServiceError(503, 'The Mac could not confirm the access change. Retry with the same request key.') from None
            with self.transaction():
                self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), key))
                self._event('settings.updated', {'approvals': result})
            return result
