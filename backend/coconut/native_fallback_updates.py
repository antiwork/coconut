"""Save and reconcile the native OpenAI fallback setting."""
import hmac
import json


class NativeFallbackUpdates:
    def mutate_native_fallback(self, data, key, fingerprint):
        from .service import ServiceError, encoded
        if not isinstance(data, dict) or set(data) != {'enabled'}:
            raise ServiceError(400, 'Specify whether to use the OpenAI API fallback.')
        if type(data['enabled']) is not bool:
            raise ServiceError(400, 'Choose whether to use the OpenAI API fallback.')
        with self.model_settings_lock:
            current = self.provider.native_settings()['api_fallback']
            with self.transaction():
                for pending_key, raw in self.db.execute("SELECT key,response FROM requests WHERE response LIKE '%\"_fallback_pending\"%'").fetchall():
                    pending = json.loads(raw)
                    if set(pending) != {'_fallback_pending'}:
                        continue
                    result = current if current['enabled'] == pending['_fallback_pending'] else {'_fallback_error': 'The Mac could not confirm the API fallback setting. Check the current setting.'}
                    self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), pending_key))
                previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
                if previous:
                    if not hmac.compare_digest(previous[0], fingerprint):
                        raise ServiceError(409, 'This idempotency key belongs to a different request.')
                    result = json.loads(previous[1])
                    if result.get('_fallback_error'):
                        raise ServiceError(409, result['_fallback_error'])
                    return result
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_fallback_pending': data['enabled']})))
            if data['enabled'] and not current['can_enable']:
                with self.transaction():
                    self.db.execute('DELETE FROM requests WHERE key=?', (key,))
                raise ServiceError(400, 'Save an OpenAI API key on the Mac and select an OpenAI model first.')
            try:
                result = self.provider.native_settings('set_api_fallback', enabled=data['enabled'])
            except Exception:
                raise ServiceError(503, 'The Mac could not confirm the API fallback setting. Retry with the same request key.') from None
            with self.transaction():
                self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), key))
                self._event('settings.updated', {'api_fallback': result})
            return result
