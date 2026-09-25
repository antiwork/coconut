"""Static public failure copy. Never forward native error text or request details."""


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


def native_failure(result):
    reason = result.get('failure_reason')
    if isinstance(reason, str) and reason in _FAILURES:
        return {'summary': _FAILURES[reason], 'error_code': 'native_' + reason}
    if result.get('interrupted'):
        return {'summary': 'The reply was interrupted before completion.', 'error_code': 'native_interrupted'}
    return {'summary': 'Hermes could not finish this reply.', 'error_code': 'native_turn_failed'}
