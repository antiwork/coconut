#!/usr/bin/env python3
"""Manage the local Coconut service without a hosted dependency."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / ".runtime/host"
LABEL = "local.coconut.agent"
# The tunnel sidecar (scripts/tunnel.py) forwards phone requests to this plain HTTP listener on loopback.
TUNNEL_PORT = 8961
MODEL_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
ADDRESS_COMMANDS = {"setup", "serve", "start", "install-service", "status"}


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


def address_parts(value: str, port: int | None = None) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise SystemExit("Use one HTTPS address without spaces or control characters.")
    try:
        url = urlsplit(value)
        address_port = url.port or 443
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or "?" in value or "#" in value or url.port == 0
                or not re.fullmatch(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", url.netloc)):
            raise ValueError()
        hostname = normalize_host(url.hostname)
    except ValueError:
        raise SystemExit("Use an HTTPS address with a valid port and no credentials, path, query, or fragment.") from None
    if port is not None and port != address_port:
        raise SystemExit(f"The address port must match --port {port}. Include :{port} in --address.")
    host_label = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{host_label}:{address_port}", hostname, address_port


def configure_address(args: argparse.Namespace) -> None:
    path = args.data_dir / "connection.json"
    saved = None
    if path.is_symlink():
        raise SystemExit("The connection file must not be a symbolic link.")
    if path.exists():
        try:
            saved = address_parts(json.loads(path.read_text())["address"])[0]
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("The connection file is invalid. Restore connection.json before continuing.") from None
    selected = args.address or saved
    args.save_address = args.address is not None
    if args.port is None:
        args.port = address_parts(selected)[2] if selected else 8960
    if not 1 <= args.port <= 65535:
        raise SystemExit("Use a port from 1 through 65535.")
    if selected is None:
        hostname = normalize_host(socket.gethostname())
        host_label = f"[{hostname}]" if ":" in hostname else hostname
        selected = f"https://{host_label}:{args.port}"
    args.address, _, _ = address_parts(selected, args.port)


def save_address(args: argparse.Namespace) -> None:
    if not getattr(args, "save_address", False):
        return
    path = args.data_dir / "connection.json"
    if path.exists() and address_parts(json.loads(path.read_text())["address"])[0] == args.address:
        return
    with stopped_service(args.data_dir, "changing its address"):
        with tempfile.NamedTemporaryFile(mode="w", dir=args.data_dir, prefix="connection-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                os.chmod(temporary, 0o600)
                json.dump({"address": args.address}, handle)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


@contextmanager
def stopped_service(data: Path, action: str = "replacing its certificate"):
    lock = data / "service.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"Stop Coconut before {action}. Unload its LaunchAgent if installed.") from None
        yield


def trusted_certificate_context(*, cafile=None, cadata=None) -> ssl.SSLContext:
    """Trust only the supplied certificate while retaining normal TLS validation."""
    if (cafile is None) == (cadata is None) or (cafile is not None and not str(cafile)) or (cadata is not None and not cadata.strip()):
        raise ValueError("Provide one nonempty certificate file or certificate value.")
    context = ssl.create_default_context(cafile=str(cafile) if cafile is not None else None, cadata=cadata)
    # Python 3.9 omits the enum; its LibreSSL supports X509_V_FLAG_PARTIAL_CHAIN.
    context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0x80000)
    return context


def verify_certificate(cert: Path, key: Path, hostname: str) -> None:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = trusted_certificate_context(cafile=cert)
    client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for name, connection in (("client", client), ("server", server)):
            try:
                connection.do_handshake()
                completed.add(name)
            except ssl.SSLWantReadError:
                pass
        if client_out.pending:
            server_in.write(client_out.read())
        if server_out.pending:
            client_in.write(server_out.read())
        if len(completed) == 2:
            return
    raise ssl.SSLError("The certificate handshake did not finish.")


def certificate(data: Path, advertised_host: str | None = None, *, replace: bool = False) -> tuple[Path, Path]:
    private_directory(data)
    cert, key = data / "server.crt", data / "server.key"
    hostname = normalize_host(advertised_host or socket.gethostname())
    if cert.is_symlink() or key.is_symlink():
        raise SystemExit("The certificate files must not be symbolic links.")
    if cert.exists() != key.exists():
        raise SystemExit("The certificate pair is incomplete. Restore both files before continuing.")
    if not cert.exists() or replace:
        with stopped_service(data):
            create_certificate(data, cert, key, hostname)
    try:
        verify_certificate(cert, key, hostname)
    except (ssl.SSLError, OSError):
        raise SystemExit(
            f"The certificate does not validate for {hostname}. Coconut kept it unchanged. "
            "While Coconut is stopped, run setup with --address and --replace-certificate. "
            "Then pair every phone again."
        ) from None
    key.chmod(0o600)
    cert.chmod(0o600)
    return cert, key


def create_certificate(data: Path, cert: Path, key: Path, advertised_host: str) -> None:
    with tempfile.TemporaryDirectory(prefix="certificate-", dir=data) as directory:
        openssl = shutil.which("openssl")
        if not openssl:
            raise SystemExit("Install OpenSSL before setting up the host.")
        hostname = normalize_host(socket.gethostname())
        names = {"localhost", hostname}
        if "." not in hostname:
            names.add(hostname + ".local")
        addresses = {"127.0.0.1", "::1"}
        try:
            for result in socket.getaddrinfo(hostname, None):
                address = result[4][0].split("%")[0]
                addresses.add(str(ipaddress.ip_address(address)))
        except (OSError, ValueError):
            pass
        try:
            addresses.add(str(ipaddress.ip_address(advertised_host)))
        except ValueError:
            names.add(advertised_host)
        alternatives = ["DNS:" + name for name in sorted(names)]
        alternatives.extend("IP:" + address for address in sorted(addresses))
        candidate_cert, candidate_key = Path(directory) / "server.crt", Path(directory) / "server.key"
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-pkeyopt", "ec_param_enc:named_curve",
             "-sha256", "-nodes", "-days", "365", "-subj", "/CN=Coconut Local Host",
             "-addext", "basicConstraints=critical,CA:FALSE",
             "-addext", "keyUsage=critical,digitalSignature",
             "-addext", "extendedKeyUsage=serverAuth",
             "-addext", "subjectAltName=" + ",".join(alternatives),
             "-keyout", str(candidate_key), "-out", str(candidate_cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        candidate_cert.chmod(0o600)
        candidate_key.chmod(0o600)
        verify_certificate(candidate_cert, candidate_key, advertised_host)
        backup = None
        if cert.exists():
            backup = Path(tempfile.mkdtemp(prefix="certificate-backup-", dir=data))
            shutil.copy2(cert, backup / "server.crt")
            shutil.copy2(key, backup / "server.key")
            (backup / "server.crt").chmod(0o600)
            (backup / "server.key").chmod(0o600)
        try:
            os.replace(candidate_key, key)
            os.replace(candidate_cert, cert)
        except OSError:
            if backup:
                shutil.copy2(backup / "server.key", key)
                shutil.copy2(backup / "server.crt", cert)
            else:
                key.unlink(missing_ok=True)
                cert.unlink(missing_ok=True)
            raise
        if backup:
            print(f"Replaced the certificate. Pair every phone again. Previous certificate: {backup}")


def connection_certificate(args: argparse.Namespace) -> tuple[Path, Path]:
    address = getattr(args, "address", None)
    hostname = address_parts(address, args.port)[1] if address else None
    result = certificate(args.data_dir, hostname, replace=getattr(args, "replace_certificate", False))
    save_address(args)
    return result


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


def fingerprint(cert: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return hashlib.sha256(der).hexdigest().upper()


def service_args(args: argparse.Namespace) -> list[str]:
    moved = args.data_dir / "moved-to.json"
    if moved.exists() or moved.is_symlink():
        raise SystemExit("Execution moved to the Mac mini. This Mac's saved copy remains stopped. Use remote.py status to check the mini.")
    result = [sys.executable, "-m", "coconut", "--data-dir", str(args.data_dir),
              "--host", args.host, "--port", str(args.port), "--provider", args.provider]
    if not args.insecure_loopback:
        cert, key = connection_certificate(args)
        result.extend(["--cert", str(cert), "--key", str(key)])
    elif args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("HTTP is available only on loopback. Use TLS for the local network.")
    return [*result, "--tunnel-port", str(TUNNEL_PORT)]


def show_connection(args: argparse.Namespace) -> None:
    cert, _ = connection_certificate(args)
    print(f"Address: {args.address}")
    print(f"Certificate SHA-256: {fingerprint(cert)}")
    print("Enter the address and fingerprint in the iPhone Settings.")


def pair(args: argparse.Namespace) -> None:
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
               "--host", args.host, "--port", str(args.port), "--provider", args.provider]
    if args.insecure_loopback:
        command.append("--insecure-loopback")
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


def owned_process() -> tuple[int, dict] | None:
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
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise SystemExit("Coconut could not inspect its process. No process was stopped.")
    pattern = (
        r"(?P<python>.+?) -m coconut --data-dir " + re.escape(str(DEFAULT_DATA))
        + r" --host (?P<host>\S+) --port (?P<port>\d+) --provider (?P<provider>hermes|fixture)"
        + r"(?P<tls> --cert " + re.escape(str(DEFAULT_DATA / "server.crt"))
        + r" --key " + re.escape(str(DEFAULT_DATA / "server.key")) + r")?"
        # A process from before the tunnel listener has no --tunnel-port.
        + r"(?: --tunnel-port " + str(TUNNEL_PORT) + r")?"
    )
    match = re.fullmatch(pattern, result.stdout.strip())
    if not match or not re.fullmatch(r"python[0-9.]*", Path(match["python"]).name, re.IGNORECASE):
        raise SystemExit("The recorded process does not match Coconut's launch command. No process was stopped.")
    return pid, {"host": match["host"], "port": int(match["port"]), "provider": match["provider"],
                 "insecure_loopback": match["tls"] is None}


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


def stop_owned_process() -> None:
    current = owned_process()
    if current is None:
        return
    pid = current[0]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(100):
        current = owned_process()
        if current is None:
            return
        if current[0] != pid:
            raise SystemExit("Another Coconut process started during the stop. Coconut kept its data unchanged.")
        time.sleep(.1)
    raise SystemExit("Coconut did not stop. Its source and data remain unchanged.")


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


def stop_for_update() -> dict:
    path, loaded = owned_agent()
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


# Pythons that stay in place when Hermes updates, in order of preference.
SERVICE_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")


def service_python() -> str | None:
    """Return a Python for the LaunchAgent that can run host.py serve and does not move with a Hermes update.

    install-service uses the Python that runs it. This prefers that Python too, unless it is in a virtual environment,
    for example the one of a Hermes install.
    """
    candidates = [sys.executable] if sys.executable and sys.prefix == sys.base_prefix else []
    for python in dict.fromkeys([*candidates, *SERVICE_PYTHONS]):
        if not os.access(python, os.X_OK) or Path(python).resolve().is_relative_to((ROOT / ".runtime").resolve()):
            continue
        try:
            check = subprocess.run([python, "-c", "import coconut.server"], cwd=ROOT / "backend", capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            return python
    return None


def repaired_agent(payload: dict) -> dict:
    """Return the LaunchAgent settings for a restore. Every key that this does not repair stays the same.

    The phone waits for the service over HTTPS, so the service runs as an interactive process. Earlier installations
    started the service with the Python of the private Hermes runtime in .runtime/hermes. The update removed that
    runtime, so launchd cannot start the service until it has another Python.
    """
    repaired = dict(payload, ProcessType="Interactive")
    command = payload["ProgramArguments"]
    if Path(command[0]).is_relative_to(ROOT / ".runtime/hermes"):
        python = service_python()
        if python is None:
            raise SystemExit("Coconut's LaunchAgent starts the Python of the removed private Hermes runtime, and no "
                             "other Python on this Mac can run Coconut. Install Python 3, then update again.")
        repaired["ProgramArguments"] = [python, *command[1:]]
    return repaired


def restore_after_update(state: dict) -> None:
    if (not isinstance(state, dict) or state.get("project") != str(ROOT) or state.get("data_dir") != str(DEFAULT_DATA)
            or state.get("mode") not in {"manual", "managed", "stopped"}):
        raise SystemExit("The saved service state does not belong to this Coconut installation.")
    if state["mode"] == "stopped":
        return
    path, loaded = owned_agent()
    current = owned_process()
    if state["mode"] == "managed":
        if loaded:
            return
        if not path.exists() or current:
            raise SystemExit("Coconut cannot restore its LaunchAgent while its file is missing or a manual process is running.")
        payload = plistlib.loads(path.read_bytes())
        repaired = repaired_agent(payload)
        if repaired != payload:
            write_agent(path, repaired)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("launchctl could not restore Coconut. Its LaunchAgent file and data remain available.")
        if not owned_agent()[1]:
            raise SystemExit("launchctl did not load Coconut. Its LaunchAgent file and data remain available.")
        return
    options = state.get("options")
    if not isinstance(options, dict) or set(options) != {"host", "port", "provider", "insecure_loopback"}:
        raise SystemExit("The saved manual launch options are invalid.")
    if loaded or (current and current[1] != options):
        raise SystemExit("A different Coconut service is running. Coconut did not start another process.")
    if current:
        return
    args = argparse.Namespace(data_dir=DEFAULT_DATA, address=None, replace_certificate=False, **options)
    configure_address(args)
    start(args)


def restart(args: argparse.Namespace) -> None:
    state = stop_for_update()
    if state["mode"] == "stopped":
        configure_address(args)
        start(args)
    else:
        restore_after_update(state)


def install(args: argparse.Namespace) -> None:
    args.save_address = True
    service_args(args)
    private_directory(args.data_dir)
    save_address(args)
    command = [sys.executable, str(Path(__file__).resolve()), "serve", "--data-dir", str(args.data_dir),
               "--host", args.host, "--provider", args.provider]
    if args.insecure_loopback:
        command.append("--insecure-loopback")
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
        # The phone waits for this service over HTTPS, without XPC priority propagation.
        "ThrottleInterval": 5, "ProcessType": "Interactive",
        "StandardOutPath": str(args.data_dir / "logs/service.log"),
        "StandardErrorPath": str(args.data_dir / "logs/error.log"),
    }
    write_agent(plist, payload)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=False)
    if result.returncode:
        raise SystemExit(f"The service file exists at {plist}, but launchctl did not start it.")
    print(f"Installed {LABEL}. The service starts when this Mac account logs in.")
    show_connection(args)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "serve", "start", "stop", "restart", "pair", "save-model-access",
                                            "install-service", "status", "hermes-status"])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, help="Listening port. Defaults to 8960 or the saved address port.")
    parser.add_argument("--address", help="Advertised HTTPS address, including the listening port.")
    parser.add_argument("--replace-certificate", action="store_true",
                        help="Setup only: replace the certificate while stopped. Every phone must pair again.")
    parser.add_argument("--provider", choices=["hermes", "fixture"], default="hermes")
    parser.add_argument("--insecure-loopback", action="store_true")
    args = parser.parse_args()
    args.data_dir = args.data_dir.expanduser().resolve()
    if args.replace_certificate and args.command != "setup":
        parser.error("--replace-certificate is available only with setup.")
    if args.address is not None and args.command not in ADDRESS_COMMANDS:
        parser.error("--address is available with setup, start, serve, install-service, and status.")
    if args.command in ADDRESS_COMMANDS:
        configure_address(args)
    load_credentials(args.data_dir)
    if args.command == "setup":
        show_connection(args)
        pair(args)
    elif args.command == "pair":
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
    elif args.command == "hermes-status":
        # Find and check the person's Hermes with this code. The saved status stays the one of the running service.
        status = hermes_runtime().status(args.data_dir, environ=service_environment(), save=False)
        print("COCONUT_HERMES_STATUS=" + json.dumps(status))
    elif args.command in {"stop", "restart", "status"}:
        if args.data_dir != DEFAULT_DATA:
            raise SystemExit("Use the default data directory for status, stop, and restart.")
        if args.command == "stop":
            stop_for_update()
            print("Coconut is stopped. Its data remains in .runtime.")
        elif args.command == "restart":
            restart(args)
        else:
            process = owned_process()
            if process:
                print(f"Coconut is running with process ID {process[0]}.")
                show_connection(args)
            else:
                print("Coconut is stopped.")
    else:
        command = service_args(args)
        sys.path.insert(0, str(ROOT / "backend"))
        # Older versions installed their own hermes command. It started the private runtime, which is gone.
        if hermes_runtime().remove_coconut_launcher():
            print("Removed Coconut's old hermes command from ~/.local/bin.", flush=True)
        os.environ['PATH'] = service_path(os.environ.get('PATH', ''))
        managed = managed_current_process(args.data_dir)
        os.environ['COCONUT_MANAGED_SERVICE'] = '1' if managed else '0'
        private_directory(args.data_dir)
        os.chdir(ROOT / "backend")
        os.execv(command[0], command)


if __name__ == "__main__":
    main()
