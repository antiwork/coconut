#!/usr/bin/env python3
"""Install, update, and pair the Mac mini through SSH. The iPhone reaches the mini through the relay."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

import host


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".runtime/remote.json"
TOP_FILES = {"README.md", "Start Coconut.command", "Uninstall Coconut.command"}


def private_directory(path: Path) -> None:
    relative = path.relative_to(ROOT)
    current = ROOT
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SystemExit("The remote setup directory must not contain symbolic links.")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def validate_config(value: dict) -> dict:
    # A configuration from before the relay also has the HTTPS address of the mini. Nothing uses it now.
    if (not isinstance(value, dict) or set(value) - {"address"} != {"version", "ssh", "directory"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise SystemExit("Configure the Mac mini before continuing.")
    target = value["ssh"]
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", target):
        raise SystemExit("Use the macOS login and the host name of the mini, such as name@mini.local.")
    host.normalize_host(target.split("@", 1)[1])
    directory = value["directory"]
    if (not isinstance(directory, str) or not directory or not PurePosixPath(directory).parts or directory != str(PurePosixPath(directory))
            or PurePosixPath(directory).is_absolute() or any(part in {".", ".."} for part in PurePosixPath(directory).parts)
            or any(ord(character) < 32 for character in directory) or directory.startswith("-")):
        raise SystemExit("Use a relative folder below the Mac account's home, such as Coconut.")
    return dict(version=1, ssh=target, directory=directory)


def configuration() -> dict:
    if not CONFIG.is_file() or CONFIG.is_symlink():
        raise SystemExit("Run remote.py configure with the Mac mini login first.")
    return validate_config(json.loads(CONFIG.read_text()))


def source_files() -> dict[str, bytes]:
    candidates = [ROOT / name for name in TOP_FILES]
    candidates += list((ROOT / "backend/coconut").rglob("*.py"))
    candidates.append(ROOT / "backend/coconut/hermes_versions.json")
    candidates += list((ROOT / "scripts").glob("*.py"))
    candidates += list((ROOT / "scripts").glob("*.swift"))
    # The tunnel sidecar compiles the shared Noise and frame code with scripts/tunnel-sidecar.swift.
    candidates += list((ROOT / "ios/Tunnel").glob("*.swift"))
    candidates += list((ROOT / "docs").rglob("*.md"))
    # The skills that the person's Hermes loads from the install folder. See scripts/hermes_skills.py.
    candidates += list((ROOT / "skills").rglob("*.md"))
    # The `coconut` gateway platform that the person's Hermes loads. See backend/coconut/gateway_plugin.py.
    candidates += list((ROOT / "hermes_plugin/coconut").glob("*.py"))
    candidates += list((ROOT / "hermes_plugin/coconut").glob("plugin.yaml"))
    files = {}
    for path in sorted(candidates):
        relative = path.relative_to(ROOT)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != ROOT and ROOT in parent.parents):
            raise SystemExit("The source package cannot contain symbolic links.")
        if not path.is_file():
            raise SystemExit("A required source file is missing.")
        files[relative.as_posix()] = path.read_bytes()
    return files


def package(destination: Path) -> dict:
    files = source_files()
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    package_id = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = json.dumps(dict(version=1, files=hashes), sort_keys=True).encode()
    if destination.is_symlink():
        raise SystemExit("The package file must not be a symbolic link.")
    with tarfile.open(destination, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in [("deployment.json", manifest), *files.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o700 if name.endswith(".command") else 0o600
            archive.addfile(info, io.BytesIO(data))
    destination.chmod(0o600)
    return dict(package_id=package_id, file_count=len(files), bytes=destination.stat().st_size)


def ssh(config: dict, arguments: list[str], *, input_data: bytes | None = None,
        input_file=None, capture: bool = True, timeout: int = 30,
        allow_failure: bool = False) -> subprocess.CompletedProcess:
    private_directory(ROOT / ".runtime/ssh")
    known = ROOT / ".runtime/ssh/known_hosts"
    if known.is_symlink():
        raise SystemExit("The SSH host file must not be a symbolic link.")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={known}"]
    identity = ROOT / ".runtime/ssh/coconut_ed25519"
    if identity.exists():
        if identity.is_symlink() or identity.stat().st_mode & 0o077:
            raise SystemExit("The Coconut SSH key must be private to this account.")
        command += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    command += ["--", config["ssh"], shlex.join(arguments)]
    kwargs = dict(capture_output=capture, timeout=timeout)
    if input_file is not None:
        kwargs["stdin"] = input_file
    else:
        kwargs["input"] = input_data
    result = subprocess.run(command, **kwargs)
    if result.returncode and not allow_failure:
        if capture and result.stderr:
            # SSH diagnostics contain no pairing data or model keys.
            diagnostic = result.stderr.decode(errors="replace").strip()
            if len(diagnostic) < 2000:
                print(diagnostic, file=sys.stderr)
        raise SystemExit("The Mac mini command failed. Check the network, Remote Login, and SSH key access.")
    return result


def public_key(identity: Path) -> str:
    result = subprocess.run(["ssh-keygen", "-y", "-f", str(identity)], check=True, capture_output=True, text=True)
    fields = result.stdout.strip().split()
    try:
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError()
        decoded = base64.b64decode(fields[1], validate=True)
        if len(decoded) != 51 or not decoded.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
            raise ValueError()
    except ValueError:
        raise SystemExit("The Coconut SSH public key is invalid.") from None
    return " ".join(fields[:2])


def authorize(config: dict) -> None:
    directory = ROOT / ".runtime/ssh"
    private_directory(directory)
    identity = directory / "coconut_ed25519"
    public = directory / "coconut_ed25519.pub"
    known = directory / "known_hosts"
    if any(path.is_symlink() for path in (identity, public, known)):
        raise SystemExit("The SSH setup files must not be symbolic links.")
    if not identity.exists():
        tag = "coconut-deploy-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", tag, "-f", str(identity)], check=True)
    key = public_key(identity)
    tag = "coconut-deploy-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    line = key + " " + tag
    # Only the public key appears in the command. SSH reads the password from its terminal.
    script = """set -eu
