"""Stop the idle local Compose test services of one waiting outcome, for the bot that owns it.

An outcome can wait in native review on a person's decision and still hold the local test services that it started.
Another outcome of the same bot can need that capacity. coconut_yield_test_services lets the owner's bot stop those
services while the decision stays pending. Only the bot that owns the outcome can call it. Its requester and other
bots cannot.

The call grants nothing and changes no work. The owner, its native card, its decisions, Work items, pauses,
dependencies, and result route stay as they are. Coconut starts no turn and answers no card. A merge card can stay
pending beside the call, because the call does not touch the PR.

The call names the outcome's native task, its one pending decision and revision, and a Compose project. Coconut reads
the rest itself: the outcome's retained workspace from the workspace registry, and the project's containers, volumes,
and networks from Docker. The project must come from that workspace, and nothing else may use it. The only effect is
`docker stop` on the project's running containers. Coconut never removes, recreates, or starts a container, and never
removes a volume, a network, or a file.

Coconut reads Docker, the connections, and the process table first, without any lock. Then it takes the dispatch
gate, so the owner cannot start, checks Docker and the connections again, and checks the work under the service lock.
It saves the receipt with the exact containers before it stops them. A repeated call returns the saved receipt while
Docker shows the project stopped. If the project runs again, the call checks everything again and can stop it once
more. A receipt without a verified result is checked again, and Coconut never repeats its stop.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

if __package__:
    from .work_workspace import process_outcomes, unlocked_registry
else:
    # Hermes workers import this module from the package directory for its tool schema.
    from work_workspace import process_outcomes, unlocked_registry

PROJECT = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}')
CONTAINER_ID = re.compile(r'[0-9a-f]{64}')
# Compose writes these labels on each container, volume, and network that it creates for a project.
PROJECT_LABEL = 'com.docker.compose.project'
WORKDIR_LABEL = 'com.docker.compose.project.working_dir'
CONFIG_LABEL = 'com.docker.compose.project.config_files'
SERVICE_LABEL = 'com.docker.compose.service'
ONEOFF_LABEL = 'com.docker.compose.oneoff'
# Docker creates an anonymous volume for a VOLUME of an image that the Compose file does not mount, for example the data
# folder of redis. It has a random 64-hex name and no project label. Only the container that created it uses it.
ANONYMOUS_VOLUME = re.compile(r'[0-9a-f]{64}')
ANONYMOUS_LABEL = 'com.docker.volume.anonymous'
# A task-local test setup publishes TCP ports on this Mac's loopback address only. lsof sees every client of such a
# port. A port on another or every address can also have clients on the network, so Coconut refuses it.
LOOPBACK_BINDINGS = ('127.0.0.1', '::1')
# How lsof -n shows a local client's destination for those bindings, including an IPv4-mapped IPv6 socket.
LOOPBACK_CLIENTS = ('127.0.0.1', '[::1]', '[::ffff:127.0.0.1]', '0.0.0.0', '[::]', 'localhost')
# A local test setup has a few services. A larger project is not task-local test setup.
CONTAINER_LIMIT = 32
STOP_GRACE_SECONDS = 10
# The dispatcher holds the dispatch gate only while it claims. A longer hold is a reason to retry later.
GATE_SECONDS = 5
STOP_TIMEOUT_SECONDS = 120
# A receipt in one of these states has no verified result. Coconut checks it again and never stops again.
UNVERIFIED = ('stopping', 'unknown', 'failed')
REFUSED = 'Coconut stopped nothing.'

SCHEMA = {
    'name': 'coconut_yield_test_services',
    'description': 'Stop the running local Compose test services of another outcome that you own, while it waits '
        'idle in native review on a person’s decision, so that other work can use the capacity. Give its native task '
        'ID from kanban_show, its one pending decision ID and current revision, and the Compose project name. Coconut '
        'checks that the project comes from that outcome’s own workspace and that nothing else uses it, then runs '
        'docker stop on its containers only. It removes no container, volume, or file, and it changes no decision, '
        'card, or work. The decision stays pending for the person. The receipt lists each container and its verified '
        'state. A repeated call returns the same receipt while Docker shows the project stopped. If the project runs '
        'again, the call checks everything again and can stop it once more. An unverified stop is never repeated.',
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {'task_id': {'type': 'string', 'maxLength': 100},
                       'decision_id': {'type': 'string', 'maxLength': 100},
                       'decision_revision': {'type': 'integer'},
                       'compose_project': {'type': 'string', 'pattern': '^' + PROJECT.pattern + '$'}},
        'required': ['task_id', 'decision_id', 'decision_revision', 'compose_project']},
}


class Refused(Exception):
    """Docker or the process table shows a reason to stop nothing."""


def requested(payload):
    """The validated request. Unknown fields or values fail closed."""
    from .service import ServiceError
    if (not isinstance(payload, dict) or set(payload) != set(SCHEMA['parameters']['required'])
            or any(not isinstance(payload[key], str) or not 0 < len(payload[key]) <= 100 for key in ('task_id', 'decision_id'))
            or type(payload['decision_revision']) is not int
            or not isinstance(payload['compose_project'], str) or not PROJECT.fullmatch(payload['compose_project'])):
        raise ServiceError(400, 'Give the native task ID, its pending decision ID and revision, and the Compose project '
                                'name, and nothing else.')
    return {key: payload[key] for key in SCHEMA['parameters']['required']}


def inside(path, root):
    """Whether an absolute host path is the root or inside it, after symbolic links."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root)))


