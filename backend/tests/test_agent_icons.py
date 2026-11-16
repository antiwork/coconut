"""The icon of a new agent (coconut/agent_icons.py)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from coconut import agent_icons
from coconut.providers import FixtureProvider
from coconut.service import Service

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class AgentIconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name, FixtureProvider(Path(self.temp.name)), start_workers=False)
        self.addCleanup(self.service.close)
        self.bot = self.service.mutate("POST", "/v1/bots", {"name": "Support", "role": "answers customer email"},
                                       str(uuid4()), "phone")
        icon = Path(self.temp.name) / "icon.png"
        icon.write_bytes(PNG)
        self.icon = icon
        self.service.provider.python = "/usr/bin/python3"
        self.service.provider.environment = lambda bot_id: ({}, self.temp.name)

    def run_worker(self, answer):
        calls = []

        def run(command, input, **_):
            calls.append(json.loads(input))
            return SimpleNamespace(stdout=json.dumps(answer) + "\n")
        return agent_icons.make(self.service, self.bot["id"], run=run), calls

    def test_a_new_agent_gets_an_icon_from_its_name_and_role(self):
        artifact, calls = self.run_worker({"path": str(self.icon)})
        self.assertEqual(calls, [{"name": "Support", "role": "answers customer email"}])
        bot = self.service._get("bots", self.bot["id"])
        self.assertEqual(bot["avatar_image"], artifact)
        self.assertEqual(self.service.artifact_content(artifact)[0], PNG)
        # The apps read the field.
        self.assertEqual(self.service._public(bot)["avatar_image"], artifact)
        # A second run keeps the first icon.
        self.assertIsNone(self.run_worker({"path": str(self.icon)})[0])

    def test_without_a_backend_or_with_bad_bytes_the_fruit_stays(self):
        self.assertIsNone(self.run_worker({"error": "no_backend"})[0])
        self.icon.write_bytes(b"not an image")
        self.assertIsNone(self.run_worker({"path": str(self.icon)})[0])
        self.assertNotIn("avatar_image", self.service._get("bots", self.bot["id"]))

    def test_a_service_without_hermes_makes_no_icon(self):
        self.assertFalse(agent_icons.wanted(SimpleNamespace(provider=SimpleNamespace())))


if __name__ == "__main__":
    unittest.main()
