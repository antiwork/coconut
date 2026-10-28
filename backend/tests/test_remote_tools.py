import hashlib
import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import remote
import pairing


class RemoteToolsTests(unittest.TestCase):
    def fixture(self, root):
        for name in remote.TOP_FILES | {"backend/coconut/service.py", "backend/coconut/hermes_versions.json",
                                        "scripts/host.py", "docs/setup.md"}:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("source\n")
        for name in [".runtime/host/credentials.json", ".runtime/host/state.db", "evidence/private.txt",
                     "backend/coconut/__pycache__/cache.py", ".git/config", "ios/signing.xcconfig"]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SECRET-SENTINEL")

    def test_package_contains_only_source_and_matching_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with patch.object(remote, "ROOT", root):
                result = remote.package(root / "source.tar.gz")
            with tarfile.open(root / "source.tar.gz") as archive:
                manifest = json.load(archive.extractfile("deployment.json"))
                self.assertEqual(set(archive.getnames()), {"deployment.json", *manifest["files"]})
                for name, digest in manifest["files"].items():
                    content = archive.extractfile(name).read()
                    self.assertNotIn(b"SECRET-SENTINEL", content)
                    self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
                self.assertEqual(result["file_count"], len(manifest["files"]))
            self.assertEqual((root / "source.tar.gz").stat().st_mode & 0o777, 0o600)

    def test_package_rejects_symlink_to_private_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "scripts/secret.py").symlink_to(root / ".runtime/host/credentials.json")
            with patch.object(remote, "ROOT", root), self.assertRaises(SystemExit):
                remote.package(root / "source.tar.gz")

    def test_remote_config_rejects_shell_flags_and_path_escape(self):
        base = dict(version=1, ssh="user@mini.local", directory="Coconut")
        for changes in [{"ssh": "-oProxyCommand=evil"}, {"ssh": "user@mini;echo bad"}, {"directory": "../Coconut"},
                        {"directory": "/Users/shared/Coconut"}, {"directory": "."}, {"directory": "a//b"},
                        {"version": True}, {"token": "secret"}]:
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                remote.validate_config(base | changes)
        self.assertEqual(remote.validate_config(base), base)
        # A configuration from before the relay also has the HTTPS address of the mini. Nothing uses it now.
        self.assertEqual(remote.validate_config(base | {"address": "https://mini.example.ts.net:8960"}), base)
        self.assertEqual(remote.validate_config(base | {"directory": "Apps/Coconut Local"})["directory"], "Apps/Coconut Local")

    def run_on_mini(self, directory, function, *, answer=None):
        """Run a remote command's Python in this process, with the mini's home in `directory`. Return its output."""
        def execute(_config, arguments):
            output = io.StringIO()
            with patch("pathlib.Path.home", return_value=Path(directory)), \
                 patch.object(sys, "argv", ["-c", "Coconut"]), patch.object(sys, "stdout", output), \
                 patch("urllib.request.urlopen", side_effect=answer or ConnectionRefusedError()) as opened:
                exec(arguments[2], {"__name__": "__main__"})
            self.opened = opened
            return subprocess.CompletedProcess(arguments, 0, output.getvalue().encode())

        with patch.object(remote, "ssh", side_effect=execute), patch.object(sys, "stdout", io.StringIO()) as output:
            function(dict(directory="Coconut"))
        return output.getvalue()

    def test_remote_status_handles_an_uninstalled_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Coconut").mkdir()
            self.assertEqual(json.loads(self.run_on_mini(directory, remote.status)), dict(installed=False, healthy=False))
            self.opened.assert_not_called()

    def installed(self, directory, tunnel_state):
        root = Path(directory) / "Coconut"
        (root / ".runtime/host/tunnel").mkdir(parents=True)
        (root / ".coconut-install.json").write_text('{"state":"installed","package_id":"package"}')
        if tunnel_state:
            (root / ".runtime/host/tunnel/status.json").write_text(json.dumps({"state": tunnel_state}))

    def healthy(self, url, timeout):
        self.assertEqual(url, "http://127.0.0.1:8961/health")
        return io.BytesIO(b'{"ok":true,"api_version":1}')

    def test_remote_status_checks_loopback_health_and_the_tunnel_over_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            self.installed(directory, "connected")
            result = json.loads(self.run_on_mini(directory, remote.status, answer=self.healthy))
        self.assertEqual(result, dict(installed=True, healthy=True, tunnel="connected", model_access_saved=False,
                                      package_id="package"))

    def test_remote_health_requires_the_service_and_reports_the_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            self.installed(directory, None)
            with self.assertRaisesRegex(SystemExit, "health check"):
                self.run_on_mini(directory, remote.verify_remote_health)
            output = self.run_on_mini(directory, remote.verify_remote_health, answer=self.healthy)
        self.assertIn("The relay tunnel on the mini is not_installed.", output)

    def test_installer_failure_keeps_the_remote_recovery_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "scripts/deploy.py").write_text("# receiver")
            result = subprocess.CompletedProcess([], 1,
                b'COCONUT_DEPLOY_RESULT={"ok":false,"error":"Previous source was restored."}\n', b"")
            with patch.object(remote, "ROOT", root), patch.object(remote, "preflight"), \
                 patch.object(remote, "ssh", return_value=result) as ssh, \
                 patch.object(sys, "stdout", io.StringIO()) as output, self.assertRaises(SystemExit):
                remote.deploy(dict(directory="Coconut"))
            self.assertIn("Previous source was restored.", output.getvalue())
            self.assertTrue(ssh.call_args.kwargs["allow_failure"])
            self.assertEqual(ssh.call_args.args[1][3:], ["receive", "--directory", "Coconut"])

    def test_revoke_removes_only_this_installations_exact_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            remote_home = Path(directory) / "mini"
            identity = root / ".runtime/ssh/coconut_ed25519"
            identity.parent.mkdir(parents=True)
            identity.write_text("private sentinel")
            identity.with_suffix(".pub").write_text("public sentinel")
            public = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode()
            tag = "coconut-deploy-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
            authorized = remote_home / ".ssh/authorized_keys"
            authorized.parent.mkdir(parents=True)
            retained = "# Keep this comment\nssh-ed25519 AAAAOTHERKEY unrelated\n"
            authorized.write_text(retained + public + " " + tag + "\n")
            authorized.chmod(0o600)

            def execute(_config, arguments, *, input_data):
                output = io.StringIO()
                with patch("pathlib.Path.home", return_value=remote_home), \
                     patch.object(sys, "stdin", io.StringIO(input_data.decode())), patch.object(sys, "stdout", output):
                    exec(arguments[2], {"__name__": "__main__"})
                return subprocess.CompletedProcess(arguments, 0, output.getvalue().encode())

            with patch.object(remote, "ROOT", root), patch.object(remote, "ssh", side_effect=execute), \
                 patch.object(remote.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, public + "\n")):
                remote.revoke_access({})
            self.assertEqual(authorized.read_text(), retained)
            self.assertEqual(authorized.stat().st_mode & 0o777, 0o600)
            self.assertFalse(identity.exists())
            self.assertFalse(identity.with_suffix(".pub").exists())

    def test_system_keygen_comment_does_not_change_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "id_ed25519"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "coconut test comment", "-f", str(key)], check=True)
            actual = remote.public_key(key)
            expected = " ".join(key.with_suffix(".pub").read_text().split()[:2])
            self.assertEqual(actual, expected)
            self.assertNotIn("comment", actual)


