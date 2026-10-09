"""One shared GitHub login for all bots that leaves other GitHub logins on the Mac untouched."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from coconut import github_connection as github
from coconut.github_connection import GitHubConnection, GitHubError, bot_environment, shell_hook, token_path


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.answers = []
        self.revoked = False

    def __call__(self, url, fields=None, token=None):
        self.calls.append((url, fields, token))
        if url == github.DEVICE_CODE_URL:
            return dict(device_code='device-1', user_code='ABCD-1234', verification_uri='https://github.com/login/device',
                        interval=5, expires_in=900)
        if url == github.TOKEN_URL:
            return self.answers.pop(0) if self.answers else dict(error='authorization_pending')
        if url == github.USER_URL:
            if self.revoked:
                raise PermissionError('revoked')
            return dict(login='sahil')
        raise AssertionError(url)


class GitHubConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='github connection ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.github = FakeGitHub()
        self.sleeps = []
        self.connection = GitHubConnection(self.root, client_id='client-1', request=self.github, sleep=self.sleeps.append)
        self.addCleanup(self.connection.disconnect)

    def start(self):
        # Run the token polling in the test thread, so each answer is checked in order.
        self.connection.wait = lambda login: None
        status = self.connection.start()
        del self.connection.wait
        return status

    def finish(self, *answers):
        self.github.answers = list(answers)
        self.connection.wait(self.connection.login)

    def test_device_sign_in_saves_only_coconuts_own_token(self):
        status = self.start()
        self.assertEqual(status['pending'], dict(user_code='ABCD-1234', verification_uri='https://github.com/login/device'))
        self.assertFalse(status['connected'])
        self.assertEqual(self.connection.start(), status)
        self.assertEqual(len([c for c in self.github.calls if c[0] == github.DEVICE_CODE_URL]), 1)
        self.assertEqual(self.github.calls[0][1], dict(client_id='client-1', scope=github.SCOPES))
        self.finish(dict(error='authorization_pending'), dict(error='slow_down', interval=10),
                    dict(access_token='gho_shared', scope='repo,read:org,workflow'))
        self.assertEqual(self.sleeps, [5, 5, 10])
        self.assertEqual(self.connection.status(), dict(connected=True, login='sahil', needs_reconnect=False, pending=None, error=None))
        self.assertEqual(self.connection.token(), 'gho_shared')
        self.assertEqual(token_path(self.root).stat().st_mode & 0o777, 0o600)

    def test_declined_or_expired_code_reports_an_error(self):
        self.start()
        self.finish(dict(error='access_denied'))
        status = self.connection.status()
        self.assertFalse(status['connected'])
        self.assertIsNone(status['pending'])
        self.assertEqual(status['error'], 'GitHub sign-in did not finish. Connect GitHub again.')
        self.start()
        self.connection.login['expires'] = 0
        self.connection.wait(self.connection.login)
        self.assertEqual(self.connection.status()['error'], 'This GitHub code expired. Connect GitHub again.')

    def test_revoked_login_needs_reconnect_and_disconnect_removes_it(self):
        self.start()
        self.finish(dict(access_token='gho_shared'))
        self.github.revoked = True
        self.assertTrue(self.connection.check()['needs_reconnect'])
        self.github.revoked = False
        self.assertFalse(self.connection.check()['needs_reconnect'])
        self.assertFalse(self.connection.disconnect()['connected'])
        self.assertEqual(self.connection.token(), '')

    def test_disconnect_stops_a_pending_sign_in(self):
        self.start()
        login = self.connection.login
        self.connection.disconnect()
        self.github.answers = [dict(access_token='late')]
        self.connection.wait(login)
        self.assertEqual(self.connection.token(), '')

    def test_missing_client_id_is_a_clear_error(self):
        with self.assertRaisesRegex(GitHubError, 'not configured'):
            GitHubConnection(self.root, client_id='', request=self.github).start()

    def test_bot_environment_adds_the_login_and_keeps_existing_git_settings(self):
        env = {'GITHUB_TOKEN': 'other', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'user.name', 'GIT_CONFIG_VALUE_0': 'Sahil'}
        self.assertEqual(bot_environment(env, token_path(self.root)), env)
        self.start()
        self.finish(dict(access_token='gho_shared'))
        result = bot_environment(env, token_path(self.root))
        self.assertEqual((result['GH_TOKEN'], result['COCONUT_GITHUB_LOGIN']), ('gho_shared', '1'))
        self.assertNotIn('GITHUB_TOKEN', result)
        self.assertEqual((result['GIT_CONFIG_COUNT'], result['GIT_CONFIG_KEY_0'], result['GIT_CONFIG_VALUE_1']), ('5', 'user.name', ''))

    def test_shell_hook_sends_github_credentials_through_coconuts_login_only(self):
        other = self.root / 'other-helper'
        other.write_text('#!/bin/sh\n[ "$1" = get ] && printf "username=gumclaw\\npassword=gumclaw-token\\n"\n')
        other.chmod(0o755)
        account_config = self.root / 'account.gitconfig'
        settings = f'[credential]\n\thelper = !{shlex.quote(str(other))}\n[user]\n\tname = Sahil\n'
        account_config.write_text(settings)
        hook = self.root / 'hook.sh'
        hook.write_text(shell_hook(token_path(self.root)))
        source = '. ' + shlex.quote(str(hook))
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(account_config), GIT_TERMINAL_PROMPT='0', GH_TOKEN='inherited')

        def run(shell, command):
            return subprocess.run([shell, '-c', f'{source}; {command}'], env=env, capture_output=True, text=True).stdout

        def fill(host, shell='bash'):
            return run(shell, f'printf "protocol=https\\nhost={host}\\n\\n" | git credential fill')

        # Without Coconut's login, the hook changes nothing.
        self.assertIn('password=gumclaw-token', fill('github.com'))
        self.assertEqual(run('bash', 'echo "$GH_TOKEN"').strip(), 'inherited')
        self.start()
        self.finish(dict(access_token='gho_shared'))
        for shell in ('bash', 'zsh', 'sh'):
            with self.subTest(shell=shell):
                self.assertIn('password=gho_shared', fill('github.com', shell))
                self.assertIn('password=gumclaw-token', fill('gitlab.com', shell))
        self.assertEqual(run('bash', 'echo "$GH_TOKEN"; git config user.name').split(), ['gho_shared', 'Sahil'])
        # SSH remotes on GitHub use HTTPS and Coconut's token, not the account's SSH keys. Other hosts keep SSH.
        repository = self.root / 'repository'
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        for remote, url in (('origin', 'git@github.com:antiwork/coconut.git'), ('ssh', 'ssh://git@github.com/antiwork/coconut.git'),
                            ('other', 'git@gitlab.com:team/project.git')):
            subprocess.run(['git', '-C', str(repository), 'remote', 'add', remote, url], check=True)
        urls = run('bash', f'for remote in origin ssh other; do git -C {shlex.quote(str(repository))} remote get-url $remote; done').split()
        self.assertEqual(urls, ['https://github.com/antiwork/coconut.git', 'https://github.com/antiwork/coconut.git',
                                'git@gitlab.com:team/project.git'])
        self.assertEqual(account_config.read_text(), settings)

        # A bot process that started with the login does not get the git settings twice.
        started = bot_environment(dict(env, GH_TOKEN=''), token_path(self.root))
        count = subprocess.run(['bash', '-c', f'{source}; echo "$GIT_CONFIG_COUNT"'], env=started, capture_output=True, text=True).stdout
        self.assertEqual(count.strip(), started['GIT_CONFIG_COUNT'])
        # After a disconnect, new sessions of that process lose the token, and git asks no other account.
        self.connection.disconnect()
        fill_started = subprocess.run(['bash', '-c', f'{source}; echo "token=$GH_TOKEN"; printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill'],
                                      env=started, capture_output=True, text=True)
        self.assertIn('token=\n', fill_started.stdout)
        self.assertNotIn('password=', fill_started.stdout)


if __name__ == '__main__':
    unittest.main()
