"""Static public failure copy. Never forward native error text or request details."""
import re


_FAILURES = {
    'context_overflow': 'The saved conversation exceeds the model context limit. It needs context recovery before retrying.',
    'rate_limit': 'The model provider rate limit prevented this reply.',
    'auth': 'The model provider could not authenticate this reply.',
    'auth_permanent': 'The model provider needs a valid sign-in or credential before retrying.',
    'billing': 'The model provider reported a billing limit.',
    'billing_unverified': 'The model provider could not confirm available billing capacity.',
    'timeout': 'The model provider timed out before this reply completed.',
    'overloaded': 'The model provider could not handle this reply.',
    'server_error': 'The model provider returned a server error before this reply completed.',
    'payload_too_large': 'The model provider rejected the request size. It needs context recovery before retrying.',
    'session_busy': 'The saved conversation is already in use. No second turn was started.',
    'content_policy_blocked': 'The model provider blocked this reply under its content policy.',
    'provider_policy_blocked': 'The model provider blocked this reply under its policy.',
    'model_not_found': 'The configured model is unavailable from its provider.',
    'format_error': 'The model provider rejected the request format.',
    'interpreter_shutdown': 'The worker stopped before this reply completed.',
    'empty_response': 'The model provider returned no usable reply.',
    'invalid_response': 'The model provider returned an invalid reply.',
    'truncated': 'The model provider cut off this reply before completion.',
}


NATIVE_ERROR_CODES = frozenset({'native_' + reason for reason in _FAILURES} | {
    'native_interrupted', 'native_turn_failed', 'worker_preparation_failed'})


# A failure of the model provider that the provider ends on its own: a rate limit, a billing limit, an overload, a timeout.
# Hermes must retry the task later without counting a failure, so its respawn guard does not park the task for good.
TRANSIENT_ERROR_CODES = frozenset('native_' + reason for reason in (
    'rate_limit', 'billing', 'billing_unverified', 'overloaded', 'timeout', 'server_error'))
# The text that Hermes stores for such a failure. It must not contain a word that the respawn guard reads as an
# authorization blocker (quota, rate limit, auth, billing, and so on).
TRANSIENT_NATIVE_ERROR = 'The model provider stopped this turn. Coconut retries it later.'


# A provider limit error, such as usage_limit_reached, can say when the limit resets. Only that number leaves the worker.
_RESETS_IN = re.compile(r'resets_in_seconds\W{0,3}(\d{1,9})')


def native_failure(result):
    reason = result.get('failure_reason')
    if isinstance(reason, str) and reason in _FAILURES:
        failure = {'summary': _FAILURES[reason], 'error_code': 'native_' + reason}
        resets = _RESETS_IN.search(str(result.get('error') or '')) if reason in ('rate_limit', 'billing') else None
        if resets:
            failure['retry_after'] = int(resets.group(1))
        return failure
    if result.get('interrupted'):
        return {'summary': 'The reply was interrupted before completion.', 'error_code': 'native_interrupted'}
    return {'summary': 'Hermes could not finish this reply.', 'error_code': 'native_turn_failed'}
