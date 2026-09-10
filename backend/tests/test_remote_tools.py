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
        for name in remote.TOP_FILES | {"backend/coconut/service.py", "scripts/host.py", "docs/setup.md"}:
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
        base = dict(version=1, ssh="user@mini.example.ts.net", directory="Coconut", address="https://mini.example.ts.net:8960")
        for changes in [{"ssh": "-oProxyCommand=evil"}, {"ssh": "user@mini;echo bad"}, {"directory": "../Coconut"},
                        {"directory": "/Users/shared/Coconut"}, {"directory": "."}, {"directory": "a//b"},
                        {"address": "https://user:secret@mini:8960"}, {"address": "http://mini:8960"}, {"version": True}]:
            with self.subTest(changes=changes), self.assertRaises(SystemExit):
                remote.validate_config(base | changes)
        self.assertEqual(remote.validate_config(base)["address"], base["address"])
        self.assertEqual(remote.validate_config(base | {"directory": "Apps/Coconut Local"})["directory"], "Apps/Coconut Local")

    def test_remote_status_handles_an_uninstalled_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Coconut"
            root.mkdir()
            (root / ".coconut-install.json").write_text('{"state":"installed"}')
            config = dict(directory="Coconut")

            def execute(_config, arguments):
                output = io.StringIO()
                with patch("pathlib.Path.home", return_value=Path(directory)), \
                     patch.object(sys, "argv", ["-c", "Coconut"]), patch.object(sys, "stdout", output):
                    exec(arguments[2], {"__name__": "__main__"})
                return subprocess.CompletedProcess(arguments, 0, output.getvalue().encode())

            with patch.object(remote, "ssh", side_effect=execute), patch.object(sys, "stdout", io.StringIO()) as output:
                remote.status(config)
            self.assertEqual(json.loads(output.getvalue()), dict(installed=False, source_present=True, healthy=False))

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
                remote.deploy(dict(directory="Coconut", address="https://mini:8960"))
            self.assertIn("Previous source was restored.", output.getvalue())
            self.assertTrue(ssh.call_args.kwargs["allow_failure"])

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
        return dict(version=1, server_url="https://mini.example.ts.net:8960", certificate_sha256="a" * 64, pairing_code="0123456789")

    def test_pairing_rejects_unsafe_payloads(self):
        for changes in [{"version": True}, {"version": 2}, {"server_url": "http://mini:8960"},
                        {"server_url": "https://mini:8960/?token=secret"}, {"pairing_code": "١٢٣٤٥٦٧٨٩٠"},
                        {"pairing_code": "12345"}, {"certificate_sha256": "a" * 63}, {"device_token": "secret"}]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, SystemExit)):
                pairing.validate(self.payload() | changes)

    @unittest.skipUnless(sys.platform == "darwin", "CoreImage and Vision require macOS.")
    def test_native_qr_roundtrip_keeps_exact_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/pairing-qr.swift").write_bytes((SCRIPTS / "pairing-qr.swift").read_bytes())
            with patch.object(pairing, "ROOT", root):
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
            result = subprocess.run(["swift", str(decoder), str(image)], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), self.payload())
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