def workspace(root, outcome):
    """The one retained workspace of the outcome in the registry, or None. A retired or missing worktree is none.

    `nested` names the saved paths of other outcomes inside it. A worktree can hold another outcome's worktree, and
    that folder belongs to the other outcome.
    """
    saved = [b for b in unlocked_registry(root).values() if isinstance(b, dict)]
    bindings = [b for b in saved if b.get('outcome') == outcome]
    if len(bindings) != 1 or bindings[0].get('retired') or not isinstance(bindings[0].get('path'), str):
        return None
    path = bindings[0]['path']
    if not os.path.isabs(path) or not os.path.isdir(path):
        return None
    nested = sorted(b['path'] for b in saved if b.get('outcome') != outcome and inside(b.get('path'), path))
    return dict(path=path, branch=bindings[0].get('branch'), repository=bindings[0].get('repository'), nested=nested)


def owned(path, binding):
    """Whether the path is in the workspace and not in the worktree of another outcome inside it."""
    return inside(path, binding['path']) and not any(inside(path, other) for other in binding['nested'])


def references(container, own):
    """Whether the container joins a namespace, the volumes, or a link of one of these containers."""
    names = {c['Id'] for c in own} | {state(c)['name'] for c in own}

    def named(reference):
        reference = reference.lstrip('/')
        # Docker accepts an ID prefix of at least 12 characters.
        return reference in names or (len(reference) >= 12 and any(c['Id'].startswith(reference) for c in own))
    host = container.get('HostConfig') or {}
    return (any((host.get(key) or '').startswith('container:') and named(host[key][len('container:'):])
                for key in ('NetworkMode', 'PidMode', 'IpcMode'))
            or any(named(str(item).split(':')[0]) for item in host.get('VolumesFrom') or [])
            or any(named(str(item).split(':')[0]) for item in host.get('Links') or []))


def labels(record):
    return (record.get('Config') or {}).get('Labels') or record.get('Labels') or {}


def state(container):
    current = container.get('State') or {}
    return dict(id=container['Id'], name=(container.get('Name') or '').lstrip('/'),
                service=labels(container).get(SERVICE_LABEL), running=current.get('Running') is True,
                paused=current.get('Paused') is True, restarting=current.get('Restarting') is True,
                status=current.get('Status'), started_at=current.get('StartedAt'), finished_at=current.get('FinishedAt'),
                execs=len(container.get('ExecIDs') or []))