class PairingToolsTests(unittest.TestCase):
    def payload(self):
        return dict(version=2, relay="https://coconut-push.gumroad.workers.dev", route="c" * 64,
                    mac_key=base64.urlsafe_b64encode(bytes(32)).decode().rstrip("="),
                    secret=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="), expires=1_800_000_000, name="Mac mini")

    def test_a_mac_without_the_tunnel_cannot_pair(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pairing.host, "DEFAULT_DATA", Path(directory)), \
                self.assertRaisesRegex(SystemExit, "setup_coconut.py"):
            pairing.local_payload()

    @unittest.skipUnless(sys.platform == "darwin", "CoreImage and Vision require macOS.")
    def test_native_qr_roundtrip_keeps_exact_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/pairing-qr.swift").write_bytes((SCRIPTS / "pairing-qr.swift").read_bytes())
            with patch.object(pairing, "ROOT", root), patch.object(sys, "stdout", io.StringIO()):
                shared = root / "shared.png"
                pairing.render(self.payload(), show=False, share=True).rename(shared)
                image = pairing.render(self.payload(), show=False)
            decoder = root / "decode.swift"
            decoder.write_text('''import Foundation
import Vision
let request = VNDetectBarcodesRequest()
request.symbologies = [.qr]
try VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1])).perform([request])
guard let value = request.results?.first?.payloadStringValue else { exit(1) }
print(value)
''')
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)
            # Both codes hold the link, so a tap or the Camera app opens Coconut.
            for code in (image, shared):
                result = subprocess.run(["swift", str(decoder), str(code)], check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), pairing.link(self.payload()))


if __name__ == "__main__":
    unittest.main()