umask 077
test ! -L "$HOME/.ssh"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
test ! -L "$HOME/.ssh/authorized_keys"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
""" + "if ! grep -Fqx -- " + shlex.quote(line) + ' "$HOME/.ssh/authorized_keys"; then\n' + "  printf '%s\\n' " + shlex.quote(line) + ' >> "$HOME/.ssh/authorized_keys"\nfi\n'
    print("Enter the Mac mini password in this terminal. SSH does not display it.", flush=True)
    result = subprocess.run(["ssh", "-o", "BatchMode=no", "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}", "--", config["ssh"], script])
    if result.returncode:
        raise SystemExit("SSH access was not saved. Check the macOS login and Remote Login settings.")
    print("Coconut SSH access is ready. Future updates use its private key.")


def revoke_access(config: dict) -> None:
    identity = ROOT / ".runtime/ssh/coconut_ed25519"
    if not identity.is_file() or identity.is_symlink():
        raise SystemExit("No Coconut SSH key exists in this installation.")
    key = public_key(identity)
    tag = "coconut-deploy-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    code = """
import os, pathlib, sys, tempfile
directory = pathlib.Path.home() / '.ssh'
path = directory / 'authorized_keys'
if directory.is_symlink() or path.is_symlink():
    raise SystemExit('The SSH authorization path must not contain symbolic links.')
line = sys.stdin.read().strip()
if path.exists():
    lines = path.read_text().splitlines(keepends=True)
    retained = [value for value in lines if value.strip() != line]
    if retained != lines:
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as handle:
            pending = pathlib.Path(handle.name)
            os.chmod(pending, path.stat().st_mode & 0o777)
            handle.writelines(retained)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
print('Removed this installation\u2019s SSH authorization.')
"""
    result = ssh(config, ["python3", "-c", code], input_data=(key + " " + tag).encode())
    identity.unlink()
    identity.with_suffix(".pub").unlink(missing_ok=True)
    print(result.stdout.decode().strip())


ROOT_CODE = """
import json, os, pathlib, sys
directory = pathlib.PurePosixPath(sys.argv[1])
if directory.is_absolute() or not directory.parts or any(p in ('.', '..') for p in directory.parts):
    raise SystemExit('Invalid installation directory.')
root = pathlib.Path.home()
for part in directory.parts:
    root = root / part
    if root.is_symlink():
        raise SystemExit('The installation path contains a symbolic link.')
