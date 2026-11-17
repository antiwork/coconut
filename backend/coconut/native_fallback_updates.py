"""Save and reconcile the fallback models of the default profile."""
from collections import Counter
import hmac
import json

from .native_fallback import MAX_MODELS, validate

UNCONFIRMED = 'The Mac could not confirm the fallback model. Check the current setting.'


def _routes(models):
    return [(item['provider'].strip().lower(), item['model'].strip().lower()) for item in models]


class NativeFallbackUpdates:
    def mutate_native_fallback(self, data, key, fingerprint):
        from .service import ServiceError, encoded
        try:
            models = validate(data.get('models') if isinstance(data, dict) and set(data) == {'models'} else None)
        except ValueError:
            raise ServiceError(400, f'Send the fallback models as a list of at most {MAX_MODELS} models, each with a model and a provider.') from None
        routes = _routes(models)
        with self.model_settings_lock:
            current = self.provider.native_settings()['fallback']
            with self.transaction():
                for pending_key, raw in self.db.execute("SELECT key,response FROM requests WHERE response LIKE '%\"_fallback_pending\"%'").fetchall():
                    pending = json.loads(raw)
                    if set(pending) != {'_fallback_pending'}:
                        continue
                    # An earlier version saved a bool here. It cannot be checked, so it settles as unconfirmed.
                    requested = pending['_fallback_pending']
                    result = current if isinstance(requested, list) and _routes(current['models']) == _routes(requested) else {'_fallback_error': UNCONFIRMED}
                    self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), pending_key))
                previous = self.db.execute('SELECT fingerprint,response FROM requests WHERE key=?', (key,)).fetchone()
                if previous:
                    if not hmac.compare_digest(previous[0], fingerprint):
                        raise ServiceError(409, 'This idempotency key belongs to a different request.')
                    result = json.loads(previous[1])
                    if result.get('_fallback_error'):
                        raise ServiceError(409, result['_fallback_error'])
                    return result
                if routes == _routes(current['models']):
                    # Nothing changes, so nothing is written.
                    self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded(current)))
                    return current
            # The chain can hold one route more than once, with different endpoints. The request can keep each copy.
            known = Counter(_routes(current['models']))
            if any(count > max(known[route], 1) for route, count in Counter(routes).items()):
                raise ServiceError(400, 'Select each fallback model once.')
            # A model that the chain has stays as it is. A new model must be in the Hermes list, as for the default model.
            added = [item for item, route in zip(models, routes) if route not in known]
            if added:
                catalog = self._validation_model_catalog()
                if catalog is None:
                    raise ServiceError(400, 'Select an available model from the Hermes list.')
                default = catalog.get('default') or {}
                for item in added:
                    try:
                        self.provider.resolve_model({'model_override': dict(item, reasoning_effort=None)}, catalog)
                    except ValueError:
                        raise ServiceError(400, 'Select an available model from the Hermes list.') from None
                    if (item['model'], item['provider']) == (default.get('model'), default.get('provider')):
                        raise ServiceError(400, 'Choose a model other than the default model.')
            with self.transaction():
                self.db.execute('INSERT INTO requests VALUES (?,?,?)', (key, fingerprint, encoded({'_fallback_pending': models})))
            try:
                result = self.provider.native_settings('set_fallback', models=models)
            except Exception:
                raise ServiceError(503, 'The Mac could not confirm the fallback model. Retry with the same request key.') from None
            with self.transaction():
                self.db.execute('UPDATE requests SET response=? WHERE key=?', (encoded(result), key))
                self._event('settings.updated', {'fallback': result})
        return result
