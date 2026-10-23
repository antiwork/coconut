"""Offline pinned-Hermes fallback contracts, including the missing aux handoff.

The inherited-chain case records a runtime API limitation, not successful
compression propagation. No config-reader or provider-router replacements are
installed by these tests. Native aux_probe_mode constructs no SDK transports.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE

REPO = Path(__file__).resolve().parents[2]


def _native_case(case):
    import io
    import json
    import logging
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from threading import Barrier
    from unittest.mock import patch

    from coconut.native_fallback import configuration, for_turn
    from agent import auxiliary_client as aux
    from agent.secret_scope import current_secret_scope, get_secret
    from hermes_cli.config import load_config_readonly
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_constants import get_hermes_home

    root = Path(os.environ['HERMES_HOME'])
    owner, bot, other = (root / name for name in ('owner', 'bot', 'other'))
    keys = ('synthetic-owner-key', 'synthetic-bot-key', 'synthetic-other-key')
    route = {'provider': 'openai-api', 'model': 'gpt-6-astra',
             'base_url': 'https://api.openai.com/v1', 'key_env': 'OPENAI_API_KEY'}

    def profile(home, config, key):
        home.mkdir(exist_ok=True)
        (home / 'config.yaml').write_text(json.dumps(config))
        (home / '.env').write_text(f'OPENAI_API_KEY={key}\n' if key else '')

    profile(owner, {'fallback_providers': [route, {'provider': 'anthropic', 'model': 'other'}]}, keys[0])
    profile(bot, {}, keys[1])
    profile(other, {'fallback_model': dict(route, model='gpt-6-sol')}, keys[2])
    os.environ['HERMES_HOME'] = str(bot)
    os.environ['COCONUT_DEFAULT_HOME'] = str(owner)
    os.environ['OPENAI_API_KEY'] = 'synthetic-stale-export'
    env_before = dict(os.environ)
    home_before, scope_before = get_hermes_home(), current_secret_scope()
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    logger = logging.getLogger('agent.auxiliary_client')
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    def resolved(entry):
        # Actual native normalization/credential/transport selection; no model call.
        with aux.aux_probe_mode():
            client, model = aux._resolve_fallback_entry(entry)
        assert client is not None
        assert model == entry['model']
        assert client.api_key == entry['api_key']
        assert str(client.base_url).rstrip('/') == entry['base_url']

    def entries_for_turn():
        entries = for_turn()
        assert entries is not None
        return entries

    def native_chain():
        with aux.aux_probe_mode():
            return aux._try_main_fallback_chain('compression', 'openai-codex')

    def snapshot():
        return {str(p): p.read_bytes() for home in (owner, bot, other)
                for p in (home / 'config.yaml', home / '.env')}

    # Fail closed even if a future native resolver starts doing network I/O.
    with ExitStack() as stack:
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.create_connection'):
            stack.enter_context(patch(target, side_effect=AssertionError('Network forbidden in fallback contracts')))
        try:
            if case == 'inherited_api_gap':
                before = snapshot()
                with configuration(bot):
                    entries = entries_for_turn()
                    assert len(entries) == 1 and entries[0]['api_key'] == keys[0]
                    resolved(entries[0])
                    assert get_secret('OPENAI_API_KEY') == keys[1]
                    assert get_hermes_home() == bot
                    # Passing this chain to AIAgent is insufficient: the aux reader
                    # still sees the bot's empty native chain. This is the blocker.
                    assert get_fallback_chain(load_config_readonly()) == []
                    assert native_chain() == (None, None, '')
                assert snapshot() == before
            elif case == 'explicit_native_positive':
                profile(bot, {'fallback_providers': [route]}, keys[1])
                before = snapshot()
                with configuration(bot):
                    entries = entries_for_turn()
                    assert entries[0]['api_key'] == keys[1]
                    client, model, provider = native_chain()
                    assert client is not None and client.api_key == keys[1]
                    assert model == route['model'] and provider == 'openai-api'
                    assert str(client.base_url).rstrip('/') == route['base_url']
                assert snapshot() == before
            elif case == 'disabled_and_missing_owner_key':
                for config in ({'fallback_providers': []}, {'fallback_model': None}):
                    profile(bot, config, keys[1])
                    with configuration(bot):
                        assert for_turn() is None
                        assert native_chain() == (None, None, '')
                profile(bot, {}, keys[1])
                profile(owner, {'fallback_providers': [route]}, '')
                with configuration(bot):
                    assert for_turn() is None  # Never borrow the bot/process key.
                    assert get_secret('OPENAI_API_KEY') == keys[1]
                profile(owner, {'fallback_model': {'provider': 'anthropic', 'model': 'other'}}, keys[0])
                with configuration(bot):
                    assert for_turn() is None  # Shared scope stays OpenAI-only.
            elif case == 'concurrent_profile_override':
                barrier = Barrier(2)
                before = snapshot()

                def check(home, model, key):
                    with configuration(home):
                        barrier.wait(timeout=10)
                        for _ in range(4):
                            entries = entries_for_turn()
                            assert len(entries) == 1
                            assert entries[0]['model'] == model
                            assert entries[0]['api_key'] == key
                            resolved(entries[0])
                            assert get_hermes_home() == home
                            assert get_secret('OPENAI_API_KEY') == (keys[1] if home == bot else keys[2])
                        barrier.wait(timeout=10)
                    assert get_hermes_home() == bot
                    assert current_secret_scope() is None

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(check, bot, 'gpt-6-astra', keys[0]),
                               pool.submit(check, other, 'gpt-6-sol', keys[2])]
                    for future in futures:
                        future.result(timeout=25)
                assert snapshot() == before
            elif case == 'failure_restores_scope':
                before = snapshot()
                with configuration(bot):
                    outer_config = load_config_readonly()
                    outer_scope = current_secret_scope()
                    try:
                        with configuration(other):
                            assert entries_for_turn()[0]['api_key'] == keys[2]
                            assert get_hermes_home() == other
                            raise RuntimeError('synthetic failure')
                    except RuntimeError:
                        pass
                    assert get_hermes_home() == bot
                    assert current_secret_scope() is outer_scope
                    assert load_config_readonly() is outer_config
                    assert entries_for_turn()[0]['api_key'] == keys[0]
                    # Exercise a failure inside the secret resolver as well.
                    with patch('coconut.native_fallback.entry_key', side_effect=RuntimeError('synthetic lookup failure')):
                        try:
                            for_turn()
                        except RuntimeError:
                            pass
                        else:
                            raise AssertionError('Expected resolver failure')
                    assert get_hermes_home() == bot
                    assert current_secret_scope() is outer_scope
                assert snapshot() == before
            elif case == 'runtime_context_and_pin_are_not_chain_overrides':
                from agent.context_compressor import pin_summary_route, take_pinned_summary_route
                with configuration(bot):
                    entries = entries_for_turn()
                    runtime = {'provider': 'openai-codex', 'model': 'gpt-6-sol',
                               'fallback_providers': entries}
                    with aux.scoped_runtime_main(runtime) as normalized:
                        assert 'fallback_providers' not in normalized
                        assert native_chain() == (None, None, '')
                    with pin_summary_route(entries[0]):
                        assert take_pinned_summary_route() == entries[0]
                        assert take_pinned_summary_route() is None
                        assert native_chain() == (None, None, '')
                    assert take_pinned_summary_route() is None
            else:
                raise AssertionError('Unknown native case')
        finally:
            logger.removeHandler(handler)
        assert dict(os.environ) == env_before
        assert get_hermes_home() == home_before
        assert current_secret_scope() is scope_before
        assert all(key not in logs.getvalue() for key in (*keys, 'synthetic-stale-export'))
    print('Native fallback contract passed: ' + case)


@needs_hermes
class NativeCompressionFallbackTests(unittest.TestCase):
    def run_case(self, case):
        assert SOURCE and PYTHON
        # All fixture writes occur under this private home. Preserve descendant
        # execution guards; these cases never create a native board/session DB.
        installed_config = Path(SOURCE).resolve().parent / 'config.yaml'
        def installed_hash():
            return hashlib.sha256(installed_config.read_bytes()).hexdigest() if installed_config.exists() else None
        before = installed_hash()
        with tempfile.TemporaryDirectory(prefix='compression-fallback-') as root:
            env = dict(os.environ, HERMES_HOME=root,
                       HERMES_MANAGED_DIR=str(Path(root) / 'managed'),
                       PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=os.pathsep.join((str(Path(SOURCE).resolve()), str(REPO / 'backend'))))
            result = subprocess.run([PYTHON, str(Path(__file__).resolve()), '--native-case', case],
                                    cwd=REPO, env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(installed_hash(), before, 'Installed config changed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Native fallback contract passed: ' + case, result.stdout)
        self.assertNotIn('synthetic-', result.stdout + result.stderr)

    def test_shared_route_resolves_but_native_aux_chain_is_missing(self):
        self.run_case('inherited_api_gap')

    def test_explicit_bot_chain_resolves_through_native_compression(self):
        self.run_case('explicit_native_positive')

    def test_disabled_overrides_and_missing_owner_credentials(self):
        self.run_case('disabled_and_missing_owner_key')

    def test_concurrent_native_home_overrides_stay_isolated(self):
        self.run_case('concurrent_profile_override')

    def test_nested_scope_and_resolver_failures_restore_context(self):
        self.run_case('failure_restores_scope')

    def test_runtime_context_and_summary_pin_do_not_propagate_chain(self):
        self.run_case('runtime_context_and_pin_are_not_chain_overrides')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--native-case']:
        _native_case(sys.argv[2])
    else:
        unittest.main()
