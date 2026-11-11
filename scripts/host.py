#!/usr/bin/env python3
"""Manage the local Coconut service without a hosted dependency."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import contextlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / ".runtime/host"
LABEL = "local.coconut.agent"
# Both listeners use plain HTTP on 127.0.0.1. The main listener serves the tools on this Mac.
PORT = 8960
# The tunnel sidecar (scripts/tunnel.py) forwards phone requests to this listener.
TUNNEL_PORT = 8961
MODEL_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def normalize_host(value: str) -> str:
    if "%" in value:
        raise SystemExit("Use a DNS hostname or an IP address without a zone identifier.")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.rstrip(".").lower().split(".")
        if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            raise SystemExit("Use a DNS hostname or an IP address without a zone identifier.")
        return ".".join(labels)


@contextmanager
def stopped_service(data: Path, action: str):
    lock = data / "service.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"Another process still locks {lock}. Stop Coconut before {action}. "
                             "Unload its LaunchAgent if installed.") from None
        yield


def backend_module(name: str):
    """Load one module of backend/coconut alone. It must use the standard library only, so any Python can run it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("coconut_" + name, ROOT / f"backend/coconut/{name}.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hermes_runtime():
    return backend_module("hermes_runtime")


def service_path(value: str) -> str:
    """Return the PATH of the service. host.py serve sets it before the service searches for Hermes."""
    return backend_module("terminal_setup").command_path(value)


def service_environment() -> dict:
    """Return the environment in which the login service searches for Hermes. An update checks Hermes with it.

    The updater runs in another environment, for example over SSH. Its PATH or settings can find another Hermes.
    """
    environment = dict(os.environ)
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    try:
        payload = plistlib.loads(path.read_bytes()) if path.is_file() and not path.is_symlink() else {}
    except (OSError, ValueError, plistlib.InvalidFileException):
        payload = {}
    service = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
    if isinstance(service, dict):
        for name in ("PATH", "COCONUT_HERMES_PYTHON", "COCONUT_HERMES_ROOT", "HERMES_HOME"):
            environment.pop(name, None)
        environment.update({name: value for name, value in service.items() if isinstance(value, str)})
        # launchd gives a job without PATH this default.
        environment.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    environment["PATH"] = service_path(environment.get("PATH", ""))
    return environment


def service_args(args: argparse.Namespace) -> list[str]:
    moved = args.data_dir / "moved-to.json"
    if moved.exists() or moved.is_symlink():
        raise SystemExit("Execution moved to the Mac mini. This Mac's saved copy remains stopped. Use remote.py status to check the mini.")
    if not 1 <= args.port <= 65535:
        raise SystemExit("Use a port from 1 through 65535.")
    return [sys.executable, "-m", "coconut", "--data-dir", str(args.data_dir), "--port", str(args.port),
            "--provider", args.provider, "--tunnel-port", str(TUNNEL_PORT)]


def pair(args: argparse.Namespace) -> None:
    """Print a one-time code with which a tool on this Mac pairs on the main listener. A phone pairs with pairing.py."""
    command = [sys.executable, "-m", "coconut", "--data-dir", str(args.data_dir), "--pair"]
    subprocess.run(command, cwd=ROOT / "backend", check=True)


def load_credentials(data: Path) -> None:
    path = data / "credentials.json"
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise SystemExit("The model credential file must be private to this Mac account.")
        for name, value in json.loads(path.read_text()).items():
            if name in MODEL_KEYS and isinstance(value, str) and value:
                os.environ.setdefault(name, value)


def start(args: argparse.Namespace) -> None:
    private_directory(args.data_dir)
    lock_path = args.data_dir / "service.lock"
    with lock_path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Coconut is already running.")
            return
    service_args(args)
    command = [sys.executable, str(Path(__file__).resolve()), "serve", "--data-dir", str(args.data_dir),
               "--port", str(args.port), "--provider", args.provider]
    environment = dict(os.environ, PATH=service_path(os.environ.get("PATH", "")))
    if args.provider == "hermes":
        # Check Hermes before the service starts, so the wait below covers only the start. The service takes this result.
        # The search uses the PATH that host.py serve gives the service.
        status = hermes_runtime().status(args.data_dir, environ=environment)
        environment["COCONUT_HERMES_STATUS"] = json.dumps(status)
        if status["status"] != "ok":
            print(f"Coconut runs no bots until Hermes works: {status['reason']}")
    with (args.data_dir / "service.log").open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, env=environment,
                                   stdout=log, stderr=log, start_new_session=True)
    record = args.data_dir / "process.json"
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit(f"The service stopped. Inspect {args.data_dir / 'service.log'}.")
        if record.exists() and json.loads(record.read_text()).get('pid') == process.pid:
            print("Coconut is running in the background. Closing this terminal leaves tasks running.")
            return
        time.sleep(.1)
    raise SystemExit("The service did not become ready. Inspect its service.log before retrying.")


