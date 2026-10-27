"""A pairing link and the iPhone's message for Hermes must pass the secret redaction of the installed Hermes unchanged.

Hermes masks text that looks like a secret before the agent sees a tool result and before a message leaves the
gateway. Hermes v0.22.5 masked the base64url JSON of the earlier link as a JSON Web Token, because it starts with "eyJ".
"""
import base64
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from hermes_env import PYTHON, SOURCE, needs_hermes

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import pairing  # noqa: E402

# Each line runs one public redaction entry point of Hermes with the options that its callers use.
REDACT = r'''
import json, sys
from agent import redact
texts = json.load(sys.stdin)
calls = [
    lambda text: redact.redact_sensitive_text(text, force=True),
    lambda text: redact.redact_sensitive_text(text, force=True, code_file=True),
    lambda text: redact.redact_sensitive_text(text, force=True, file_read=True),
    lambda text: redact.redact_sensitive_text(text, force=True, secret_file=True),
    lambda text: redact.redact_sensitive_text(text, force=True, redact_url_credentials=True),
    redact.redact_for_egress,
    lambda text: redact.redact_terminal_output(text, 'python3 scripts/pairing.py --share', force=True),
    lambda text: redact.redact_terminal_output(text, 'env', force=True),
    redact.redact_cdp_url,
]
json.dump([[call(text) for call in calls] for text in texts], sys.stdout)
'''


# The message that the iPhone shows for Hermes (ios/Coconut/PairingCode.swift reads the same file).
MESSAGE = (Path(__file__).resolve().parents[2] / 'ios/Coconut/Resources/hermes-pairing-message.txt').read_text().strip()


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


@needs_hermes
class PairingLinkRedactionTests(unittest.TestCase):
    def redact(self, texts):
        with tempfile.TemporaryDirectory() as home:
            environment = dict(os.environ, HERMES_HOME=home, HERMES_REDACT_SECRETS='true')
            result = subprocess.run([PYTHON, '-c', REDACT], cwd=SOURCE, env=environment, input=json.dumps(texts),
                                    capture_output=True, text=True, check=True, timeout=120)
        return json.loads(result.stdout)

    def payloads(self):
        names = ['Mac mini', 'Gianfranco’s Mac mini', 'Büro "Mac" {x}', 'sk-proj Mac', 'eyJ Mac', 'Mac' * 26]
        relays = ['https://coconut-push.gumroad.workers.dev', 'https://relay.example.com:8443', 'http://127.0.0.1:8787',
                  'https://' + '.'.join(['a' * 63] * 3 + ['b' * 61]) + ':65535']
        generator = random.Random(7)
        for index in range(300):
            yield dict(version=2, relay=relays[index % len(relays)], route=generator.randbytes(32).hex(),
                       mac_key=encode(generator.randbytes(32)), secret=encode(generator.randbytes(32)),
                       expires=generator.randrange(2**32), name=names[index % len(names)])

    def test_new_links_pass_every_redaction_unchanged(self):
        links = [pairing.link(payload) for payload in self.payloads()]
        # The agent reads the whole output of pairing.py --share, and the message to the person holds the link.
        texts = links + ['Setup link: ' + link + '\nQR image: /tmp/iphone-setup.png' for link in links[:20]]
        for text, results in zip(texts, self.redact(texts)):
            for call, result in enumerate(results):
                self.assertEqual(result, text, f'redaction {call} changed the link')
        for link, payload in zip(links, self.payloads()):
            self.assertEqual(pairing.read_link(link), payload)

    def test_the_message_for_hermes_passes_every_redaction_unchanged(self):
        generator = random.Random(11)
        codes = [base64.b32encode(generator.randbytes(16)).decode().rstrip('=').lower() for _ in range(300)]
        # Codes that start like the secret shapes that Hermes knows, in the lowercase letters that a code can have.
        codes += [(prefix + 'a' * 26)[:26] for prefix in ['eyj', 'sk', 'ghp', 'xox', 'aiza', 'akia', 'hf', 'npm', 'gaaaa', 'bot2']]
        messages = [MESSAGE.replace('{code}', code) for code in codes]
        self.assertTrue(all(len(code) == 26 for code in codes))
        for text, results in zip(messages, self.redact(messages)):
            for call, result in enumerate(results):
                self.assertEqual(result, text, f'redaction {call} changed the message')

    def test_the_earlier_link_format_is_masked(self):
        # A negative control: the real Hermes rules run, and they break the earlier format.
        payload = next(self.payloads())
        earlier = 'coconut://pair?setup=' + encode(json.dumps(payload, separators=(',', ':')).encode())
        self.assertNotEqual(self.redact([earlier])[0][0], earlier)


if __name__ == '__main__':
    unittest.main()
