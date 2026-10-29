"""The owner's bot stops the idle Compose test services of its waiting outcome, and nothing else changes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from coconut.service import ServiceError
from coconut.service_yield import SCHEMA, DockerCli, Refused
from coconut.technical_lane import REFUSED_EVENTS
import test_independent_technical_work as lanes

PROJECT = 'gp4074dev'
MERGE_URL = 'https://github.com/antiwork/gumroad/pull/8723'


def identity(name):
    return hashlib.sha256(name.encode()).hexdigest()


def container(name, project, service, workdir, *, running=True, mounts=(), networks=None, ports=None, oneoff=False,
              config=None, mode=None):
    labels = {'com.docker.compose.project': project, 'com.docker.compose.service': service,
              'com.docker.compose.project.working_dir': workdir,
              'com.docker.compose.project.config_files': config or os.path.join(workdir, 'docker-compose.yml'),
              'com.docker.compose.oneoff': 'True' if oneoff else 'False'}
    return {'Id': identity(name), 'Name': '/' + name, 'Config': {'Labels': labels},
            'State': {'Running': running, 'Paused': False, 'Restarting': False,
                      'Status': 'running' if running else 'exited', 'FinishedAt': '2026-10-28T10:00:00Z'},
            'Mounts': list(mounts), 'HostConfig': {'NetworkMode': mode or project + '_default'},
            'NetworkSettings': {'Networks': {project + '_default': {'NetworkID': 'net-' + project}} if networks is None
                                else networks, 'Ports': ports or {}}}


class FakeDocker:
    """A controlled Docker. It records each call. Only stop changes a container, and only its running state."""

    def __init__(self, items, volumes, networks):
        self.items, self.volumes, self.networks = items, volumes, networks
        self.calls, self.clients = [], []
        self.stop_behavior = None
        self.on_containers = None

    def containers(self):
        self.calls.append(('containers',))
        if self.on_containers:
            self.on_containers()
        return copy.deepcopy(self.items)

    def inspect(self, ids):
        self.calls.append(('inspect', tuple(ids)))
        return [copy.deepcopy(c) for c in self.items if c['Id'] in ids]

    def volume(self, name):
        self.calls.append(('volume', name))
        return copy.deepcopy(self.volumes.get(name, {}))

    def network(self, name):
        self.calls.append(('network', name))
        return copy.deepcopy(self.networks.get(name, {}))

    def port_clients(self, ports):
        self.calls.append(('port_clients', tuple(sorted(ports))))
        return list(self.clients)

    def stop(self, ids):
        self.calls.append(('stop', tuple(ids)))
        if self.stop_behavior:
            return self.stop_behavior(ids)
        self.halt(ids)
        return 0, ''

    def halt(self, ids):
        for item in self.items:
            if item['Id'] in ids:
                item['State'].update(Running=False, Status='exited', FinishedAt='2026-10-29T09:00:00Z')

    def stops(self):
        return [call for call in self.calls if call[0] == 'stop']


class ServiceYieldTests(unittest.TestCase):
    setUp_lane = lanes.IndependentTechnicalWorkTests.setUp
    tearDown = lanes.IndependentTechnicalWorkTests.tearDown
    write = lanes.IndependentTechnicalWorkTests.write
    prepare = lanes.IndependentTechnicalWorkTests.prepare
    snapshot_result = lanes.IndependentTechnicalWorkTests.snapshot_result
    scheduled_native = lanes.IndependentTechnicalWorkTests.scheduled_native
    review_native = lanes.IndependentTechnicalWorkTests.review_native
    lane_native = lanes.IndependentTechnicalWorkTests.lane_native
    lane = lanes.IndependentTechnicalWorkTests.lane

    def setUp(self):
        self.setUp_lane()
        # gp4074: the original owner waits idle in native review on a plain license question.
        self.owner, self.requester = self.lane_native()
        self.gate = self.service._get('decisions', 'gate')
        self.gate.update(title='Which license applies to the bundled fonts?', context='The vendor terms are unclear.')
        self.service._put('decisions', self.gate)
        self.owner = self.service._get('tasks', self.owner['id'])
        # #4080: another outcome of the same bot runs and needs the capacity.
        self.caller = self.caller_of(self.owner['bot_id'])
        self.workspace = Path(self.temp.name) / 'checkouts' / 'gp4074'
        self.workspace.mkdir(parents=True)
        (self.workspace / 'docker-compose.yml').write_text('services: {}\n')
        self.bind(str(self.workspace))
        workdir = str(self.workspace)
        own = 'net-' + PROJECT
        self.docker = FakeDocker([
            container('gp4074dev-web-1', PROJECT, 'web', workdir, ports={'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '3074'}]},
                      mounts=[{'Type': 'bind', 'Source': os.path.join(workdir, 'app'), 'Destination': '/app'}]),
            container('gp4074dev-db-1', PROJECT, 'db', workdir,
                      mounts=[{'Type': 'volume', 'Name': 'gp4074dev_db', 'Destination': '/var/lib/mysql'},
                              {'Type': 'tmpfs', 'Destination': '/tmp'}]),
            container('gp4074dev-migrate-1', PROJECT, 'migrate', workdir, running=False),
            # #4080's own project, and an unrelated project. Neither shares anything with gp4074dev.
            container('gp4080dev-web-1', 'gp4080dev', 'web', '/Users/coconut/elsewhere/gp4080',
                      mounts=[{'Type': 'volume', 'Name': 'gp4080dev_db', 'Destination': '/data'}]),
        ], volumes={'gp4074dev_db': {'Labels': {'com.docker.compose.project': PROJECT}},
                    'gp4080dev_db': {'Labels': {'com.docker.compose.project': 'gp4080dev'}},
                    'shared_cache': {'Labels': {}}},
           networks={PROJECT + '_default': {'Id': own, 'Labels': {'com.docker.compose.project': PROJECT}},
                     'gp4080dev_default': {'Id': 'net-gp4080dev', 'Labels': {'com.docker.compose.project': 'gp4080dev'}},
                     'bridge': {'Id': 'net-bridge', 'Labels': {}}})
        self.service.test_service_docker = self.docker
        self.processes = patch('coconut.service_yield.process_outcomes', return_value={'another-execution'})
        self.process_scan = self.processes.start()
        self.addCleanup(self.processes.stop)
        self.running_ids = [identity('gp4074dev-db-1'), identity('gp4074dev-web-1')]

    def caller_of(self, bot_id):
        caller = dict(copy.deepcopy(self.requester), id='dev-4080-' + bot_id[:8], bot_id=bot_id, status='working',
                      _execution_session_id='dev-4080-' + bot_id[:8], _native_turn_id='turn-4080',
                      _kanban_claim={'native_id': 't_03b20817', 'input': 'turn-4080'})
        for key in ('source_task_id', 'source_bot_id', '_reply_to_task_id', 'parent_task_id'):
            caller.pop(key, None)
        self.service._put('tasks', caller)
        return caller

    def bind(self, location, **changes):
        from coconut.work_capacity import execution_key
        root = self.service.root / 'workspaces'
        root.mkdir(parents=True, exist_ok=True)
        binding = dict(path=location, branch='wt/gp4074', repository='/Users/coconut/gumroad', outcome=execution_key(self.owner))
        saved = {execution_key(self.owner) + '-repo': dict(binding, **changes)}
        (root / 'worktrees.json').write_text(json.dumps(saved))

    def payload(self, **changes):
        return dict(dict(task_id='t_existing', decision_id='gate', decision_revision=2, compose_project=PROJECT), **changes)

    def call(self, caller=None, **changes):
        return self.service.emit((caller or self.caller)['id'], 'service_yield', self.payload(**changes))

    def records(self):
        return [json.loads(row[0]) for row in self.service.db.execute('SELECT data FROM service_yields')]

    def state(self):
        tables = ('tasks', 'decisions', 'artifacts', 'contexts', 'deliveries', 'native_receipts', 'messages')
        tasks = {t['id']: t for t in self.service._all('tasks')}
        tasks.pop(self.caller['id'], None)
        return dict({table: self.service._all(table) for table in tables if table != 'tasks'}, tasks=tasks,
                    kanban=copy.deepcopy(self.service.kanban.tasks),
                    rows=[(r['id'], r['state'], r['input']) for r in self.service._kanban_rows()])

    def refused(self, pattern, status=409, caller=None, **changes):
        before = self.state()
        with self.assertRaisesRegex(ServiceError, pattern) as raised:
            self.call(caller, **changes)
        self.assertEqual(raised.exception.status, status)
        self.assertEqual(self.docker.stops(), [])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.state(), before)
        return raised.exception

    def assert_stopped(self, receipt):
        self.assertEqual(receipt['status'], 'stopped')
        self.assertEqual(receipt['stopped_container_ids'], self.running_ids)
        self.assertEqual({c['id']: c['running'] for c in receipt['observed']}, dict.fromkeys(self.running_ids, False))
        self.assertEqual(self.docker.stops(), [('stop', tuple(self.running_ids))])

    def test_the_owners_bot_stops_the_idle_services_beside_a_pending_license_question(self):
        before = self.state()
        receipt = self.call()
        self.assert_stopped(receipt)
        self.assertEqual((receipt['native_task_id'], receipt['decision_id'], receipt['decision_revision'],
                          receipt['compose_project'], receipt['workspace']),
                         ('t_existing', 'gate', 2, PROJECT, str(self.workspace)))
        self.assertEqual({c['service']: c['running'] for c in receipt['containers']},
                         dict(web=True, db=True, migrate=False))
        # Only the two running containers of gp4074dev stopped. #4080's project and the exited one stay as they were.
        self.assertTrue(next(c for c in self.docker.items if c['Name'] == '/gp4080dev-web-1')['State']['Running'])
        self.assertFalse({'rm', 'down', 'start', 'create', 'kill'} & {call[0] for call in self.docker.calls})
        # The owner, its card, revision and answer, the native card, Work, routes, and messages stay exactly the same.
        self.assertEqual(self.state(), before)
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)
        self.assertEqual(self.service._get('tasks', self.owner['id']), self.owner)
        [record] = self.records()
        self.assertEqual(record['state'], 'stopped')
        self.assertEqual((record['owner_task_id'], record['requester_task_id'], record['bot_id']),
                         (self.owner['id'], self.caller['id'], self.owner['bot_id']))
        self.assertEqual(record['stop_ids'], self.running_ids)
        # A repeat returns the same verified receipt and never stops again.
        self.assertEqual(self.call(), receipt)
        self.assertEqual(len(self.docker.stops()), 1)
        self.assertEqual(self.state(), before)

    def test_a_pending_merge_card_stays_and_the_technical_lane_still_refuses_it(self):
        # gp4068: the one pending card is a merge question. The yield leaves it pending and does not touch the PR.
        merge = dict(self.gate, merge_pr_url=MERGE_URL, title='Merge #8723?')
        self.service._put('decisions', merge)
        with self.assertRaisesRegex(ServiceError, 'holds a tool, a merge, a draft'):
            self.lane(self.requester, dict(scope=['diagnosis']))
        with self.assertRaisesRegex(ServiceError, 'holds a tool, a merge, a draft'):
            self.lane(self.caller, dict(scope=['diagnosis']))
        before = self.state()
        self.assert_stopped(self.call())
        self.assertEqual(self.service._get('decisions', 'gate'), merge)
        self.assertEqual(self.state(), before)
        # The plain technical lane still refuses the merge card after the yield.
        with self.assertRaisesRegex(ServiceError, 'holds a tool, a merge, a draft'):
            self.lane(self.requester, dict(scope=['diagnosis']))
        self.assertEqual(self.state(), before)

    def test_the_requester_another_bot_and_the_running_owner_cannot_stop_them(self):
        # The requester (Desk) keeps its continuation authority, but it cannot act as the owner here.
        self.refused('Only the bot that owns this work', 403, caller=self.requester)
        care = self.write('/v1/bots', {'name': 'Care'})
        self.refused('Only the bot that owns this work', 403, caller=self.caller_of(care['id']))
        # A turn of the owner's own execution is the running owner, not an idle one.
        own = dict(self.owner, id='owner-turn', status='working', _native_turn_id='other-turn', _kanban_claim=None)
        self.service._put('tasks', own)
        self.refused('This work is running', caller=own)
        self.service.db.execute("DELETE FROM tasks WHERE id='owner-turn'")
        paused = dict(self.caller, status='needs_you')
        self.service._put('tasks', paused)
        self.refused('cannot stop test services now', caller=paused)

    def test_a_changed_decision_or_card_refuses(self):
        for changes, pattern in ((dict(decision_revision=1), 'pending decision changed'),
                                 (dict(decision_revision=3), 'pending decision changed'),
                                 (dict(decision_id='scope'), 'pending decision changed'),
                                 (dict(decision_id='unrelated-rejected'), 'pending decision changed')):
            with self.subTest(changes=changes):
                self.refused(pattern, **changes)
        for change in (dict(_receipt_id='call'), dict(action_type='native_approval'),
                       dict(action_type='draft_message', draft={'body': 'Hi'}), dict(inbox_kind='proposal'),
                       dict(_source_recheck=True), dict(decided_by='person-device'), dict(selected_option_id='accept'),
                       dict(_answers=[dict(choice='reply', decided_by='person-device')])):
            with self.subTest(change=sorted(change)):
                self.service._put('decisions', dict(self.gate, **change))
                self.refused('tool approval, a draft, or a recorded answer')
        self.service._put('decisions', dict(self.gate, status='approved', decided_by='person-device'))
        self.refused('pending decision changed')
        self.service._put('decisions', self.gate)
        self.service._put('decisions', dict(id='second', task_id=self.owner['id'], bot_id=self.owner['bot_id'],
                                            status='pending', action_type='question', revision=1))
        self.refused('pending decision changed')
        self.service.db.execute("DELETE FROM decisions WHERE id='second'")
        self.service._put('decisions', dict(id='refusal', task_id=self.owner['id'], bot_id=self.owner['bot_id'],
                                            status='rejected', action_type='question', revision=1, decided_by='person-device'))
        self.refused('declined part of this execution')

    def test_unknown_fields_commands_paths_and_permissions_are_refused(self):
        parameters = SCHEMA['parameters']
        self.assertFalse(parameters['additionalProperties'])
        self.assertEqual(sorted(parameters['properties']),
                         ['compose_project', 'decision_id', 'decision_revision', 'task_id'])
        for changes in (dict(command='docker compose down -v'), dict(action='down'), dict(path='/Users/coconut'),
                        dict(workspace='/tmp'), dict(services=['web']), dict(merge=True), dict(publish=True),
                        dict(scope=['local_fixes']), dict(compose_project='gp4074dev; rm -rf /'),
                        dict(compose_project='--all'), dict(compose_project='GP4074DEV'), dict(compose_project=''),
                        dict(decision_revision='2'), dict(decision_revision=True), dict(task_id='')):
            with self.subTest(changes=changes):
                self.refused('Compose project name, and nothing else', 400, **changes)
        before = self.state()
        with self.assertRaisesRegex(ServiceError, 'nothing else'):
            self.service.emit(self.caller['id'], 'service_yield', dict(task_id='t_existing'))
        self.assertEqual(self.state(), before)

    def test_another_project_or_a_project_from_another_workspace_refuses(self):
        self.refused('no container of this Compose project', compose_project='gp4068dev')
        self.refused('does not come from the workspace', compose_project='gp4080dev')
        for change, pattern in ((dict(workdir='/Users/coconut/other'), 'does not come from the workspace'),
                                (dict(config='/Users/coconut/other/docker-compose.yml'), 'does not come from the workspace'),
                                (dict(config=str(self.workspace / 'other.yml')), 'different Compose configurations')):
            with self.subTest(change=change):
                original = copy.deepcopy(self.docker.items)
                # A different origin on all containers, or a second configuration on one of them.
                for item in self.docker.items[:3] if pattern != 'different Compose configurations' else self.docker.items[1:2]:
                    labels = item['Config']['Labels']
                    if 'workdir' in change:
                        labels['com.docker.compose.project.working_dir'] = change['workdir']
                    else:
                        labels['com.docker.compose.project.config_files'] = change['config']
                self.refused(pattern)
                self.docker.items = original
        # A symbolic link out of the workspace does not count as inside it.
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.workspace / 'link').symlink_to(outside)
        for item in self.docker.items[:3]:
            item['Config']['Labels']['com.docker.compose.project.working_dir'] = str(self.workspace / 'link')
            item['Config']['Labels']['com.docker.compose.project.config_files'] = str(self.workspace / 'link' / 'c.yml')
        self.refused('does not come from the workspace')

    def test_unowned_mounts_shared_volumes_networks_and_consumers_refuse(self):
        db, web = self.docker.items[1], self.docker.items[0]
        cases = (
            (lambda: db['Mounts'].append({'Type': 'volume', 'Name': 'shared_cache', 'Destination': '/cache'}),
             'does not own'),
            (lambda: web['Mounts'].append({'Type': 'bind', 'Source': '/Users/coconut/.ssh', 'Destination': '/keys'}),
             'does not own'),
            (lambda: web['Mounts'].append({'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/sock'}),
             'does not own'),
            (lambda: web['Mounts'].append({'Type': 'npipe', 'Source': 'x', 'Destination': '/x'}), 'does not own'),
            (lambda: web['NetworkSettings']['Networks'].update(bridge={'NetworkID': 'net-bridge'}), 'network that this'),
            (lambda: web['HostConfig'].update(NetworkMode='host'), 'shares a namespace'),
            (lambda: web['HostConfig'].update(NetworkMode='container:' + identity('gp4080dev-web-1')),
             'shares a namespace'),
            (lambda: web['HostConfig'].update(PidMode='host'), 'shares a namespace'),
            (lambda: web['HostConfig'].update(IpcMode='container:gp4080dev-web-1'), 'shares a namespace'),
            (lambda: web['HostConfig'].update(VolumesFrom=['gp4080dev-web-1:ro']), 'volumes of another container'),
            # Another running container that uses the project's volume, network, namespace, or workspace.
            (lambda: self.docker.items[3]['Mounts'].append({'Type': 'volume', 'Name': 'gp4074dev_db', 'Destination': '/d'}),
             'Another container uses'),
            (lambda: self.docker.items[3]['NetworkSettings']['Networks'].update(
                {PROJECT + '_default': {'NetworkID': 'net-' + PROJECT}}), 'Another container uses'),
            (lambda: self.docker.items[3]['HostConfig'].update(NetworkMode='container:gp4074dev-db-1'),
             'Another container uses'),
            # A sidecar that joins the PID or IPC namespace, the volumes, or a link of a project container.
            (lambda: self.docker.items[3]['HostConfig'].update(PidMode='container:' + identity('gp4074dev-web-1')[:12]),
             'Another container uses'),
            (lambda: self.docker.items[3]['HostConfig'].update(IpcMode='container:gp4074dev-db-1'),
             'Another container uses'),
            (lambda: self.docker.items[3]['HostConfig'].update(VolumesFrom=[identity('gp4074dev-db-1') + ':ro']),
             'Another container uses'),
            (lambda: self.docker.items[3]['HostConfig'].update(Links=['/gp4074dev-db-1:/gp4080dev-web-1/db']),
             'Another container uses'),
            (lambda: self.docker.items[3]['Mounts'].append({'Type': 'bind', 'Source': str(self.workspace / 'tmp'),
                                                            'Destination': '/w'}), 'Another container uses'),
            (lambda: self.docker.items.append(container('gp4074dev-run-1', PROJECT, 'web', str(self.workspace), oneoff=True)),
             'one-off command'),
            (lambda: db['State'].update(Paused=True), 'paused or restarting'),
            (lambda: db['State'].update(Restarting=True), 'paused or restarting'),
            (lambda: self.docker.clients.append(dict(pid='42', command='rspec', remote='127.0.0.1:3074')),
             'connected to a port'),
        )
        for change, pattern in cases:
            original, clients = copy.deepcopy(self.docker.items), list(self.docker.clients)
            db, web = self.docker.items[1], self.docker.items[0]
            with self.subTest(pattern=pattern):
                change()
                self.refused(pattern)
            self.docker.items, self.docker.clients = original, clients
            db, web = self.docker.items[1], self.docker.items[0]
        # An anonymous volume that an image declares, such as the data folder of redis, belongs to its container.
        anonymous = identity('anonymous-redis-data')
        self.docker.volumes[anonymous] = {'Labels': {'com.docker.volume.anonymous': ''}}
        self.docker.items[1]['Mounts'].append({'Type': 'volume', 'Name': anonymous, 'Destination': '/data'})
        # With a foreign label, or mounted by another running container, it is not the project's own.
        for volume, mount, pattern in (({'Labels': {'com.docker.volume.anonymous': '', 'owner': 'x'}}, None, 'does not own'),
                                       ({'Labels': {}}, 'other', 'Another container uses')):
            with self.subTest(pattern=pattern):
                saved, mounts = self.docker.volumes[anonymous], list(self.docker.items[3]['Mounts'])
                self.docker.volumes[anonymous] = volume
                if mount:
                    self.docker.items[3]['Mounts'].append({'Type': 'volume', 'Name': anonymous, 'Destination': '/d'})
                self.refused(pattern)
                self.docker.volumes[anonymous], self.docker.items[3]['Mounts'] = saved, mounts
        self.docker.volumes['short-name'] = {'Labels': {}}
        self.docker.items[0]['Mounts'].append({'Type': 'volume', 'Name': 'short-name', 'Destination': '/x'})
        self.refused('does not own')
        self.docker.items[0]['Mounts'].pop()
        # A stopped container of another project is no consumer.
        self.docker.items[3]['Mounts'].append({'Type': 'volume', 'Name': 'gp4074dev_db', 'Destination': '/d'})
        self.docker.items[3]['State']['Running'] = False
        self.assert_stopped(self.call())

    def test_an_active_native_run_process_tool_wait_or_callback_refuses(self):
        native = self.service.kanban.tasks[self.owner['id']]
        for field in ('current_run_id', 'worker_pid'):
            native[field] = 123
            with self.subTest(field=field):
                self.refused('active work')
            native[field] = None
        for status in ('running', 'ready', 'scheduled', 'done', 'blocked'):
            native['status'] = status
            with self.subTest(native=status):
                self.refused('waits idle in native review')
        native['status'] = 'review'
        self.service.running[self.owner['id']] = Mock(is_alive=lambda: True)
        self.refused('active work')
        self.service.running.clear()
        for changes in (dict(status='queued'), dict(status='working'), dict(status='completed'),
                        dict(_waiting_on=['x']), dict(_return_pending=True), dict(_native_reply_pending=True)):
            with self.subTest(owner=changes):
                self.service._put('tasks', dict(self.owner, **changes))
                self.refused('waits idle in native review')
        self.service._put('tasks', self.owner)
        # A process of this execution, or a process table that cannot be read, refuses.
        from coconut.work_capacity import execution_key
        for found, pattern in (({execution_key(self.owner)}, 'process of this work still runs'),
                               (None, 'cannot prove that no process')):
            self.process_scan.return_value = found
            with self.subTest(processes=found):
                self.refused(pattern)
        self.process_scan.return_value = set()
        # A tool without a confirmed result, a process subscription, or a queued callback refuses.
        for state in ('executing', 'awaiting_approval', 'awaiting_answer'):
            receipt = dict(id='unknown', task_id=self.owner['id'], turn_id=self.owner['_native_turn_id'], state=state,
                           action={'name': 'terminal', 'arguments': {'command': 'bin/dev'}}, action_hash='x',
                           created_at='2026-10-28')
            self.service._put('native_receipts', receipt)
            with self.subTest(receipt=state):
                self.refused('no confirmed result')
        self.service.db.execute("DELETE FROM native_receipts WHERE id='unknown'")
        self.service._put('tasks', dict(self.owner, _self_recovery=dict(unknown_tools=[dict(id='old')])))
        self.refused('no confirmed result')
        self.service._put('tasks', self.owner)
        with patch.object(type(self.service), '_background_pending', lambda service, identity, **kw: identity == self.owner['id']):
            self.refused('a process, or a wait')
        callback = dict(copy.deepcopy(self.caller), id='callback', status='queued', _execution_session_id='callback',
                        _reply_to_task_id=self.owner['id'])
        self.service._put('tasks', callback)
        self.refused('still open')
        self.service.db.execute("DELETE FROM tasks WHERE id='callback'")
        for status in ('queued', 'working'):
            self.service._put('contexts', dict(id='reply', task_id=self.owner['id'], bot_id=self.owner['bot_id'],
                                               decision_id='gate', status=status))
            with self.subTest(context=status):
                self.refused('still open')
        self.service.db.execute("DELETE FROM contexts WHERE id='reply'")
        # A paused Work item, a stopped bot, or an earlier conversation refuses.
        item = dict(id='paused-work', task_id=self.owner['id'], bot_id=self.owner['bot_id'], kind='work', title='x',
                    status='waiting', paused_by_decision_id='human-pause', created_at='2026-10-20')
        self.service._put('decisions', dict(id='human-pause', task_id='unrelated', bot_id=self.owner['bot_id'],
                                            status='approved', action_type='question', decided_by='person-device', revision=1))
        self.service._put('artifacts', item)
        self.refused('paused, cancelled')
        self.service.db.execute("DELETE FROM artifacts WHERE id='paused-work'")
        self.service._put('tasks', dict(self.owner, _conversation_epoch='earlier'))
        self.refused('earlier conversation')
        self.service._put('tasks', self.owner)
        self.assert_stopped(self.call())

    def test_native_dispatch_uncertainty_or_a_held_dispatch_gate_refuses(self):
        for flag in ('_kanban_claiming', '_kanban_dispatch_uncertain'):
            with self.subTest(flag=flag), patch.object(self.service, flag, True, create=True):
                self.refused('Native dispatch is in progress')
        held, release = threading.Event(), threading.Event()

        def dispatcher():
            with self.service.dispatch_gate:
                held.set()
                release.wait(10)
        thread = threading.Thread(target=dispatcher)
        thread.start()
        held.wait(5)
        try:
            with patch('coconut.service_yield.GATE_SECONDS', 0.1):
                self.refused('Native dispatch is in progress')
        finally:
            release.set()
            thread.join()
        # Only read-only checks ran before the gate.
        self.assertEqual({call[0] for call in self.docker.calls}, {'containers', 'network', 'volume', 'port_clients'})

    def test_the_slow_checks_run_without_the_dispatch_gate_and_any_change_before_the_gate_stops_nothing(self):
        free = []

        def probe():
            result = []
            thread = threading.Thread(target=lambda: result.append(self.service.dispatch_gate.acquire(blocking=False)
                                                                  and self.service.dispatch_gate.release() is None))
            thread.start()
            thread.join()
            free.append(result[0])
        self.docker.on_containers = probe
        self.assert_stopped(self.call())
        # The first Docker read runs without the gate. The second one runs under it, right before the effect.
        self.assertEqual(free, [True, False])
        # An exec session, a restart, or a removal after the slow checks: the check under the gate stops nothing.
        # The process scan is the last slow check, so the change happens there.
        self.service.db.execute('DELETE FROM service_yields')
        self.docker.calls.clear()
        self.docker.on_containers = None
        for item in self.docker.items[:2]:
            item['State']['Running'] = True
        changes = (
            (lambda: self.docker.items[0].update(ExecIDs=['exec-1']), 'exec session'),
            (lambda: self.docker.items[1]['State'].update(StartedAt='2026-10-29T10:00:00Z'), 'changed while'),
            (lambda: self.docker.items.__setitem__(1, dict(self.docker.items[1], Id=identity('gone'))), 'changed while'),
            (lambda: self.docker.items.append(container('gp4074dev-web-run-1', PROJECT, 'web', str(self.workspace),
                                                        oneoff=True)), 'one-off command'),
            (lambda: self.docker.items.append(container('gp4074dev-worker-1', PROJECT, 'worker', str(self.workspace))),
             'changed while'),
            (lambda: self.docker.items[3]['HostConfig'].update(VolumesFrom=['gp4074dev-db-1']), 'Another container uses'),
            (lambda: self.docker.clients.append(dict(pid='7', command='rspec', remote='127.0.0.1:3074')),
             'connected to a port'))
        for change, pattern in changes:
            original, clients = copy.deepcopy(self.docker.items), list(self.docker.clients)
            with self.subTest(pattern=pattern), patch('coconut.service_yield.process_outcomes',
                                                      side_effect=lambda probe=None: change() or set()):
                self.refused(pattern)
            self.docker.items, self.docker.clients = original, clients
        self.docker.items[0]['ExecIDs'] = ['exec-1']
        self.refused('exec session of this project still runs')

    def test_a_project_of_another_outcomes_nested_worktree_refuses(self):
        # Another outcome's registered worktree inside the owner's workspace belongs to that other outcome.
        root = self.service.root / 'workspaces'
        nested = self.workspace / '.worktrees' / 'gp4068'
        nested.mkdir(parents=True)
        saved = json.loads((root / 'worktrees.json').read_text())
        saved['gp4068-repo'] = dict(path=str(nested), branch='wt/gp4068', repository='/Users/coconut/gumroad',
                                    outcome='gp4068-execution')
        (root / 'worktrees.json').write_text(json.dumps(saved))
        for item in self.docker.items[:3]:
            item['Config']['Labels'].update({'com.docker.compose.project.working_dir': str(nested),
                                             'com.docker.compose.project.config_files': str(nested / 'compose.yml')})
        self.refused('does not come from the workspace')
        for item in self.docker.items[:3]:
            item['Config']['Labels'].update({'com.docker.compose.project.working_dir': str(self.workspace),
                                             'com.docker.compose.project.config_files': str(self.workspace / 'c.yml')})
        self.docker.items[0]['Mounts'][0]['Source'] = str(nested / 'app')
        self.refused('does not own')
        self.docker.items[0]['Mounts'][0]['Source'] = str(self.workspace / 'app')
        self.assert_stopped(self.call())

    def test_a_verified_receipt_is_checked_again_and_a_restarted_project_gets_a_new_guarded_attempt(self):
        first = self.call()
        self.assert_stopped(first)
        # Still stopped: the same receipt, without another stop.
        self.assertEqual(self.call(), first)
        # The services run again under the same pending revision, one of them recreated with a new ID.
        recreated = container('gp4074dev-web-2', PROJECT, 'web', str(self.workspace))
        self.docker.items[0]['State']['Running'] = False
        self.docker.items.append(recreated)
        self.docker.items[1]['State']['Running'] = True
        second = self.call()
        self.assertEqual(second['status'], 'stopped')
        self.assertEqual(second['stopped_container_ids'], sorted([identity('gp4074dev-db-1'), recreated['Id']]))
        self.assertNotEqual(second['verified_at'], None)
        self.assertEqual(len(self.docker.stops()), 2)
        records = sorted(self.records(), key=lambda r: r['id'])
        self.assertEqual([r.get('superseded', False) for r in records], [False, True])
        self.assertEqual(records[1]['stop_ids'], self.running_ids)
        # A fresh attempt passes every check again: an active owner run now refuses it.
        self.docker.items[1]['State']['Running'] = True
        self.service.kanban.tasks[self.owner['id']]['current_run_id'] = 7
        with self.assertRaisesRegex(ServiceError, 'active work'):
            self.call()
        self.assertEqual(len(self.docker.stops()), 2)
        # When Docker cannot answer, Coconut does not claim the services stay stopped.
        self.service.kanban.tasks[self.owner['id']]['current_run_id'] = None
        self.docker.items[1]['State']['Running'] = False
        with patch.object(self.docker, 'containers', side_effect=Refused('Docker did not answer.')):
            with self.assertRaisesRegex(ServiceError, 'cannot confirm that the services stay stopped'):
                self.call()

    def test_a_stale_missing_or_changed_workspace_binding_refuses(self):
        for changes, pattern in ((dict(retired={'archive': 'x'}), 'no single retained workspace'),
                                 (dict(path=str(self.workspace / 'missing')), 'no single retained workspace'),
                                 (dict(path='relative/path'), 'no single retained workspace')):
            with self.subTest(changes=sorted(changes)):
                self.bind(str(self.workspace), **changes)
                self.refused(pattern)
        root = self.service.root / 'workspaces'
        saved = {'a': dict(path=str(self.workspace), outcome=self.owner['_execution_session_id']),
                 'b': dict(path=str(self.workspace), outcome=self.owner['_execution_session_id'])}
        (root / 'worktrees.json').write_text(json.dumps(saved))
        self.refused('no single retained workspace')
        self.bind(str(self.workspace))
        # The registry changes while Docker is read: the transaction sees it and stops nothing.
        other = Path(self.temp.name) / 'checkouts' / 'moved'
        other.mkdir()
        self.docker.on_containers = lambda: self.bind(str(other))
        with self.assertRaisesRegex(ServiceError, 'changed while Coconut checked it'):
            self.call()
        self.assertEqual((self.docker.stops(), self.records()), ([], []))

    def test_a_change_between_the_check_and_the_effect_stops_nothing(self):
        def answered():
            self.service._put('decisions', dict(self.gate, decided_by='person-device', selected_option_id='accept'))
        self.docker.on_containers = answered
        with self.assertRaisesRegex(ServiceError, 'recorded answer'):
            self.call()
        self.assertEqual((self.docker.stops(), self.records()), ([], []))
        self.service._put('decisions', self.gate)

        def claimed():
            self.service.kanban.tasks[self.owner['id']].update(status='running', current_run_id=9)
        self.docker.on_containers = claimed
        with self.assertRaisesRegex(ServiceError, 'waits idle in native review'):
            self.call()
        self.assertEqual((self.docker.stops(), self.records()), ([], []))

    def test_a_failed_stop_is_saved_and_reconciled_without_another_stop(self):
        self.docker.stop_behavior = lambda ids: (1, 'Error response from daemon: cannot stop container')
        receipt = self.call()
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['stop_exit'], 1)
        self.assertTrue(all(c['running'] for c in receipt['observed']))
        self.assertEqual(len(self.docker.stops()), 1)
        # A repeat checks again and never stops again. It stays failed while a container runs.
        again = self.call()
        self.assertEqual((again['status'], again['reconciled']), ('failed', True))
        self.assertEqual(len(self.docker.stops()), 1)
        # After the containers stop by other means, the same receipt is verified.
        self.docker.halt(self.running_ids)
        verified = self.call()
        self.assertEqual((verified['status'], verified['reconciled']), ('stopped', True))
        self.assertEqual(len(self.docker.stops()), 1)
        self.assertEqual(self.service._get('decisions', 'gate'), self.gate)

    def test_an_unknown_stop_is_never_repeated_and_is_reconciled(self):
        def timeout(ids):
            self.docker.halt(ids[:1])
            raise subprocess.TimeoutExpired('docker stop', 120)
        self.docker.stop_behavior = timeout
        receipt = self.call()
        self.assertEqual(receipt['status'], 'unknown')
        self.assertIn('does not stop again', receipt['note'])
        [record] = self.records()
        self.assertEqual(record['stop_error'], 'Docker did not confirm the stop.')
        self.assertEqual(self.call()['status'], 'unknown')
        self.docker.halt(self.running_ids)
        self.assertEqual(self.call()['status'], 'stopped')
        self.assertEqual(len(self.docker.stops()), 1)
        # A verify that fails is unknown too, not stopped.
        self.service.db.execute('DELETE FROM service_yields')
        self.docker.items[0]['State']['Running'] = self.docker.items[1]['State']['Running'] = True
        self.docker.stop_behavior = None
        check = self.docker.inspect

        def after_stop(ids):
            if self.docker.stops()[1:]:
                raise Refused('Docker did not answer.')
            return check(ids)
        with patch.object(self.docker, 'inspect', side_effect=after_stop):
            self.assertEqual(self.call()['status'], 'unknown')

    def test_a_crash_after_the_saved_intent_reconciles_and_never_replays(self):
        def crash(ids):
            raise KeyboardInterrupt
        self.docker.stop_behavior = crash
        with self.assertRaises(KeyboardInterrupt):
            self.call()
        [record] = self.records()
        self.assertEqual((record['state'], record['stop_ids']), ('stopping', self.running_ids))
        # The dispatch gate is free again after the crash of the call.
        self.assertTrue(self.service.dispatch_gate.acquire(blocking=False))
        self.service.dispatch_gate.release()
        self.docker.stop_behavior = None
        receipt = self.call()
        self.assertEqual((receipt['status'], receipt['reconciled']), ('unknown', True))
        self.assertEqual(len(self.docker.stops()), 1)
        # Another bot cannot read or reconcile the receipt.
        self.refused_receipt = self.caller_of(self.requester['bot_id'])
        with self.assertRaisesRegex(ServiceError, 'Only the bot that owns this work'):
            self.call(self.refused_receipt)

    def test_a_shared_work_observer_peer_grants_no_work_and_a_running_peer_refuses(self):
        # gp4074 shares its Work with Desk's completed observer, whose native child waits in TODO on this owner.
        desk = self.service._get('bots', self.requester['bot_id'])
        observer = dict(copy.deepcopy(self.requester), id='desk-observer', status='completed',
                        _execution_session_id='desk-observer', _kanban_claim={'native_id': 't_fdf92423', 'input': 'o'})
        self.service._put('tasks', observer)
        self.service.kanban.tasks['desk-observer'] = dict(id='t_fdf92423', status='todo', current_run_id=None,
                                                          worker_pid=None, parents=['t_existing'])
        item = dict(id='work-gp4074', bot_id=self.owner['bot_id'], task_id=self.owner['id'], kind='work',
                    title='License review', status='waiting', created_at='2026-10-20')
        peer = dict(id='work-observer', bot_id=desk['id'], task_id='desk-observer', kind='work', title='Observe',
                    status='waiting', _same_outcome_as='work-gp4074', created_at='2026-10-21')
        self.service._put('artifacts', item)
        self.service._put('artifacts', peer)
        self.owner = self.service._get('tasks', self.owner['id'])
        before = self.state()
        # Ordinary continuation and the existing technical lane do not admit new work through the yield.
        with self.assertRaisesRegex(ServiceError, 'Wait for this task|another execution'):
            self.service.emit(self.caller['id'], 'work_continue', dict(task_id='t_existing', message='Run the suite.'))
        self.assertEqual(self.state(), before)
        receipt = self.call()
        self.assert_stopped(receipt)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.service._get('artifacts', 'work-gp4074'), item)
        self.assertEqual(self.service._get('tasks', 'desk-observer'), observer)
        self.assertNotIn('_native_outcome_id', self.service._get('tasks', self.owner['id']))
        self.assertNotIn('_work_context_id', self.service._get('tasks', self.owner['id']))
        with self.assertRaisesRegex(ServiceError, 'Wait for this task|another execution'):
            self.service.emit(self.caller['id'], 'work_continue', dict(task_id='t_existing', message='Run the suite.'))
        # The peer's bot cannot act for the owner, and a running peer may use the services.
        self.service.db.execute('DELETE FROM service_yields')
        for item_ in self.docker.items[:2]:
            item_['State']['Running'] = True
        self.docker.calls.clear()
        self.refused('Only the bot that owns this work', 403, caller=self.caller_of(desk['id']))
        self.service.kanban.tasks['desk-observer'].update(status='running', current_run_id=5)
        self.refused('Another execution of this Work is running')

    def test_a_work_peer_without_a_native_card_is_judged_by_its_coconut_state(self):
        # An older review task without a native card shares the Work. The native read never returns it.
        reviewer = dict(copy.deepcopy(self.requester), id='legacy-review', status='completed',
                        _execution_session_id='legacy-review')
        reviewer.pop('_kanban_claim', None)
        reviewer.pop('_native_outcome_id', None)
        self.service._put('tasks', reviewer)
        self.service._put('artifacts', dict(id='work-gp4074', bot_id=self.owner['bot_id'], task_id=self.owner['id'],
                                            kind='work', title='License review', status='waiting', created_at='2026-10-20'))
        self.service._put('artifacts', dict(id='pr-review', bot_id=reviewer['bot_id'], task_id='legacy-review', kind='work',
                                            title='Review', status='waiting', _same_outcome_as='work-gp4074',
                                            created_at='2026-10-21'))
        self.service._put('tasks', dict(reviewer, status='working'))
        self.refused('Another execution of this Work is running')
        self.service._put('tasks', reviewer)
        self.assert_stopped(self.call())

    def test_only_loopback_tcp_ports_pass_and_the_real_lsof_check_sees_each_loopback_client(self):
        web = self.docker.items[0]
        # A later container with a bind mount, after the container that publishes a port, still checks its mount.
        self.docker.items[1]['Mounts'].append({'Type': 'bind', 'Source': str(self.workspace / 'db-init'),
                                               'Destination': '/docker-entrypoint-initdb.d'})
        loopback = {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '3074'}, {'HostIp': '::1', 'HostPort': '3074'}]}
        # A port on every address or a network address can have clients that lsof cannot tie to this Mac.
        refused = [{'3000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '3074'}, {'HostIp': '::', 'HostPort': '3074'}]}]
        for host in ('0.0.0.0', '', None, '192.168.1.20', '10.0.0.5', '::', '2001:db8::5', 'fe80::1',
                     '::ffff:127.0.0.1', '127.0.0.2', 'localhost'):
            refused.append({'3000/tcp': [{'HostIp': host, 'HostPort': '3074'}]})
        refused += [{'3000/tcp': [{'HostPort': '3074'}]}, {'5353/udp': [{'HostIp': '127.0.0.1', 'HostPort': '5353'}]},
                    {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': ''}]}, {'3000/tcp': ['127.0.0.1:3074']},
                    {'3000': [{'HostIp': '127.0.0.1', 'HostPort': '3074'}]}]
        for ports in refused:
            with self.subTest(ports=ports):
                web['NetworkSettings']['Ports'] = ports
                self.refused('not TCP on this Mac’s loopback address')
        # Only exposed ports, without a binding, publish nothing.
        web['NetworkSettings']['Ports'] = dict(loopback, **{'9000/tcp': None, '9001/udp': []})
        # The real lsof parser reads every form of a local client of the loopback port.
        lsof = []

        def run(arguments, **kwargs):
            self.assertEqual(arguments[0], 'lsof')
            return Mock(returncode=0 if lsof else 1, stdout=''.join(lsof))
        self.docker.port_clients = lambda ports: DockerCli('docker').port_clients(ports)
        with patch('coconut.service_yield.subprocess.run', side_effect=run):
            for remote in ('127.0.0.1:3074', '[::1]:3074', '[::ffff:127.0.0.1]:3074', '0.0.0.0:3074', '[::]:3074'):
                lsof[:] = ['p42\ncruby\nn127.0.0.1:61000->' + remote + '\n']
                with self.subTest(client=remote):
                    self.refused('connected to a port')
            lsof[:] = ['p42\ncruby\nn127.0.0.1:61000->127.0.0.1:3074 (unexpected)\n']
            self.refused('cannot read a connection')
            # The server side of the connection and another port's client are no clients of this project.
            lsof[:] = ['p77\ncOrbStack Helper\nn127.0.0.1:3074->127.0.0.1:61000\nn[::1]:3074->[::1]:61001\n'
                       'p90\ncnode\nn127.0.0.1:62000->127.0.0.1:3080\n']
            self.assert_stopped(self.call())

    def test_a_technical_lane_turn_cannot_yield(self):
        self.assertIn('service_yield', REFUSED_EVENTS)
        task, _ = lanes.IndependentTechnicalWorkTests.admitted_turn.__get__(self)()
        # The owner's own lane turn is not idle, and the lane holds the operation before any check.
        with self.assertRaisesRegex(ServiceError, 'Independent technical work cannot'):
            self.service.emit(task['id'], 'service_yield', self.payload())
        self.assertEqual((self.docker.calls, self.records()), ([], []))


class DockerCliTests(unittest.TestCase):
    def test_stop_runs_only_docker_stop_on_exact_ids(self):
        ids = [identity('a'), identity('b')]
        with patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=0, stderr='')) as run:
            self.assertEqual(DockerCli('/usr/local/bin/docker').stop(ids), (0, ''))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['/usr/local/bin/docker', 'stop', '--time', '10', *ids])
        for bad in ([], ['--all'], [identity('a')[:12]], ['$(rm -rf /)']):
            with self.subTest(bad=bad), patch('coconut.service_yield.subprocess.run') as run, self.assertRaises(Refused):
                DockerCli('/usr/local/bin/docker').stop(bad)
            run.assert_not_called()

    def test_reads_use_fixed_read_only_commands(self):
        docker = DockerCli('/usr/local/bin/docker')
        ids = identity('a')
        outputs = {('ps',): ids + '\n', ('container',): json.dumps([{'Id': ids}]),
                   ('volume',): json.dumps([{'Labels': {}}]), ('network',): json.dumps([{'Labels': {}}])}
        commands = []

        def run(arguments, **kwargs):
            commands.append(arguments[1:])
            return Mock(returncode=0, stdout=outputs[(arguments[1],)], stderr='')
        with patch('coconut.service_yield.subprocess.run', side_effect=run):
            self.assertEqual(docker.containers(), [{'Id': ids}])
            docker.volume('gp4074dev_db')
            docker.network('gp4074dev_default')
            self.assertEqual(docker.volume('--help'), {})
        self.assertEqual(commands, [['ps', '--all', '--no-trunc', '--format', '{{.ID}}'], ['container', 'inspect', ids],
                                    ['volume', 'inspect', 'gp4074dev_db'], ['network', 'inspect', 'gp4074dev_default']])

    def test_port_clients_reads_established_local_connections(self):
        output = ('p42\ncruby\nn127.0.0.1:61000->127.0.0.1:3074\n'
                  'p77\ncOrbStack Helper\nn127.0.0.1:3074->127.0.0.1:61000\n'
                  'p90\ncnode\nn127.0.0.1:62000->127.0.0.1:3080\nn10.0.0.2:5000->93.184.216.34:3074\n')
        with patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=0, stdout=output)) as run:
            self.assertEqual(DockerCli('docker').port_clients({3074}),
                             [dict(pid='42', command='ruby', remote='127.0.0.1:3074')])
        self.assertIn('-sTCP:ESTABLISHED', run.call_args.args[0])
        for remote in ('[::1]:3074', '[::ffff:127.0.0.1]:3074'):
            output = 'p42\ncruby\nn[::1]:61000->' + remote + '\n'
            with self.subTest(remote=remote), \
                    patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=0, stdout=output)):
                self.assertEqual(DockerCli('docker').port_clients({3074}), [dict(pid='42', command='ruby', remote=remote)])
        for line in ('n127.0.0.1:61000->127.0.0.1:http', 'n127.0.0.1:61000->3074', 'n127.0.0.1:61000->[::1]'):
            with self.subTest(line=line), self.assertRaisesRegex(Refused, 'cannot read a connection'), \
                    patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=0, stdout='p1\ncx\n' + line)):
                DockerCli('docker').port_clients({3074})
        with patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=1, stdout='')):
            self.assertEqual(DockerCli('docker').port_clients({3074}), [])
        with patch('coconut.service_yield.subprocess.run', return_value=Mock(returncode=2, stdout='')), \
                self.assertRaises(Refused):
            DockerCli('docker').port_clients({3074})
        with patch('coconut.service_yield.subprocess.run') as run:
            self.assertEqual(DockerCli('docker').port_clients(set()), [])
        run.assert_not_called()


class WorkerToolTests(unittest.TestCase):
    def test_the_worker_offers_the_tool_and_the_provider_returns_its_receipt(self):
        root = Path(__file__).resolve().parents[1] / 'coconut'
        worker = (root / 'hermes_worker.py').read_text()
        self.assertIn("request_receipt('service_yield', arguments)", worker)
        provider = (root / 'providers.py').read_text()
        # The provider accepts the event, returns its result as the receipt, and turns a 4xx refusal into a rejection.
        self.assertEqual(provider.count("'work_continue', 'service_yield'"), 4)


if __name__ == '__main__':
    unittest.main()
