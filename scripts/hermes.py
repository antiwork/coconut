#!/usr/bin/env python3
"""Install or check the removable Hermes runtime inside this project."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
HERMES = RUNTIME / "hermes"
SOURCE = HERMES / "source"
VENV = HERMES / "venv"
PYTHON = VENV / "bin/python"
REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
REVISION = "71d34bad1045610252eda02ee79549436896e784"
MANIFEST = HERMES / "install.json"
UV_VERSION = "0.12.10"
UV_RELEASE = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}"
# Official release checksums: https://github.com/astral-sh/uv/releases/tag/0.12.10
UV_SHA256 = {
    "aarch64-apple-darwin": "51c6170e8e3a01cef9f33b94f582b7b81ac65046f55d40afb35f9cff5a68c179",
    "x86_64-apple-darwin": "5296d5aa2b9143360405eea866f8ef4d5dc8986b164eb0dc35e8f876a9304d30",
}
UV_MAX_BYTES = 128 * 1024 * 1024


def private_directory(path: Path) -> None:
    if path != ROOT and ROOT not in path.parents:
        raise SystemExit("The installer can write only inside this project.")
    for parent in [path, *path.parents]:
        if parent == ROOT:
            break
        if parent.is_symlink():
            raise SystemExit(f"The runtime contains a symbolic link: {parent}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def prerequisites() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit("Run this installer with Python 3.9 or newer.")
    if sys.platform == "darwin":
        selected = subprocess.run(["/usr/bin/xcode-select", "-p"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if selected.returncode:
            raise SystemExit("Install Apple Command Line Tools with xcode-select --install. Then rerun this installer.")
    git = shutil.which("git")
    if not git:
        raise SystemExit("Install Git before running the Hermes installer.")
    available = subprocess.run([git, "--version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
    if available.returncode:
        raise SystemExit("Git is not ready. Finish Apple Command Line Tools or Git setup before continuing.")


def uv_target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64", "x86_64"}:
        return ("aarch64" if machine in {"arm64", "aarch64"} else machine) + "-apple-darwin"
    raise SystemExit("The local uv bootstrap supports Apple Silicon and Intel Macs. This platform needs uv on PATH.")


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def download_uv(url: str, destination: Path) -> None:
    try:
        with urlopen(url, timeout=60) as response, destination.open("xb") as output:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > UV_MAX_BYTES:
                    raise SystemExit("The uv download exceeds its expected size limit.")
                output.write(chunk)
    except (OSError, URLError):
        raise SystemExit("The uv download failed. Check this Mac's HTTPS connection and retry install.") from None


def extract_uv(archive: Path, destination: Path, target: str, expected_sha256: str) -> dict[str, str]:
    if file_sha256(archive) != expected_sha256:
        raise SystemExit("The uv checksum does not match the pinned release. Nothing was installed.")
    private_directory(destination)
    root = "uv-" + target
    seen = set()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle:
                name = member.name.removeprefix("./").rstrip("/")
                if name in seen:
                    raise SystemExit("The uv archive contains duplicate entries.")
                seen.add(name)
                if name == root and member.isdir():
                    continue
                if (name not in {root + "/uv", root + "/uvx"} or not member.isfile()
                        or member.sparse is not None or not 0 < member.size <= UV_MAX_BYTES):
                    raise SystemExit("The uv archive contains an unexpected path or file type.")
                # Copy regular file bytes only. Archive paths, links, owners, and modes cannot affect the host.
                source = bundle.extractfile(member)
                if source is None:
                    raise SystemExit("The uv archive lacks executable content.")
                output_path = destination / name.rsplit("/", 1)[1]
                with source, output_path.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                output_path.chmod(0o700)
        if not {root + "/uv", root + "/uvx"}.issubset(seen):
            raise SystemExit("The uv archive lacks its expected executables.")
    except (tarfile.TarError, EOFError):
        raise SystemExit("The uv archive is invalid. Nothing was installed.") from None
    return {name: file_sha256(destination / name) for name in ("uv", "uvx")}


def managed_uv() -> str:
    existing = shutil.which("uv")
    if existing:
        return existing
    target = uv_target()
    tools = RUNTIME / "tools"
    private_directory(tools)
    destination = tools / f"uv-{UV_VERSION}-{target}"
    receipt_path = destination / "install.json"
    if destination.exists() or destination.is_symlink():
        private_directory(destination)
        try:
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise ValueError()
            receipt = json.loads(receipt_path.read_text())
            if (receipt.get("version") != UV_VERSION or receipt.get("target") != target
                    or receipt.get("archive_sha256") != UV_SHA256[target]):
                raise ValueError()
            for name in ("uv", "uvx"):
                binary = destination / name
                if (binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK)
                        or file_sha256(binary) != receipt["binaries"][name]):
                    raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("The managed uv installation changed or is incomplete. Inspect its folder before retrying.") from None
        return str(destination / "uv")
    print(f"Downloading verified uv {UV_VERSION} into .runtime/tools.", flush=True)
    with tempfile.TemporaryDirectory(prefix="uv-download-", dir=tools) as temporary:
        staging = Path(temporary)
        archive = staging / "uv.tar.gz"
        download_uv(f"{UV_RELEASE}/uv-{target}.tar.gz", archive)
        extracted = staging / "bin"
        binaries = extract_uv(archive, extracted, target, UV_SHA256[target])
        (extracted / "install.json").write_text(json.dumps({
            "version": UV_VERSION, "target": target, "archive_sha256": UV_SHA256[target],
            "binaries": binaries,
        }, indent=2) + "\n")
        (extracted / "install.json").chmod(0o600)
        extracted.rename(destination)
    return str(destination / "uv")


def environment() -> dict[str, str]:
    # Dependency builds do not receive model credentials or the user's Hermes config.
    names = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SYSTEMROOT")
    result = {name: os.environ[name] for name in names if name in os.environ}
    paths = {
        "UV_CACHE_DIR": RUNTIME / "cache/uv",
        "UV_PYTHON_INSTALL_DIR": HERMES / "python",
        "UV_PYTHON_BIN_DIR": HERMES / "bin",
        "UV_TOOL_DIR": HERMES / "tools",
        "UV_TOOL_BIN_DIR": HERMES / "bin",
        "UV_PROJECT_ENVIRONMENT": VENV,
        "XDG_CACHE_HOME": RUNTIME / "cache",
        "XDG_CONFIG_HOME": HERMES / "config",
        "XDG_DATA_HOME": HERMES / "data",
        "HERMES_HOME": HERMES / "check-home",
        "PYTHONPYCACHEPREFIX": RUNTIME / "cache/pycache",
        "TMPDIR": RUNTIME / "tmp",
    }
    for name, path in paths.items():
        if path.is_symlink():
            raise SystemExit(f"The runtime contains a symbolic link: {path}")
        if name != "UV_PROJECT_ENVIRONMENT":
            private_directory(path)
        result[name] = str(path)
    result.update({"PYTHONNOUSERSITE": "1", "UV_NO_PROGRESS": "1",
                   "UV_LINK_MODE": "copy", "UV_MANAGED_PYTHON": "1",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0"})
    return result


def run(command: list[str], *, env: dict[str, str], cwd: Path = ROOT,
        capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=True, text=True,
                          capture_output=capture, timeout=900)


def source_revision(env: dict[str, str]) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=SOURCE, env=env, capture=True).stdout.strip()


def install(revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("Use a full 40-character Hermes commit ID for --revision.")
    prerequisites()
    uv = managed_uv()
    env = environment()
    private_directory(HERMES)
    private_directory(SOURCE)
    if not (SOURCE / ".git").exists():
        if any(SOURCE.iterdir()):
            raise SystemExit("The source folder contains unrelated files. The installer kept them intact.")
        run(["git", "init", "--quiet"], cwd=SOURCE, env=env)
        run(["git", "remote", "add", "origin", REPOSITORY], cwd=SOURCE, env=env)
    remote = run(["git", "remote", "get-url", "origin"], cwd=SOURCE, env=env, capture=True).stdout.strip()
    if remote != REPOSITORY:
        raise SystemExit("The source remote does not match Hermes. The installer kept it intact.")
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=SOURCE,
                          env=env, text=True, capture_output=True)
    if head.returncode == 0 and head.stdout.strip() != revision:
        raise SystemExit("A different Hermes commit exists. Remove its runtime before changing the pinned commit.")
    if head.returncode:
        print(f"Downloading Hermes {revision[:12]} into .runtime.", flush=True)
        run(["git", "fetch", "--depth=1", "origin", revision], cwd=SOURCE, env=env)
        run(["git", "-c", "core.hooksPath=" + os.devnull,
             "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=SOURCE, env=env)
    if source_revision(env) != revision:
        raise SystemExit("The downloaded source does not match the requested commit.")
    changed = run(["git", "status", "--porcelain", "--untracked-files=no"],
                  cwd=SOURCE, env=env, capture=True).stdout.strip()
    if changed:
        raise SystemExit("The Hermes source has local changes. The installer kept them intact.")
    if not (SOURCE / "LICENSE").is_file() or not (SOURCE / "uv.lock").is_file():
        raise SystemExit("The source lacks its license or dependency lock. Installation stopped.")
    print("Installing project-local Python 3.14 and Hermes dependencies.", flush=True)
    run([uv, "python", "install", "3.14", "--no-bin"], env=env)
    run([uv, "sync", "--frozen", "--no-dev", "--extra", "anthropic", "--extra", "mcp",
         "--python", "3.14"], cwd=SOURCE, env=env)
    MANIFEST.write_text(json.dumps({
        "repository": REPOSITORY, "commit": revision,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "python": str(PYTHON.relative_to(ROOT)), "source": str(SOURCE.relative_to(ROOT)),
        "extras": ["anthropic", "mcp"], "dependency_lock": "upstream uv.lock",
    }, indent=2) + "\n")
    MANIFEST.chmod(0o600)
    check()


CHECK_CODE = """
import json
import sys
def no_network(event, arguments):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('The Hermes compatibility check cannot use the network.')
sys.addaudithook(no_network)
from PIL import Image
from coconut.hermes_contract import verify_contract
print(json.dumps(verify_contract()))
"""


def check_source(env: dict[str, str]) -> str:
    origin = run(['git', 'remote', 'get-url', 'origin'], cwd=SOURCE, env=env, capture=True).stdout.strip()
    if origin != REPOSITORY:
        raise SystemExit('The source remote does not match Hermes.')
    revision = source_revision(env)
    if not MANIFEST.is_file() or MANIFEST.is_symlink():
        raise SystemExit('The Hermes installation receipt is missing or invalid.')
    receipt = json.loads(MANIFEST.read_text())
    if receipt.get('repository') != REPOSITORY or receipt.get('commit') != revision:
        raise SystemExit('The source commit differs from the recorded installation.')
    changed = run(['git', 'status', '--porcelain', '--untracked-files=all'],
                  cwd=SOURCE, env=env, capture=True).stdout.strip()
    if changed:
        raise SystemExit('The Hermes source has local changes. Keep Coconut code outside its checkout.')
    return revision


def check() -> None:
    if not PYTHON.exists() or not (SOURCE / "run_agent.py").is_file():
        raise SystemExit("Hermes is not installed in this project. Run install with --revision.")
    env = environment()
    revision = check_source(env)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / 'backend'), str(SOURCE)))
    run([str(PYTHON), "-c", CHECK_CODE], cwd=HERMES / "check-home", env=env)
    if check_source(env) != revision:
        raise SystemExit('The Hermes source changed during its compatibility check.')
    run([str(PYTHON), str(ROOT / "scripts/test_concurrent_hermes.py"),
         "--source", str(SOURCE), "--python", str(PYTHON)], cwd=HERMES / "check-home", env=env)
    if check_source(env) != revision:
        raise SystemExit("The Hermes source changed during its concurrency check.")
    run([str(PYTHON), str(ROOT / "scripts/test_native_kanban.py"),
         "--source", str(SOURCE), "--python", str(PYTHON)], cwd=HERMES / "check-home", env=env)
    if check_source(env) != revision:
        raise SystemExit("The Hermes source changed during its Kanban check.")
    print(f"Hermes commit: {revision}")
    print(f"Runtime: {HERMES}")
    print("Remove the runtime and local data: python3 scripts/cleanup.py --yes")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="Install one explicit upstream commit inside .runtime.")
    setup.add_argument("--revision", default=REVISION,
                       help="The full Hermes commit ID. The default is the verified project version.")
    commands.add_parser("check", help="Check imports and the provider API without sending a model request.")
    profiles = commands.add_parser("profiles", help="Inspect this project's Hermes profiles without running an agent.")
    profiles.add_argument("--data-dir", type=Path, default=RUNTIME / "host")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    listing = profile_commands.add_parser("list", help="List profiles from the local Coconut database.")
    listing.add_argument("--include-deleted", action="store_true")
    show = profile_commands.add_parser("show", help="Show one profile's identity and local home.")
    show.add_argument("profile_id")
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.revision)
        elif args.command == "profiles":
            sys.path.insert(0, str(ROOT / "backend"))
            from coconut.profiles import inspect_profiles
            data_dir = args.data_dir.expanduser().resolve()
            if RUNTIME.resolve() not in data_dir.parents:
                raise SystemExit("Choose a data directory inside this project's .runtime folder.")
            results = inspect_profiles(data_dir, getattr(args, "profile_id", None),
                                       include_deleted=args.profile_command == "show" or args.include_deleted)
            print(json.dumps(results[0] if args.profile_command == "show" else results, indent=2))
        else:
            check()
    except (ValueError, OSError) as error:
        if args.command == "profiles":
            raise SystemExit("The profile inspection failed. Check the profile ID and local data directory.") from None
        raise
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"The local installation command failed with exit code {error.returncode}.") from None


if __name__ == "__main__":
    main()