def evidence(docker, project, binding):
    """The project's containers and the IDs to stop, or Refused. Read-only.

    Every container of the project must come from one Compose configuration inside the workspace, and not from the
    worktree of another outcome inside it. Its bind mounts must stay there too, and its volumes and networks must
    belong to the project. An anonymous volume of an image belongs to its container. A published port must be TCP on
    the loopback address. It shares no namespace and no other container's volumes. No other running container may
    share a volume, a network, a namespace, a link, or the workspace, and no local process may hold a connection to a
    published port. A one-off command, an exec session, a paused container, or a restarting container means the
    project is in use.
    """
    root = binding['path']
    containers = docker.containers()
    own = [c for c in containers if labels(c).get(PROJECT_LABEL) == project]
    if not own:
        raise Refused('Docker has no container of this Compose project.')
    if len(own) > CONTAINER_LIMIT:
        raise Refused('This Compose project is larger than a local test setup.')
    if any(not CONTAINER_ID.fullmatch(c.get('Id') or '') for c in own):
        raise Refused('Docker returned a container without a valid ID.')
    origins = {(labels(c).get(WORKDIR_LABEL), labels(c).get(CONFIG_LABEL)) for c in own}
    if len(origins) != 1:
        raise Refused('The containers of this project come from different Compose configurations.')
    working_dir, config = next(iter(origins))
    files = (config or '').split(',')
    if not owned(working_dir, binding) or not all(owned(f, binding) for f in files):
        raise Refused('This Compose project does not come from the workspace of this work.')
    volumes, networks, ports, ids = set(), set(), set(), {c['Id'] for c in own}
    for container in own:
        current = state(container)
        if current['paused'] or current['restarting']:
            raise Refused('A container of this project is paused or restarting.')
        if current['running'] and (labels(container).get(ONEOFF_LABEL, '').lower() == 'true' or current['execs']):
            raise Refused('A one-off command or an exec session of this project still runs.')
        for mount in container.get('Mounts') or []:
            if mount.get('Type') == 'bind' and owned(mount.get('Source'), binding):
                continue
            if mount.get('Type') == 'volume':
                found = labels(docker.volume(mount.get('Name')))
                # Another running container that mounts the same volume refuses below, anonymous or not.
                if (found.get(PROJECT_LABEL) == project
                        or ANONYMOUS_VOLUME.fullmatch(mount.get('Name') or '') and set(found) <= {ANONYMOUS_LABEL}):
                    volumes.add(mount['Name'])
                    continue
            if mount.get('Type') == 'tmpfs':
                continue
            raise Refused('A container of this project mounts a path or volume that this project does not own.')
        host = container.get('HostConfig') or {}
        if (any((host.get(key) or '') == 'host' or (host.get(key) or '').startswith('container:')
                for key in ('NetworkMode', 'PidMode', 'IpcMode')) or host.get('VolumesFrom')):
            raise Refused('A container of this project shares a namespace or the volumes of another container.')
        for name, network in ((container.get('NetworkSettings') or {}).get('Networks') or {}).items():
            if labels(docker.network(name)).get(PROJECT_LABEL) != project:
                raise Refused('A container of this project uses a network that this project does not own.')
            networks.add(network.get('NetworkID') or name)
        for exposed, published in ((container.get('NetworkSettings') or {}).get('Ports') or {}).items():
            for port in published or []:
                if (not str(exposed).endswith('/tcp') or not isinstance(port, dict)
                        or port.get('HostIp') not in LOOPBACK_BINDINGS or not str(port.get('HostPort', '')).isdigit()):
                    raise Refused('A container of this project publishes a port that is not TCP on this Mac’s loopback '
                                  'address, so Coconut cannot see all of its clients.')
                ports.add(int(port['HostPort']))
    for other in containers:
        if other['Id'] in ids or not state(other)['running']:
            continue
        attached = {n.get('NetworkID') or name for name, n in ((other.get('NetworkSettings') or {}).get('Networks') or {}).items()}
        if (any(m.get('Type') == 'volume' and m.get('Name') in volumes
                or m.get('Type') == 'bind' and inside(m.get('Source'), root) for m in other.get('Mounts') or [])
                or attached & networks or references(other, own)):
            raise Refused('Another container uses this project or its workspace.')
    if docker.port_clients(ports):
        raise Refused('A local process is connected to a port of this project.')
    found = [state(c) for c in own]
    return dict(working_dir=working_dir, config_files=files, containers=found,
                stop_ids=sorted(c['id'] for c in found if c['running']))


def verified(docker, ids):
    """The current state of the stopped containers, and whether each one exists and does not run."""
    observed = [state(c) for c in docker.inspect(ids)] if ids else []
    return observed, {c['id'] for c in observed if not c['running'] and not c['paused'] and not c['restarting']} == set(ids)


def docker_executable():
    for candidate in (shutil.which('docker'), '/opt/homebrew/bin/docker', '/usr/local/bin/docker',
                      str(Path.home() / '.orbstack' / 'bin' / 'docker'),
                      '/Applications/OrbStack.app/Contents/MacOS/xbin/docker',
                      '/Applications/Docker.app/Contents/Resources/bin/docker'):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise Refused('Docker is not available on this Mac.')