"""


# Run on the mini: health() checks /health on the loopback listener of the service and reads the state of the tunnel sidecar.
HEALTH_CODE = ROOT_CODE + """
def health():
    import urllib.request
    sys.path.insert(0, str(root / 'scripts'))
    import host
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % host.TUNNEL_PORT, timeout=5) as response:
            healthy = json.load(response) == {'ok': True, 'api_version': 1}
    except Exception:
        healthy = False
    try:
        tunnel = json.loads((root / '.runtime/host/tunnel/status.json').read_text()).get('state', 'unknown')
    except (OSError, ValueError, AttributeError):
        tunnel = 'not_installed'
    return {'healthy': healthy, 'tunnel': tunnel}
"""


def verify_remote_health(config: dict) -> None:
    code = HEALTH_CODE + """
print(json.dumps(health()))
"""
    result = json.loads(ssh(config, ["python3", "-c", code, config["directory"]]).stdout)
    if not result["healthy"]:
        raise SystemExit("Coconut is installed, but its service did not pass the health check on the mini.")
    if result["tunnel"] != "connected":
        print(f"The relay tunnel on the mini is {result['tunnel']}. The iPhone cannot reach the mini until it connects.")


def preflight(config: dict) -> dict:
    code = ROOT_CODE + """
import platform, shutil, subprocess
clt = subprocess.run(['/usr/bin/xcode-select', '-p'], capture_output=True).returncode == 0
print(json.dumps({'platform': platform.system(), 'machine': platform.machine(),
    'python': platform.python_version(), 'command_line_tools': clt,
    'git': bool(shutil.which('git')),
    'directory': str(root), 'installed': (root / '.coconut-install.json').is_file()}))
"""
    result = ssh(config, ["python3", "-c", code, config["directory"]])
    metadata = json.loads(result.stdout)
    if metadata["platform"] != "Darwin" or tuple(map(int, metadata["python"].split(".")[:2])) < (3, 9):
        raise SystemExit("The installer requires macOS and Python 3.9 or later from Xcode Command Line Tools.")
    if not metadata["command_line_tools"] or not metadata["git"]:
        raise SystemExit("Install Xcode Command Line Tools on the mini with xcode-select --install, then retry.")
    return metadata


def deploy(config: dict) -> None:
    preflight(config)
    private_directory(ROOT / ".runtime/deploy")
    with tempfile.TemporaryDirectory(dir=ROOT / ".runtime/deploy", prefix="upload-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        summary = package(archive)
        print(f"Sending {summary['file_count']} source files to the Mac mini.", flush=True)
        receiver = (ROOT / "scripts/deploy.py").read_text()
        arguments = ["python3", "-c", receiver, "receive", "--directory", config["directory"]]
        with archive.open("rb") as handle:
            result = ssh(config, arguments, input_file=handle, timeout=2400, allow_failure=True)
        lines = result.stdout.decode().splitlines()
        results = [line.removeprefix("COCONUT_DEPLOY_RESULT=") for line in lines if line.startswith("COCONUT_DEPLOY_RESULT=")]
        if len(results) != 1:
            raise SystemExit("The deployment result is unknown. Run remote.py status before attempting another update.")
        receipt = json.loads(results[0])
        print(json.dumps(receipt, indent=2))
        if result.returncode or receipt.get("ok") is not True:
            raise SystemExit("Coconut kept the deployment result above. Resolve that error before retrying.")
        if receipt.get("health", {}).get("ok"):
            verify_remote_health(config)
            print("Verified the Mac mini service over SSH.")


def status(config: dict) -> None:
    code = HEALTH_CODE + """
def model_keys_in_hermes():
    # The service moves the saved model keys into the .env of the default Hermes profile.
    path = pathlib.Path.home() / '.hermes/.env'
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    return any(line.strip().removeprefix('export ').split('=', 1)[0].strip() in host_keys and
               line.split('=', 1)[-1].strip().strip(chr(34) + chr(39)) for line in lines if '=' in line)
host_keys = ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')
marker = root / '.coconut-install.json'
if not marker.is_file():
    print(json.dumps({'installed': False, 'healthy': False}))
else:
    print(json.dumps(dict(health(), installed=True,
        model_access_saved=(root / '.runtime/host/credentials.json').is_file() or model_keys_in_hermes(),
        package_id=json.loads(marker.read_text()).get('package_id'))))
