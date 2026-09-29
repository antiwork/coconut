"""Exercise unlink admission through the real dispatcher, not a stubbed idle pass."""
from contextlib import ExitStack
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from coconut.service import ServiceError
import test_dependency_unlink as fixtures


class UnlinkDispatchIdleTests(unittest.TestCase):
    service: Any
    requests: list[dict]
    setUp = fixtures.DependencyUnlinkTests.setUp
    tearDown = fixtures.DependencyUnlinkTests.tearDown
    write = fixtures.DependencyUnlinkTests.write
    prepare = fixtures.DependencyUnlinkTests.prepare
    setup_edge = fixtures.DependencyUnlinkTests.setup_edge
    unlink = fixtures.DependencyUnlinkTests.unlink

    def check_stage(self, stage, *, restart=False, lost_response=False):
        child, parent, source = self.setup_edge()
        entered, release = threading.Event(), threading.Event()
        errors = []
        adapter_call = self.service.kanban.call.side_effect
        real_wait = self.service.changed.wait
        phase = {'lost': lost_response}

        def hold():
            entered.set()
            if not release.wait(12):
                raise AssertionError('Test did not release dispatcher')

        def call(request, spawn=None):
            if request['op'] == 'tick':
                if phase['lost']:
                    raise RuntimeError('Lost native tick response')
                if stage == 'claim':
                    hold()
                return {}
            return adapter_call(request)

        def trim(keep):
            if stage == 'trim':
                hold()

        def idle(timeout=None):
            if stage != 'idle':
                return
            entered.set()
            deadline = time.monotonic() + 12
            while not release.is_set():
                if time.monotonic() >= deadline:
                    raise AssertionError('Test did not release dispatcher idle')
                real_wait(.1)

        def dispatch():
            try:
                self.service._dispatch_pending()
            except BaseException as error:
                errors.append(error)

        self.service.kanban.call.side_effect = call
        with ExitStack() as stack:
            for name in ('_migrate_native_work', '_adopt_native_outcomes', '_reconcile_kanban',
                         '_dispatch_chat', '_publish_kanban_changes'):
                stack.enter_context(patch.object(self.service, name))
            stack.enter_context(patch.object(self.service, '_kanban_rows', return_value=[]))
            stack.enter_context(patch.object(self.service.provider, 'trim_kanban_workers', side_effect=trim, create=True))
            stack.enter_context(patch.object(self.service.changed, 'wait', side_effect=idle))
            if lost_response:
                with self.assertRaisesRegex(RuntimeError, 'Lost native tick response'):
                    self.service._dispatch_pending()
                self.assertEqual(self.service._kanban_claiming, 0)
                self.assertTrue(self.service._kanban_dispatch_uncertain)
                with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                    self.unlink(source)
                self.assertEqual(self.requests, [])
                phase['lost'] = False
            self.service.restart_pending = restart
            worker = threading.Thread(target=dispatch, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), errors)
                if stage == 'claim':
                    self.assertEqual(self.service._kanban_claiming, 1)
                    self.assertTrue(self.service._kanban_dispatch_uncertain)
                    with self.assertRaisesRegex(ServiceError, 'dispatch is in progress'):
                        self.unlink(source)
                    self.assertEqual(self.requests, [])
                else:
                    # The foreground reservation remains, but there is no native claim in flight.
                    self.assertTrue(self.service._kanban_dispatching)
                    self.assertEqual(self.service._kanban_claiming, 0)
                    self.assertFalse(self.service._kanban_dispatch_uncertain)
                    receipt = self.unlink(source)
                    self.assertEqual(receipt['status'], 'unlinked')
                    self.assertFalse(release.is_set())
                    self.assertEqual(self.service._get('tasks', child['id']), child)
                    self.assertEqual(self.service._get('tasks', parent['id']), parent)
            finally:
                release.set()
                with self.service.changed:
                    self.service.changed.notify_all()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.assertEqual(self.service._kanban_claiming, 0)
        self.assertFalse(self.service._kanban_dispatch_uncertain)
        self.assertFalse(self.service._kanban_dispatching)

    def test_unlink_during_slow_idle_worker_trim(self):
        self.check_stage('trim')

    def test_unlink_during_actual_dispatcher_condition_wait(self):
        self.check_stage('idle')

    def test_unlink_during_reconciled_restart_idle(self):
        self.check_stage('idle', restart=True)

    def test_native_claim_keeps_the_gate_closed(self):
        self.check_stage('claim')

    def test_lost_tick_requires_reconciliation_before_idle_unlink(self):
        self.check_stage('idle', lost_response=True)


if __name__ == '__main__':
    unittest.main()