class DockerCli:
    """Fixed Docker and lsof commands. No argument comes from a bot except a validated project name or container ID."""

    def __init__(self, executable=None):
        self.executable = executable

    def run(self, *arguments, timeout=30):
        self.executable = self.executable or docker_executable()
        try:
            result = subprocess.run([self.executable, *arguments], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Refused('Docker did not answer.') from error
        if result.returncode:
            raise Refused('Docker could not read the containers: ' + (result.stderr.strip() or 'no detail')[:300])
        return result.stdout

    def containers(self):
        ids = self.run('ps', '--all', '--no-trunc', '--format', '{{.ID}}').split()
        return self.inspect(ids) if ids else []

    def inspect(self, ids):
        if any(not CONTAINER_ID.fullmatch(identity) for identity in ids):
            raise Refused('Docker returned a container without a valid ID.')
        return json.loads(self.run('container', 'inspect', *ids))

    def volume(self, name):
        if not isinstance(name, str) or not name or name.startswith('-'):
            return {}
        return json.loads(self.run('volume', 'inspect', name))[0]

    def network(self, name):
        if not isinstance(name, str) or not name or name.startswith('-'):
            return {}
        return json.loads(self.run('network', 'inspect', name))[0]

    def port_clients(self, ports):
        """The local processes with an open connection to one of the published ports."""
        if not ports:
            return []
        try:
            result = subprocess.run(['lsof', '-n', '-P', '-w', '-iTCP', '-sTCP:ESTABLISHED', '-F', 'pcn'],
                                    capture_output=True, text=True, errors='replace', timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Refused('Coconut cannot check the connections without lsof.') from error
        # lsof exits with 1 when no connection matches.
        if result.returncode not in (0, 1) or result.returncode == 1 and result.stdout.strip():
            raise Refused('Coconut cannot check the connections without lsof.')
        found, pid, command = [], None, None
        for line in result.stdout.splitlines():
            if line.startswith('p'):
                pid, command = line[1:], None
            elif line.startswith('c'):
                command = line[1:]
            elif line.startswith('n') and '->' in line:
                remote = line.split('->', 1)[1]
                host, _, port = remote.rpartition(':')
                if not host or not port.isdigit():
                    raise Refused('Coconut cannot read a connection that lsof shows.')
                if host in LOOPBACK_CLIENTS and int(port) in ports:
                    found.append(dict(pid=pid, command=command, remote=remote))
        return found

    def stop(self, ids):
        """Stop the exact containers. Return Docker's exit status and message, or raise when the result is unknown."""
        if not ids or any(not CONTAINER_ID.fullmatch(identity) for identity in ids):
            raise Refused('Coconut stops only the exact containers that it checked.')
        self.executable = self.executable or docker_executable()
        result = subprocess.run([self.executable, 'stop', '--time', str(STOP_GRACE_SECONDS), *ids],
                                capture_output=True, text=True, timeout=STOP_TIMEOUT_SECONDS)
        return result.returncode, result.stderr.strip()[:500]


class ServiceYield:
    def yield_test_services(self, source_id, payload):
        from .service import ServiceError, digest, encoded
        request = requested(payload)
        if not self.kanban:
            raise ServiceError(409, 'Choose one existing native outcome from kanban_show.')
        with self.lock:
            source = self._get('tasks', source_id)
            invocation = (source.get('_native_turn_id'), source.get('_kanban_claim'))
        # The durable owner mapping can read the native board, so it runs before the transaction.
        owner, _ = self._native_continuation_owner(request['task_id'])
        if source['bot_id'] != owner['bot_id']:
            raise ServiceError(403, 'Only the bot that owns this work can stop its test services.')
        key = digest(encoded(['test_service_yield', owner['id'], request['decision_id'], request['decision_revision'],
                              request['compose_project']]))
        with self.lock:
            saved = self._read_one('SELECT data FROM service_yields WHERE id=?', (key,))
        record = json.loads(saved[0]) if saved else None
        # Without a receipt, or when the services run again after a verified stop, a new attempt passes every check.
        # Its slow reads of Docker and the process table run before the gate, so dispatch waits only for the effect.
        attempt = (not record or record['state'] == 'stopped' and self._yield_running_again(record))
        found = self._yield_evidence(owner, request) if attempt else None
        # The dispatcher needs this gate to start a turn, so the owner cannot start while Coconut checks and stops.
        if not self.dispatch_gate.acquire(timeout=GATE_SECONDS):
            raise ServiceError(409, 'Native dispatch is in progress. Read its current activity and retry. ' + REFUSED)
        try:
            with self.lock:
                if self._read_one('SELECT data FROM service_yields WHERE id=?', (key,)) != saved:
                    raise ServiceError(409, 'Another call changed this receipt. Retry to read it. ' + REFUSED)
            if not attempt:
                return self._reconcile_yield(record, source)
            return self._yield(source_id, invocation, request, owner, key, record, found)
        finally:
            self.dispatch_gate.release()

    def _yield_running_again(self, record):
        """Whether a container of the stopped project runs now, including a recreated one. Docker must answer."""
        from .service import ServiceError
        try:
            return any(labels(c).get(PROJECT_LABEL) == record['compose_project'] and state(c)['running']
                       for c in self._yield_docker().containers())
        except Refused as error:
            raise ServiceError(409, str(error) + ' Coconut cannot confirm that the services stay stopped.') from None

    def _yield_docker(self):
        return getattr(self, 'test_service_docker', None) or DockerCli()

    def _yield_peers(self, owner, related_ids):
        """The tasks of other executions that share a Work item of the owner. They must not run."""
        identities = set()
        for item in self._task_work_items(owner):
            for peer in self._work_pause_peers(item, shared=True):
                identities.update(peer.get(key) for key in ('task_id', '_review_task_id', '_activity_task_id'))
        return identities - related_ids - {None}

    def _yield_evidence(self, owner, request):
        """The workspace and the project's containers, from the read-only checks. They run without any lock.

        The process scan is the slow part. The Docker and connection checks run again under the gate.
        """
        from .service import ServiceError
        from .work_capacity import execution_key
        if getattr(self, '_kanban_claiming', False) or getattr(self, '_kanban_dispatch_uncertain', False):
            raise ServiceError(409, 'Native dispatch is in progress. Read its current activity and retry. ' + REFUSED)
        binding = workspace(self.root / 'workspaces', execution_key(owner))
        if not binding:
            raise ServiceError(409, 'This work has no single retained workspace. ' + REFUSED)
        try:
            found = evidence(self._yield_docker(), request['compose_project'], binding)
            outcomes = process_outcomes(getattr(self.provider, 'python', None))
            if outcomes is None:
                raise Refused('Coconut cannot prove that no process of this work runs.')
            if execution_key(owner) in outcomes:
                raise Refused('A process of this work still runs.')
        except Refused as error:
            raise ServiceError(409, str(error) + ' ' + REFUSED) from None
        return dict(found, binding=binding)

    def _yield(self, source_id, invocation, request, owner, key, superseded, found):
        from .service import ServiceError, now
        from .work_capacity import execution_key
        root, binding, docker = self.root / 'workspaces', found['binding'], self._yield_docker()
        with self.lock:
            related_ids = {t['id'] for t in self._all('tasks') if execution_key(t) == execution_key(owner)}
            peers = self._yield_peers(owner, related_ids)
        # Under the gate, check Docker and the published ports again. A new container of the project or a consumer,
        # a new port client, a start, a stop, a removal, or an exec session since the slow checks refuses.
        try:
            same = evidence(docker, request['compose_project'], binding) == {k: v for k, v in found.items() if k != 'binding'}
        except Refused as error:
            raise ServiceError(409, str(error) + ' ' + REFUSED) from None
        if not same:
            raise ServiceError(409, 'The Compose project changed while Coconut checked it. Retry. ' + REFUSED)
        # A fresh native read of the owner and its Work peers, after the slow reads. A cached snapshot can miss a claim.
        fresh = {t['external_id']: t for t in self.kanban.call(
            {'op': 'snapshot', 'external_ids': sorted({owner['id'], *peers})})['tasks']}

        def changed():
            raise ServiceError(409, 'The work changed while Coconut checked it. Read it and retry. ' + REFUSED)

        with self.transaction():
            source = self._get('tasks', source_id)
            if (source.get('_native_turn_id'), source.get('_kanban_claim')) != invocation:
                raise ServiceError(409, 'The calling turn changed while checking this work. ' + REFUSED)
            snapshots = dict(self.kanban.tasks) | fresh
            # Re-read the owner under the lock. A native task that the fresh read did not cover means it changed.
            current, native = self._native_continuation_owner(
                request['task_id'], lambda identities: set(identities) - set(snapshots) and changed(), snapshots)
            if current['id'] != owner['id'] or workspace(root, execution_key(current)) != binding:
                changed()
            related = [t for t in self._all('tasks') if execution_key(t) == execution_key(current)]
            # A peer with a native card must be in the fresh read. A peer without one has only its Coconut state.
            tasks = {t['id']: t for t in self._all('tasks')}
            if {identity for identity in self._yield_peers(current, {t['id'] for t in related}) - set(fresh)
                    if identity not in tasks or tasks[identity].get('_native_outcome_id')
                    or (tasks[identity].get('_kanban_claim') or {}).get('native_id')}:
                changed()
            self._yield_guard(source, current, native, related, request, fresh)
            saved = self._read_one('SELECT data FROM service_yields WHERE id=?', (key,))
            if (json.loads(saved[0]) if saved else None) != superseded:
                changed()
            if superseded:
                # The verified receipt stays as history under its own ID.
                self.db.execute('DELETE FROM service_yields WHERE id=?', (key,))
                self._put('service_yields', dict(superseded, id=key + ':' + superseded['created_at'], superseded=True))
            record = dict(id=key, state='stopping', bot_id=current['bot_id'], owner_task_id=current['id'],
                          native_task_id=native['id'], requester_task_id=source_id, decision_id=request['decision_id'],
                          decision_revision=request['decision_revision'], compose_project=request['compose_project'],
                          workspace=binding['path'], working_dir=found['working_dir'], config_files=found['config_files'],
                          containers=found['containers'], stop_ids=found['stop_ids'], created_at=now())
            # The intent and the exact containers are saved before Docker runs. A crash leaves an unverified receipt.
            self._put('service_yields', record)
        stop = None
        if record['stop_ids']:
            try:
                stop = docker.stop(record['stop_ids'])
            except Exception:
                # Docker may or may not have stopped them. Only the check below can tell.
                stop = None
                record['stop_error'] = 'Docker did not confirm the stop.'
        else:
            stop = (0, '')
        return self._finish_yield(record, stop, docker)

    def _finish_yield(self, record, stop, docker):
        from .service import now
        try:
            observed, stopped = verified(docker, record['stop_ids'])
        except Exception:
            observed, stopped = None, False
        if stop is not None:
            record.update(stop_exit=stop[0], stop_message=stop[1])
        if stopped:
            record['state'] = 'stopped'
        elif observed is not None and (stop[0] if stop is not None else record['state'] == 'failed'):
            # Docker reported a failure, and a container still runs.
            record['state'] = 'failed'
        else:
            record['state'] = 'unknown'
        record.update(observed=observed, verified_at=now())
        with self.transaction():
            self._put('service_yields', record)
        return self._yield_receipt(record)

    def _reconcile_yield(self, record, source):
        """Return a saved receipt. Check an unverified one again, and never repeat its stop."""
        from .service import ServiceError
        if source['bot_id'] != record['bot_id']:
            raise ServiceError(403, 'Only the bot that owns this work can read its test service receipt.')
        if record['state'] in UNVERIFIED:
            record['reconciled'] = True
            return self._finish_yield(record, None, self._yield_docker())
        return self._yield_receipt(record)

    @staticmethod
    def _yield_receipt(record):
        note = {'stopped': 'Docker shows each listed container stopped. Nothing was removed. The decision stays pending.',
                'failed': 'Docker did not stop every container. Coconut does not stop again. Check them before new work.',
                'unknown': 'Coconut cannot verify the stop. It does not stop again. Check the containers before new work.'}
        return dict(status=record['state'], receipt_id=record['id'], native_task_id=record['native_task_id'],
                    compose_project=record['compose_project'], decision_id=record['decision_id'],
                    decision_revision=record['decision_revision'], workspace=record['workspace'],
                    stopped_container_ids=record['stop_ids'], containers=record['containers'],
                    observed=record.get('observed'), stop_exit=record.get('stop_exit'),
                    reconciled=record.get('reconciled', False), verified_at=record.get('verified_at'),
                    note=note[record['state']])

    def _yield_guard(self, source, owner, native, related, request, snapshots):
        """Refuse unless the owner is idle on its exact pending decision and nothing of its execution runs."""
        from .service import ServiceError
        related_ids = {t['id'] for t in related}
        if source['status'] != 'working' or self._task_paused(source):
            raise ServiceError(409, 'This task cannot stop test services now.')
        if source['bot_id'] != owner['bot_id']:
            raise ServiceError(403, 'Only the bot that owns this work can stop its test services.')
        if source['id'] in related_ids:
            raise ServiceError(409, 'This work is running. Stop its own services in its own turn.')
        bot = self._get('bots', owner['bot_id'])
        if (self._task_paused(owner) or owner['session_id'] != bot['session_id']
                or owner.get('_conversation_epoch') != self._setting('conversation_epoch')
                or self._get('exchanges', owner['exchange_id']).get('cancelled')
                or any(self._work_pause(item) for item in self._task_work_items(owner))):
            raise ServiceError(409, 'This work is paused, cancelled, or belongs to an earlier conversation. ' + REFUSED)
        if getattr(self, '_kanban_claiming', False) or getattr(self, '_kanban_dispatch_uncertain', False):
            raise ServiceError(409, 'Native dispatch is in progress. Read its current activity and retry. ' + REFUSED)
        worker = self.running.get(owner['id'])
        if (owner['status'] != 'needs_you' or native.get('status') != 'review'
                or owner.get('_waiting_on') or owner.get('_return_pending') or owner.get('_native_reply_pending')):
            raise ServiceError(409, 'Only work that waits idle in native review on its decision can yield its test '
                                    'services. ' + REFUSED)
        if (native.get('current_run_id') or native.get('worker_pid') or (worker and worker.is_alive())
                or any(t['id'] != owner['id'] and t['status'] in self.ACTIVE for t in related)
                or any(self._background_pending(identity) for identity in related_ids)):
            raise ServiceError(409, 'This execution has active work, a process, or a wait. Wait for its result. ' + REFUSED)
        from .technical_lane import PENDING_RECEIPT_STATES
        if ((owner.get('_self_recovery') or {}).get('unknown_tools')
                or any(r['state'] in PENDING_RECEIPT_STATES for identity in related_ids
                       for r in self._native_task_receipts(identity))):
            raise ServiceError(409, 'A tool of this execution has no confirmed result. Check it first. ' + REFUSED)
        if (any(c['task_id'] in related_ids and c['status'] in ('queued', 'working') for c in self._all('contexts'))
                or any(t.get('_reply_to_task_id') in related_ids and (t['status'] in self.ACTIVE or t.get('_return_pending')
                       or t.get('_native_reply_pending')) for t in self._all('tasks'))):
            raise ServiceError(409, 'A request or a result of this execution is still open. Wait for it. ' + REFUSED)
        tasks = {t['id']: t for t in self._all('tasks')}
        for identity in self._yield_peers(owner, related_ids) - {source['id']}:
            peer, card, worker = tasks.get(identity) or {}, snapshots.get(identity) or {}, self.running.get(identity)
            if (peer.get('status') == 'working' or (worker and worker.is_alive())
                    or card.get('current_run_id') or card.get('worker_pid') or card.get('status') == 'running'):
                raise ServiceError(409, 'Another execution of this Work is running. Wait for it. ' + REFUSED)
        decisions = [d for d in self._all('decisions') if d['task_id'] in related_ids]
        pending = [d for d in decisions if d['status'] == 'pending']
        gate = pending[0] if len(pending) == 1 else None
        if not gate or gate['id'] != request['decision_id'] or gate.get('revision') != request['decision_revision']:
            raise ServiceError(409, 'The pending decision changed. Read it and retry with its exact ID and revision. ' + REFUSED)
        # The card stays as it is. A tool approval, a draft, or a recorded answer is about to act, so Coconut waits.
        # A merge question can stay pending: this call does not touch the PR.
        if (gate.get('action_type') != 'question' or gate.get('inbox_kind', 'decision') != 'decision'
                or gate.get('_receipt_id') or gate.get('draft') is not None or gate.get('_source_recheck')
                or gate.get('decided_by') or gate.get('selected_option_id')
                or any(answer.get('decided_by') for answer in gate.get('_answers') or [])):
            raise ServiceError(409, 'This decision holds a tool approval, a draft, or a recorded answer. Wait for it. '
                               + REFUSED)
        if any(d['status'] == 'rejected' for d in decisions):
            raise ServiceError(409, 'A person declined part of this execution. ' + REFUSED)
        return gate
