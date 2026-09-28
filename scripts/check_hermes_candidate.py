#!/usr/bin/env python3
"""Run the native upgrade checks against a candidate Hermes runtime and save one JSON result."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import hermes
import host

# Hermes sets these for a native Kanban worker and every process that it starts.
FENCE_KEYS = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK")
EXECUTED_PATHS = ("backend/", "scripts/")
RESULT_PREFIX = "COCONUT_CREATION_RESULT="
TAIL_CHARACTERS = 1500
FENCED_MESSAGE = ("This process descends from a native Kanban worker, and Hermes keeps its Kanban boards read-only. "
                  "The checks create disposable boards. Run this script from a process outside the worker, "
                  "such as a queued maintenance service.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=path, text=True, capture_output=True,
                          check=True, timeout=60).stdout.strip()


def fence(env) -> list[str]:
    return [key for key in FENCE_KEYS if env.get(key)]


def default_python(runtime: Path) -> Path:
    for candidate in (runtime / "venv/bin/python", runtime / "source/.venv/bin/python"):
        if candidate.is_file():
            return candidate
    raise SystemExit("The runtime has no virtual environment. Supply --python.")


def identity(code: Path, source: Path) -> dict:
    """Name the exact Coconut and Hermes code that the checks run."""
    changes = git(code, "diff", "--name-only", "HEAD", "--").splitlines()
    # Untracked files in the executed folders also run, and the commit does not name them.
    changes += git(code, "ls-files", "--others", "--exclude-standard", "--", *EXECUTED_PATHS).splitlines()
    changes = sorted(set(filter(None, changes)))
    return dict(source_sha=git(code, "rev-parse", "HEAD"), source_tree=git(code, "rev-parse", "HEAD^{tree}"),
                source_changes=changes, hermes_sha=git(source, "rev-parse", "HEAD"),
                hermes_repository=git(source, "remote", "get-url", "origin"),
                hermes_changes=sorted(filter(None, git(source, "diff", "--name-only", "HEAD", "--").splitlines())))


def identity_problem(value: dict) -> str | None:
    executed = [name for name in value["source_changes"] if name.startswith(EXECUTED_PATHS)]
    if executed:
        return "Commit the Coconut changes before the checks: " + ", ".join(executed)
    if value["hermes_repository"] != hermes.REPOSITORY:
        return "The candidate source remote does not match Hermes."
    if value["hermes_changes"]:
        return "The candidate Hermes source has local changes: " + ", ".join(value["hermes_changes"])
    return None


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise SystemExit(f"The check folder must not be a symbolic link: {path}")
    path.chmod(0o700)
    return path


@contextmanager
def runtime_lock(runtime: Path):
    with (private_directory(runtime / "checks") / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another candidate check uses this runtime. Wait for its result.") from None
        yield


@contextmanager
def candidate_root(runtime: Path, python: Path, code: Path):
    """Build the Coconut root layout that the check programs expect, inside the candidate runtime."""
    root = Path(tempfile.mkdtemp(prefix="candidate-", dir=private_directory(runtime / "checks")))
    try:
        for folder in (".runtime/hermes/check-home", ".runtime/checks", ".runtime/host", "tmp"):
            (root / folder).mkdir(parents=True, mode=0o700)
        # The model catalog resolves the source link. Its native profile stays in runtime/terminal.
        (root / ".runtime/hermes/source").symlink_to(runtime / "source")
        (root / ".runtime/hermes/venv").symlink_to(python.parent.parent)
        (root / "backend").symlink_to(code / "backend")
        (root / "scripts").symlink_to(code / "scripts")
        yield root
    finally:
        # rmtree removes the links and does not follow them.
        shutil.rmtree(root, ignore_errors=True)


def programs():
    import test_native_profiles
    import test_native_relay
    import test_native_routines
    import test_script_routines
    return dict(routines=test_native_routines.PROGRAM, script_routines=test_script_routines.PROGRAM,
                relay=test_native_relay.PROGRAM, profiles=test_native_profiles.PROGRAMS)


def plan(root: Path, code: Path, python: Path, texts: dict) -> list:
    """Return (check, needs model credentials, [(label, arguments, result kind, timeout)])."""
    source = str(root / ".runtime/hermes/source")
    def program(text):
        return [str(python), "-c", text, str(root), str(code)]
    def native(script):
        return [str(python), str(code / "scripts" / script), "--source", source, "--python", str(python)]
    return [
        ("contracts", False, [("contract", [str(python), "-c", hermes.CHECK_CODE], "exit", 120),
                              ("concurrency", native("test_concurrent_hermes.py"), "exit", 900),
                              ("kanban", native("test_native_kanban.py"), "exit", 900)]),
        ("routines", False, [("routines", program(texts["routines"]), "report", 430)]),
        ("script_routines", False, [("script-routines", program(texts["script_routines"]), "report", 430)]),
        ("relay", True, [("relay", program(texts["relay"]), "report", 430)]),
        ("profiles", True, [("profiles-" + name, program(text), "pass", 430)
                            for name, text in zip(("metadata", "approval", "routine"), texts["profiles"])]),
    ]


def child_env(root: Path, code: Path, model: bool) -> dict:
    # Only the model-backed checks receive model credentials.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HERMES_", "COCONUT_HERMES_")) and (model or key not in host.MODEL_KEYS)}
    env.update(PYTHONPATH=os.pathsep.join((str(code / "backend"), str(root / ".runtime/hermes/source"))),
               HERMES_HOME=str(root / ".runtime/hermes/check-home"), TMPDIR=str(root / "tmp"),
               PYTHONPYCACHEPREFIX=str(root / "tmp/pycache"), PYTHONUNBUFFERED="1")
    return env


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def execute(arguments: list[str], env: dict, cwd: Path, timeout: float):
    started = time.monotonic()
    process = subprocess.Popen(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    try:
        # Stop workers that outlive their check.
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return process.returncode, timed_out, stdout, stderr, round(time.monotonic() - started, 2)


def outcome(kind: str, returncode: int, stdout: str) -> tuple[bool, dict | None]:
    if kind == "report":
        lines = [line[len(RESULT_PREFIX):] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
        report = json.loads(lines[0]) if len(lines) == 1 else None
        return returncode == 0 and bool(report) and report.get("passed") is True, report
    if kind == "pass":
        return returncode == 0 and any(line.startswith("PASS:") for line in stdout.splitlines()), None
    return returncode == 0, None


def run_checks(root: Path, code: Path, python: Path, logs: Path, secrets: list[str], texts: dict) -> dict:
    results = {}
    contracts_passed = True
    for check, model, steps in plan(root, code, python, texts):
        if not contracts_passed:
            results[check] = dict(passed=False, status="skipped", programs=[])
            continue
        entries = []
        for label, arguments, kind, timeout in steps:
            returncode, timed_out, stdout, stderr, elapsed = execute(arguments, child_env(root, code, model), root, timeout)
            stdout, stderr = redact(stdout, secrets), redact(stderr, secrets)
            passed, report = outcome(kind, returncode, stdout)
            log = logs / (label + ".log")
            log.write_text(f"exit: {returncode}\ntimed out: {timed_out}\n--- stdout\n{stdout}\n--- stderr\n{stderr}")
            log.chmod(0o600)
            entry = dict(label=label, passed=passed, returncode=returncode, timed_out=timed_out,
                         elapsed_seconds=elapsed, log=str(log), stdout_tail=stdout[-TAIL_CHARACTERS:],
                         stderr_tail=stderr[-TAIL_CHARACTERS:])
            if kind == "report":
                entry["report"] = report
            entries.append(entry)
            print(json.dumps(dict(check=check, label=label, passed=passed, elapsed_seconds=elapsed)), flush=True)
        passed = all(entry["passed"] for entry in entries)
        results[check] = dict(passed=passed, status="passed" if passed else "failed", programs=entries)
        if check == "contracts":
            contracts_passed = passed
    return results


def write_result(path: Path, value: dict) -> None:
    private_directory(path.parent)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def check_candidate(runtime: Path, host_data: Path, *, python: Path | None = None, output: Path | None = None,
                    code: Path = ROOT, texts: dict | None = None) -> tuple[Path, dict]:
    runtime = runtime.expanduser().resolve()
    if not (runtime / "source/run_agent.py").is_file():
        raise SystemExit("The runtime needs a Hermes checkout in its source folder.")
    # Keep the virtual environment path. Its interpreter is often a link to a shared Python.
    python = Path(os.path.abspath(python or default_python(runtime)))
    before = identity(code, runtime / "source")
    output = output or runtime / "checks/results" / f"{before['source_sha']}-{before['hermes_sha']}.json"
    output = Path(os.path.abspath(output))
    result = dict(version=1, check="hermes-candidate", passed=False, started_at=now(), runtime=str(runtime),
                  python=str(python), repository=hermes.REPOSITORY, **before)
    problem = identity_problem(before)
    fenced = fence(os.environ)
    if fenced:
        # Do not remove the fence. Hermes applies it to protect the worker's own board.
        problem = FENCED_MESSAGE
        result["fence"] = fenced
    if problem:
        result.update(error=problem, finished_at=now())
        write_result(output, result)
        return output, result
    started = time.monotonic()
    try:
        host.load_credentials(host_data.expanduser())
    except SystemExit as error:
        result.update(error=str(error), finished_at=now())
        write_result(output, result)
        return output, result
    # The result names the loaded keys. It never holds their values.
    result["credentials"] = sorted(key for key in host.MODEL_KEYS if os.environ.get(key))
    secrets = [os.environ[key] for key in result["credentials"]]
    logs = output.with_suffix(".logs")
    if logs.is_dir() and not logs.is_symlink():
        # A new run for the same commits replaces the earlier logs.
        shutil.rmtree(logs)
    private_directory(logs)
    root = None
    with runtime_lock(runtime):
        try:
            with candidate_root(runtime, python, code) as root:
                result["checks"] = run_checks(root, code, python, logs, secrets,
                                                     programs() if texts is None else texts)
        except Exception as error:
            result.update(error=redact(f"{type(error).__name__}: {error}", secrets)[:TAIL_CHARACTERS])
    result["scratch_removed"] = root is None or not os.path.lexists(root)
    after = identity(code, runtime / "source")
    changed = {key: after[key] for key in before if before[key] != after[key]}
    if changed:
        result.update(error="The Coconut or Hermes source changed during the checks.", changed=changed)
    checks = result.get("checks")
    result["passed"] = bool(checks and not result.get("error") and result["scratch_removed"]
                            and all(check["passed"] for check in checks.values()))
    result.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 2))
    write_result(output, result)
    return output, result


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True,
                        help="The candidate runtime folder. It holds the Hermes checkout in source.")
    parser.add_argument("--host-data", type=Path, required=True,
                        help="The Coconut host data folder whose credentials.json holds the model keys.")
    parser.add_argument("--python", type=Path,
                        help="The candidate Python. The default is venv/bin/python or source/.venv/bin/python.")
    parser.add_argument("--output", type=Path, help="The result file. The default is inside runtime/checks/results.")
    args = parser.parse_args()
    path, result = check_candidate(args.runtime, args.host_data, python=args.python, output=args.output)
    print(json.dumps(dict(passed=result["passed"], result=str(path), source_sha=result["source_sha"],
                          hermes_sha=result["hermes_sha"], error=result.get("error")), indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