def process_state(pid: int) -> tuple[str, str] | None:
    """Return the state letters and the command of a process, or None when no such process exists."""
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=,command="], capture_output=True, text=True)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise SystemExit("Coconut could not inspect its process. No process was stopped.")
    state, _, command = result.stdout.strip().partition(" ")
    return state, command.strip()


# The state letters of ps: E, a process that exits (macOS). Z, a zombie: it exited and waits for its parent. X, dead
# (Linux). Between its exit and the moment launchd collects it, ps shows the process as "(Python)" or "<defunct>", not
# as its launch command. A service of the login item, a child of launchd, passes through these states at each stop.
EXITED = "ZX"
EXITING = "E" + EXITED


def exiting(state: str) -> bool:
    return any(letter in state for letter in EXITING)


def start_time(pid: int) -> str | None:
    """The start time of a process, as process_table gives it, or None. With the process ID, it identifies one process."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL="C"))
    if result.returncode:
        return None
    # The same form as the start time of process_table: five words with single spaces.
    return " ".join(result.stdout.split()) or None


def exits_without_arguments(pid: int, command: str, verified: tuple[int, str] | None) -> bool:
    """True when Linux shows a process of this service that exits.

    Linux has no state letter for it: a process that exits releases its memory, and with it its argument list, before it
    becomes a zombie. ps then shows its name in brackets, such as "[python3]", with a live state such as R or D. Any
    process can show a bracketed name, so this counts only for the process that this stop already checked: the same
    process ID and the same start time.
    """
    if not (command.startswith("[") and command.endswith("]") and verified is not None and verified[0] == pid
            and verified[1] is not None):
        return False
    started = start_time(pid)
    # A process that is gone when ps reads its start time has ended: it was the exiting service. A process with another
    # start time is another process.
    return started is None or started == verified[1]


def recorded_pid() -> int | None:
    """The process ID in process.json, without any check. Use it only after owned_process accepted the record."""
    try:
        return int(json.loads((DEFAULT_DATA / "process.json").read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ended(pid: int) -> bool:
    """True when the process is gone or is a zombie. A zombie has closed its files, so service.lock is free."""
    state = process_state(pid)
    return state is None or any(letter in state[0] for letter in EXITED)


def owned_process(verified: tuple[int, str] | None = None) -> tuple[int, dict] | None:
    """The process ID and options of this installation's service, or None when none runs or it exits.

    `verified` is the process ID and start time of the service that the caller already checked and stops.
    """
    if DEFAULT_DATA.is_symlink() or (ROOT / ".runtime").is_symlink():
        raise SystemExit("The runtime must not be a symbolic link. Coconut did not stop any process.")
    path = DEFAULT_DATA / "process.json"
    if path.is_symlink():
        raise SystemExit("The process record must not be a symbolic link.")
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
        pid = int(record["pid"])
        if record.get("project") != str(ROOT) or record.get("data_dir") != str(DEFAULT_DATA) or pid <= 1 or pid == os.getpid():
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("The process record does not identify this Coconut installation. No process was stopped.") from None
    state = process_state(pid)
    if state is None or exiting(state[0]) or exits_without_arguments(pid, state[1], verified):
        # An exiting process serves nothing. stop_owned_process waits until it is gone.
        return None
    pattern = (
        r"(?P<python>.+?) -m coconut --data-dir " + re.escape(str(DEFAULT_DATA))
        + r" --port (?P<port>\d+) --provider (?P<provider>hermes|fixture) --tunnel-port " + str(TUNNEL_PORT)
    )
    match = re.fullmatch(pattern, state[1])
    if not match or not re.fullmatch(r"python[0-9.]*", Path(match["python"]).name, re.IGNORECASE):
        raise SystemExit("The recorded process does not match Coconut's launch command. No process was stopped.")
    return pid, {"port": int(match["port"]), "provider": match["provider"]}


def owned_agent(data_dir: Path | None = None) -> tuple[Path, bool]:
    data_dir = data_dir if data_dir is not None else DEFAULT_DATA
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if path.is_symlink():
        raise SystemExit("The LaunchAgent file must not be a symbolic link.")
    if path.exists():
        try:
            payload = plistlib.loads(path.read_bytes())
            command = payload["ProgramArguments"]
            valid = (payload.get("Label") == LABEL and payload.get("WorkingDirectory") == str(ROOT / "backend")
                     and isinstance(command, list) and len(command) >= 5
                     and command[1:3] == [str(Path(__file__).resolve()), "serve"]
                     and command.count("--data-dir") == 1
                     and command[command.index("--data-dir") + 1] == str(data_dir))
            if not valid:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, IndexError, plistlib.InvalidFileException):
            raise SystemExit("The LaunchAgent belongs to another installation or has an invalid configuration. Coconut left it unchanged.") from None
    target = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if result.returncode:
        if f'Could not find service "{LABEL}"' in result.stderr:
            return path, False
        raise SystemExit("Coconut could not read its LaunchAgent state. No service was changed.")
    loaded_path = re.search(r"^\s*path = (.+)$", result.stdout, re.MULTILINE)
    working_directory = re.search(r"^\s*working directory = (.+)$", result.stdout, re.MULTILINE)
    if (not path.exists() or not loaded_path or not working_directory
            or loaded_path[1].strip('"') != str(path)
            or working_directory[1].strip('"') != str(ROOT / "backend")):
        raise SystemExit("The loaded LaunchAgent does not identify this Coconut installation. No service was changed.")
    return path, True


# Coconut.app runs the service as this login item (mac/LaunchAgents). Its tool names the label in COCONUT_LOGIN_ITEM.
LOGIN_ITEM_LABEL = "com.gumroad.coconut.service"
# Coconut.app keeps the mode of its install in its preferences, next to InstallDirectory (mac/Sources/CoconutKit,
# InstallMode). "team": the Mac runs Coconut for a team only, and Coconut adds nothing to its Hermes.
APP_DOMAIN = "com.gumroad.coconut.mac"
TEAM_MODE = "team"


def team_folder_refusal(data: Path | None = None) -> str | None:
    """The refusal of a Hermes step when the data folder belongs to a team-only service, else None.

    service_base.team_folder decides, so a lost preference of Coconut.app never lets a step reach Hermes."""
    base = backend_module("service_base")
    return base.HERMES_REFUSAL if base.team_folder(data if data is not None else DEFAULT_DATA) else None


def connected_chats(data: Path | None = None) -> list[str]:
    """The names of the bots of a team-only Mac whose team chat is connected to their Hermes home. Only
    `coconut team disconnect-chat` takes that change away, so a removal of Coconut waits for it. The store opens
    read-only."""
    import sqlite3
    path = (data if data is not None else DEFAULT_DATA) / "coconut.sqlite3"
    if not path.is_file():
        return []
    with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as db:
        try:
            rows = db.execute("SELECT json_extract(data, '$.name') FROM bots WHERE json_extract(data, '$.chat')=1 "
                              "ORDER BY rowid").fetchall()
        except sqlite3.OperationalError:
            return []
    return [row[0] for row in rows]


def install_mode() -> str:
    """The mode of the install of Coconut.app on this Mac: "team", or "full" without the preference."""
    try:
        result = subprocess.run(["defaults", "read", APP_DOMAIN, "InstallMode"], capture_output=True, text=True)
    except OSError:
        # No `defaults`, for example on the Linux CI runner: no Coconut.app either.
        return "full"
    return TEAM_MODE if result.returncode == 0 and result.stdout.strip() == TEAM_MODE else "full"


def launchd_pid(label: str) -> int | None:
    """Return the process that launchd runs for a job of this account, or None."""
    result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True)
    match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.MULTILINE)
    return int(match[1]) if result.returncode == 0 and match else None


def managed_current_process(data_dir: Path | None = None) -> bool:
    """Return True when launchd runs this process and starts it again after it exits, so a bot can ask for a restart."""
    if os.environ.get('COCONUT_LOGIN_ITEM') == LOGIN_ITEM_LABEL:
        # The login item keeps KeepAlive on (mac/LaunchAgents). The variable alone proves nothing, so launchd decides.
        return launchd_pid(LOGIN_ITEM_LABEL) == os.getpid()
    try:
        path, loaded = owned_agent(data_dir)
    except (SystemExit, OSError):
        # Optional restart support must not prevent a manual service from starting.
        return False
    if not loaded or plistlib.loads(path.read_bytes()).get('KeepAlive') is not True:
        return False
    return launchd_pid(LABEL) == os.getpid()


# The service stops its Hermes gateway and its workers before it exits (Service.close). Each one can take many seconds, so
# a stop waits this long for a clean exit. Then it sends SIGKILL. An update drains the turns first, so little work is lost.
STOP_GRACE_SECONDS = 120
# After SIGKILL, the kernel ends a process at once. The wait covers a slow ps.
KILL_WAIT_SECONDS = 10
# The processes that the service started and that are still alive after it ended get this long after SIGTERM, then SIGKILL.
CHILD_STOP_SECONDS = 10


def process_table() -> dict[int, tuple[int, int, str, str, str]]:
    """Return every process: {process ID: (parent ID, process group ID, state letters, start time, command)}.

    The start time and the command together identify a process. A reused process ID has another start time. An empty
    table means that ps failed, so no process is known and none gets a signal.
    """
    try:
        result = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="],
                                capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
    except OSError:
        return {}
    if result.returncode:
        return {}
    table = {}
    for line in result.stdout.splitlines():
        # lstart has five words, for example "Thu Oct 29 16:08:06 2026".
        fields = line.split(None, 9)
        if len(fields) == 10 and all(field.isdigit() for field in fields[:3]):
            table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3], " ".join(fields[4:9]), fields[9])
    return table


def identity(table: dict, pid: int) -> tuple[str, str]:
    return table[pid][3], table[pid][4]


def alive_processes(known: dict[int, tuple[str, str]], table: dict) -> dict[int, tuple[str, str]]:
    """The known processes that still run as the same process: same start time and command, not a zombie."""
    return {pid: value for pid, value in known.items()
            if pid in table and identity(table, pid) == value and not any(letter in table[pid][2] for letter in EXITED)}


def record_descendants(known: dict[int, tuple[str, str]], table: dict) -> None:
    """Add to `known` the processes that provably belong to the service, with their start time and command.

    A process belongs when its parent is a known process that still runs as itself, or when it is in the process group
    that such a process leads. `known` starts with the service. The service starts its Hermes gateway, its workers, and the
    Kanban dispatcher in sessions of their own, and a worker starts the background commands of its turns in further
    sessions, so a signal to the service reaches none of them. A process ID that now runs another process proves nothing,
    so its children and its group are never added. Only processes of the same session can join a group, so a member of a
    group whose leader still runs as itself is a process of that leader. A gateway that a person started has no such link.
    """
    proven = set(alive_processes(known, table))
    while True:
        added = {pid for pid, (parent, group, *_) in table.items()
                 if pid not in proven and pid not in known and (parent in proven or group in proven)}
        if not added:
            return
        for pid in added:
            known[pid] = identity(table, pid)
        proven |= added


def log_unproven(known: dict[int, tuple[str, str]], table: dict) -> None:
    """Name the processes in a group of an ended process of the service. They can be its background commands, but no
    living process proves it, so they get no signal. deploy.py keeps this output in its log."""
    ended_leaders = set(known) - set(alive_processes(known, table))
    for pid, (parent, group, state, _, command) in sorted(table.items()):
        if group in ended_leaders and pid not in known and not any(letter in state for letter in EXITED):
            print(f"Coconut did not stop process {pid} ({command[:120]}). It is in the process group of process {group} "
                  "of the old service, which ended, so nothing proves that it belongs to Coconut.", file=sys.stderr)


def stop_descendants(service: int, known: dict[int, tuple[str, str]]) -> None:
    """Stop the processes of an ended service that are still alive: SIGTERM, then SIGKILL. Check each one before each
    signal, and add the processes that they started meanwhile.

    A Hermes gateway of the old source that keeps running blocks the gateway of the new source. A worker or a background
    command of the old source can change the files of the new one. So the update waits until each one has ended.
    """
    def remaining(table):
        record_descendants(known, table)
        return {pid: value for pid, value in alive_processes(known, table).items() if pid != service}
    alive = {}
    table = {}
    for number, seconds in ((signal.SIGTERM, CHILD_STOP_SECONDS), (signal.SIGKILL, KILL_WAIT_SECONDS)):
        table = process_table()
        alive = remaining(table)
        if not alive:
            break
        for pid in alive:
            try:
                os.kill(pid, number)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + seconds
        while alive and time.monotonic() < deadline:
            time.sleep(.1)
            table = process_table()
            alive = remaining(table)
    if alive:
        pid, (_, command) = next(iter(alive.items()))
        raise SystemExit(f"The service ended, but its process {pid} ({command[:120]}) did not end after SIGKILL. "
                         "Coconut kept its data unchanged.")
    log_unproven(known, table)


def stop_owned_process() -> None:
    """Stop the service of this installation and every process that it started. Return only when all of them have ended.

    The service gets SIGTERM and STOP_GRACE_SECONDS to stop its processes itself. Then it gets SIGKILL, and Coconut stops
    the processes that remain. Each failure names the process and the step.
    """
    current = owned_process()
    if current is None:
        # A service that exits already can still hold service.lock. Wait until it is gone.
        pid = recorded_pid()
        if pid is None or pid <= 1 or pid == os.getpid():
            return
    else:
        pid = current[0]
    # {process ID: (start time, command)} of each process that the service started.
    table = process_table()
    known: dict[int, tuple[str, str]] = {pid: identity(table, pid)} if pid in table else {}
    record_descendants(known, table)
    if current is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    killed = False
    listed = time.monotonic()
    # The service that this stop checked, with its start time from the table. Only it may show the bracketed name of a
    # Linux process that exits.
    verified = (pid, known[pid][0]) if current is not None and pid in known else None
    while True:
        current = owned_process(verified)
        if current is not None and current[0] != pid:
            raise SystemExit(f"Another Coconut process ({current[0]}) started while process {pid} stopped. "
                             "Coconut kept its data unchanged.")
        if current is None and ended(pid):
            break
        # The processes start more processes while they stop. ps lists every process, so the list is read once a second.
        if time.monotonic() - listed >= 1:
            record_descendants(known, process_table())
            listed = time.monotonic()
        if time.monotonic() >= deadline:
            if killed:
                raise SystemExit(f"Coconut process {pid} did not end {KILL_WAIT_SECONDS} seconds after SIGKILL. "
                                 "Its source and data remain unchanged.")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed = True
            deadline = time.monotonic() + KILL_WAIT_SECONDS
        time.sleep(.1)
    stop_descendants(pid, known)


def wait_for_agent_unloaded(timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while owned_agent()[1]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit("Coconut's LaunchAgent did not unload within ten seconds. Its source and data remain unchanged.")
        time.sleep(min(.1, remaining))


# The updater waits this long for the running turns before it stops the service. A turn that is still running then loses its
# tool results, and Coconut fails its task at the next start.
DRAIN_SECONDS = 600


def drain_for_update(timeout: float = DRAIN_SECONDS) -> bool:
    """Ask a running service to finish its turns, and wait until it says none is left. Return whether it did.

    The request ends by itself, so a failed update cannot leave the service paused. A service that does not say
    "drain" in its process record is stopped at once, as before.
    """
    try:
        record = json.loads((DEFAULT_DATA / "process.json").read_text())
    except (OSError, ValueError):
        return False
    if record.get("drain") is not True or owned_process() is None:
        return False
    request, drained = DEFAULT_DATA / "drain-request", DEFAULT_DATA / "drained"
    if request.is_symlink() or drained.is_symlink():
        return False
    drained.unlink(missing_ok=True)
    # The service reads this file while it runs. A truncated file must never be visible, so the request replaces the file at once.
    pending = request.with_name(request.name + ".tmp")
    pending.write_text(str(time.time() + timeout + 60))
    os.replace(pending, request)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if drained.exists():
            return True
        time.sleep(1)
    return False


def login_item_loaded() -> bool:
    try:
        return subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LOGIN_ITEM_LABEL}'],
                              capture_output=True, text=True).returncode == 0
    except OSError:
        # No launchctl, for example on the Linux CI runner: no login item either.
        return False


def app_runs_service() -> bool:
    """True when the login item of Coconut.app runs the service and no LaunchAgent of host.py does."""
    try:
        _, loaded = owned_agent()
    except (SystemExit, OSError):
        return False
    return not loaded and login_item_loaded()


# While this file exists, the `coconut service` tool of Coconut.app waits before it starts host.py serve. It holds the
# time at which the hold ends, so a failed update cannot keep the service stopped.
UPDATE_HOLD = "update-hold"
UPDATE_HOLD_SECONDS = 1800


def hold_login_item() -> None:
    hold = DEFAULT_DATA / UPDATE_HOLD
    if hold.is_symlink():
        raise SystemExit("The update hold must not be a symbolic link.")
    pending = hold.with_name(hold.name + ".tmp")
    pending.write_text(str(time.time() + UPDATE_HOLD_SECONDS))
    os.replace(pending, hold)


def release_login_item() -> None:
    (DEFAULT_DATA / UPDATE_HOLD).unlink(missing_ok=True)


def inspect_for_update() -> dict:
    """The mode that stop_for_update will return. "app": the login item of Coconut.app runs the service."""
    if app_runs_service():
        return {"mode": "app", "project": str(ROOT), "data_dir": str(DEFAULT_DATA)}
    _, loaded = owned_agent()
    process = owned_process()
    state = {"mode": "managed" if loaded else "manual" if process else "stopped",
             "project": str(ROOT), "data_dir": str(DEFAULT_DATA)}
    if state["mode"] == "manual":
        state["options"] = process[1]
    return state


def stop_for_app_update() -> dict:
    """Stop a service of the login item. launchd starts the tool again at once, and the tool waits for the hold."""
    private_directory(DEFAULT_DATA)
    hold_login_item()
    try:
        stop_owned_process()
        with stopped_service(DEFAULT_DATA, "updating its source"):
            pass
    except (SystemExit, OSError):
        release_login_item()
        raise
    return {"mode": "app", "project": str(ROOT), "data_dir": str(DEFAULT_DATA)}


TUNNEL_LABEL = "com.gumroad.coconut.tunnel"


def migrate_to_app() -> Path:
    """Stop the service of the LaunchAgents of host.py and tunnel.py, and move their files aside. Return that folder.

    Coconut.app then registers its own login items. The data stays in place, so bots and paired phones keep working.
    The running turns finish first, as in an update.
    """
    agents = Path.home() / "Library/LaunchAgents"
    backup = DEFAULT_DATA.parent / "legacy-agents"
    drain_for_update()
    stop_for_update()
    # The sidecar has no data to protect. It stops at once.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{TUNNEL_LABEL}"], capture_output=True, text=True)
    private_directory(backup)
    for label in (LABEL, TUNNEL_LABEL):
        source = agents / f"{label}.plist"
        if source.is_file() and not source.is_symlink():
            os.replace(source, backup / source.name)
    return backup


def stop_for_update() -> dict:
    path, loaded = owned_agent()
    if not loaded and login_item_loaded():
        return stop_for_app_update()
    process = owned_process()
    state = {"mode": "managed" if loaded else "manual" if process else "stopped",
             "project": str(ROOT), "data_dir": str(DEFAULT_DATA)}
    if state["mode"] == "manual":
        state["options"] = process[1]
    bootout_completed = False
    try:
        if loaded:
            result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
            if result.returncode:
                raise SystemExit("launchctl could not unload Coconut. Its source and data remain unchanged.")
            bootout_completed = True
            wait_for_agent_unloaded()
        stop_owned_process()
        if DEFAULT_DATA.exists():
            with stopped_service(DEFAULT_DATA, "updating its source"):
                pass
    except (SystemExit, OSError) as error:
        if bootout_completed:
            command = shlex.join(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
            raise SystemExit(f"{error} launchctl completed bootout, but Coconut did not finish the stop. "
                             f"Restore its LaunchAgent with: {command}") from None
        raise
    return state


def write_agent(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), 0o600)
            plistlib.dump(payload, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def restore_after_update(state: dict) -> None:
    if (not isinstance(state, dict) or state.get("project") != str(ROOT) or state.get("data_dir") != str(DEFAULT_DATA)
            or state.get("mode") not in {"manual", "managed", "stopped", "app"}):
        raise SystemExit("The saved service state does not belong to this Coconut installation.")
    if state["mode"] == "stopped":
        return
    if state["mode"] == "app":
        # The waiting tool starts the service from the current source. The deploy health check confirms it.
        release_login_item()
        return
    path, loaded = owned_agent()
    current = owned_process()
    if state["mode"] == "managed":
        if loaded:
            return
        if not path.exists() or current:
            raise SystemExit("Coconut cannot restore its LaunchAgent while its file is missing or a manual process is running.")
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("launchctl could not restore Coconut. Its LaunchAgent file and data remain available.")
        if not owned_agent()[1]:
            raise SystemExit("launchctl did not load Coconut. Its LaunchAgent file and data remain available.")
        return
    options = state.get("options")
    if (not isinstance(options, dict) or set(options) != {"port", "provider"}
            or type(options["port"]) is not int or options["provider"] not in {"hermes", "fixture"}):
        raise SystemExit("The saved manual launch options are invalid.")
    if loaded or (current and current[1] != options):
        raise SystemExit("A different Coconut service is running. Coconut did not start another process.")
    if current:
        return
    start(argparse.Namespace(data_dir=DEFAULT_DATA, **options))


def restart(args: argparse.Namespace) -> None:
    state = stop_for_update()
    if state["mode"] == "stopped":
        start(args)
    else:
        restore_after_update(state)


def install(args: argparse.Namespace) -> None:
    service_args(args)
    private_directory(args.data_dir)
    command = [sys.executable, str(Path(__file__).resolve()), "serve", "--data-dir", str(args.data_dir),
               "--port", str(args.port), "--provider", args.provider]
    private_directory(args.data_dir / "logs")
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{LABEL}.plist"
    if plist.exists():
        raise SystemExit(f"A service already exists at {plist}. Use launchctl to manage it.")
    # host.py serve finds the person's Hermes at each start: `hermes` on this PATH, then ~/.hermes/hermes-agent.
    # Explicit settings select another Hermes. Credentials stay in the host environment file.
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"}
    for name in ("COCONUT_HERMES_PYTHON", "COCONUT_HERMES_ROOT", "HERMES_HOME"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    payload = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(ROOT / "backend"),
        "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True,
        # The phone waits for this service through the tunnel, without XPC priority propagation.
        "ThrottleInterval": 5, "ProcessType": "Interactive",
        "StandardOutPath": str(args.data_dir / "logs/service.log"),
        "StandardErrorPath": str(args.data_dir / "logs/error.log"),
    }
    write_agent(plist, payload)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=False)
    if result.returncode:
        raise SystemExit(f"The service file exists at {plist}, but launchctl did not start it.")
    print(f"Installed {LABEL}. The service starts when this Mac account logs in.")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "start", "stop", "restart", "pair", "save-model-access",
                                            "install-service", "status", "hermes-status", "migrate-to-app"])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--port", type=int, default=PORT, help="The port of the main listener on 127.0.0.1.")
    parser.add_argument("--provider", choices=["hermes", "fixture"], default="hermes")
    args = parser.parse_args()
    args.data_dir = args.data_dir.expanduser().resolve()
    load_credentials(args.data_dir)
    if args.command == "pair":
        pair(args)
    elif args.command == "install-service":
        install(args)
    elif args.command == "save-model-access":
        private_directory(args.data_dir)
        configured = {key: os.environ[key] for key in MODEL_KEYS if os.environ.get(key)}
        if not configured:
            raise SystemExit("Configure a model API key in this shell before saving model access.")
        path = args.data_dir / "credentials.json"
        path.write_text(json.dumps(configured))
        path.chmod(0o600)
        print("Saved configured model access in the private local runtime.")
    elif args.command == "start":
        start(args)
    elif args.command == "migrate-to-app":
        if args.data_dir != DEFAULT_DATA:
            raise SystemExit("Use the default data directory to move Coconut to Coconut.app.")
        print(f"Moved the earlier LaunchAgents to {migrate_to_app()}.")
    elif args.command == "hermes-status":
        # Find and check the person's Hermes with this code. The saved status stays the one of the running service.
        status = hermes_runtime().status(args.data_dir, environ=service_environment(), save=False)
        print("COCONUT_HERMES_STATUS=" + json.dumps(status))
    elif args.command in {"stop", "restart", "status"}:
        if args.data_dir != DEFAULT_DATA:
            raise SystemExit("Use the default data directory for status, stop, and restart.")
        if args.command == "stop":
            if app_runs_service():
                # A stop under the app holds the login item only until the hold ends.
                raise SystemExit("Coconut.app runs this service. Turn off Start at Login in its menu to stop it.")
            stop_for_update()
            print("Coconut is stopped. Its data remains in .runtime.")
        elif args.command == "restart":
            restart(args)
        else:
            process = owned_process()
            if process:
                print(f"Coconut is running with process ID {process[0]} on 127.0.0.1:{process[1]['port']}.")
            else:
                print("Coconut is stopped.")
    else:
        command = service_args(args)
        sys.path.insert(0, str(ROOT / "backend"))
        os.environ['PATH'] = service_path(os.environ.get('PATH', ''))
        managed = managed_current_process(args.data_dir)
        os.environ['COCONUT_MANAGED_SERVICE'] = '1' if managed else '0'
        private_directory(args.data_dir)
        os.chdir(ROOT / "backend")
        os.execv(command[0], command)


if __name__ == "__main__":
    main()