"""
    result = ssh(config, ["python3", "-c", code, config["directory"]])
    print(json.dumps(json.loads(result.stdout), indent=2))


def hermes_env(home: Path) -> dict:
    """The values in the `.env` of a Hermes home, read as plain KEY=VALUE lines."""
    path = home / ".env"
    if not path.is_file() or path.is_symlink():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def local_model_keys() -> dict:
    """The model keys of this Mac: the keys that Coconut saved, else the keys of the default Hermes profile.

    The service moves the saved keys into the `.env` of the default Hermes profile at its start (model_keys.py).
    """
    path = ROOT / ".runtime/host/credentials.json"
    if path.is_file():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise SystemExit("Save private model access locally before copying it to the mini.")
        values = json.loads(path.read_text())
    else:
        values = hermes_env(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"))
    return {key: value for key, value in values.items() if key in host.MODEL_KEYS and isinstance(value, str) and value}


def model_access(config: dict) -> None:
    values = local_model_keys()
    if not values:
        raise SystemExit("No supported model keys are saved on this Mac.")
    code = ROOT_CODE + """
import tempfile
if not (root / '.coconut-install.json').is_file():
    raise SystemExit('Install Coconut before copying model access.')
data = root / '.runtime/host'
if data.is_symlink() or (root / '.runtime').is_symlink() or (data / 'credentials.json').is_symlink():
    raise SystemExit('The credential path contains a symbolic link.')
values = json.load(sys.stdin)
if not isinstance(values, dict) or not values or any(k not in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY') or not isinstance(v, str) or not v for k, v in values.items()):
    raise SystemExit('Invalid model access.')
os.umask(0o077)
with tempfile.NamedTemporaryFile(mode='w', dir=data, delete=False) as handle:
    pending = pathlib.Path(handle.name)
    json.dump(values, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(pending, data / 'credentials.json')
import subprocess
subprocess.run([sys.executable, str(root / 'scripts/host.py'), 'restart'], check=True, stdout=subprocess.DEVNULL)
print('Model access saved. Coconut restarted.')
"""
    result = ssh(config, ["python3", "-c", code, config["directory"]], input_data=json.dumps(values).encode(), timeout=90)
    print(result.stdout.decode().strip())


def pair(config: dict) -> None:
    verify_remote_health(config)
    code = ROOT_CODE + """
if not (root / '.coconut-install.json').is_file():
    raise SystemExit('Install Coconut before pairing.')
sys.path.insert(0, str(root / 'scripts'))
import pairing
print(json.dumps(pairing.local_payload()))
"""
    result = ssh(config, ["python3", "-c", code, config["directory"]])
    import pairing
    pairing.render(json.loads(result.stdout))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure", help="Save the mini's SSH login locally.")
    setup.add_argument("--ssh", required=True)
    setup.add_argument("--directory", default="Coconut")
    for name in ("install", "update"):
        command = commands.add_parser(name, help="Send source files and verify the Mac mini service.")
        command.add_argument("--copy-model-access", action="store_true", help="Copy this Mac's saved model keys privately over SSH.")
    for name in ("doctor", "status", "pair", "model-access", "authorize", "revoke-access"):
        commands.add_parser(name)
    commands.add_parser("package", help="Build a source-only package without contacting the mini.")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            config = validate_config(dict(version=1, ssh=args.ssh, directory=args.directory))
            private_directory(CONFIG.parent)
            if CONFIG.is_symlink():
                raise SystemExit("The remote configuration must not be a symbolic link.")
            CONFIG.write_text(json.dumps(config, indent=2) + "\n")
            CONFIG.chmod(0o600)
            print("Mac mini saved. Run python3 scripts/remote.py install to install Coconut.")
        elif args.command == "package":
            private_directory(ROOT / ".runtime/deploy")
            archive = ROOT / ".runtime/deploy/source.tar.gz"
            print(json.dumps(dict(path=str(archive), **package(archive)), indent=2))
        else:
            config = configuration()
            if args.command in {"install", "update"}:
                deploy(config)
                if args.copy_model_access:
                    model_access(config)
            elif args.command == "doctor":
                print(json.dumps(preflight(config), indent=2))
            else:
                {"status": status, "pair": pair, "model-access": model_access,
                 "authorize": authorize, "revoke-access": revoke_access}[args.command](config)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise SystemExit("The command did not finish. Check the Mac connection and run remote.py status before retrying an update.") from None


if __name__ == "__main__":
    main()
