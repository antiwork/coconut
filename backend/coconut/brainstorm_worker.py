"""Answers the model calls of a brainstorm (brainstorm.py) in the Hermes Python of a project's profile.

It reads one JSON request per line on standard input and writes one JSON answer per line on standard output. The answer
comes from the main model of the profile through the client of Hermes (agent.auxiliary_client.call_llm), with the logins
of the profile, so Coconut never reads a key. Whatever else writes to standard output goes to standard error, so the
answers stay readable. It ends when the service closes its input, or after a quarter of an hour without a request.
"""
import json
import os
import select
import sys

IDLE_SECONDS = 15 * 60


def main():
    answers = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if len(sys.argv) > 1 and sys.argv[1]:
        # The Hermes source, as the gateway of the service starts it (gateway_chat.LAUNCH).
        sys.path.insert(0, sys.argv[1])
    from agent.auxiliary_client import call_llm
    from hermes_cli.config import load_config
    provider, model = _model(load_config())
    try:
        # One small call opens the connection and the login while the person says their first sentence, so the first
        # answer of the talk comes as fast as the others (eight seconds against three without it).
        call_llm(provider=provider, model=model, messages=[{'role': 'user', 'content': 'Hi'}], max_tokens=1, timeout=30)
    except Exception:  # noqa: BLE001 - the first real call reports the problem
        pass
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], IDLE_SECONDS)
        if not ready:
            return
        line = sys.stdin.readline()
        if not line:
            return
        request = json.loads(line)
        # The config is read for each call, so a change of the model of the project counts at once.
        provider, model = _model(load_config())
        try:
            response = call_llm(provider=provider, model=model, messages=request['messages'],
                                max_tokens=request['max_tokens'], timeout=request['timeout'])
            answer = {'id': request['id'], 'text': response.choices[0].message.content or ''}
        except Exception as error:  # noqa: BLE001 - the phone shows the reason
            answer = {'id': request['id'], 'error': f'{type(error).__name__}: {error}'[:300]}
        answers.write(json.dumps(answer) + '\n')


def _model(config):
    """The main model and provider of the profile, as its turns use them."""
    model = config.get('model')
    if isinstance(model, dict):
        return model.get('provider') or None, model.get('default') or model.get('model') or None
    return None, model if isinstance(model, str) and model else None


if __name__ == '__main__':
    main()
