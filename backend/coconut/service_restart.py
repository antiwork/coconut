"""Bound the queue pause before one launchd-managed service restart."""
import time

# An update stops the service. Turns that are still running would lose their tool results, and Coconut fails such a task at the
# next start so that it never repeats an action. So the updater first asks the service to drain (see `poll_service_drain`).
DRAIN_REQUEST_FILE = 'drain-request'
DRAINED_FILE = 'drained'
# A restart for a new Hermes waits for the running turns as long as an update does (host.DRAIN_SECONDS).
HERMES_DRAIN_SECONDS = 600


class ServiceRestart:
    def poll_service_drain(self):
        """Pause new turns while the updater asks for it, and say when no turn is running.

        The updater writes the time at which the request ends into `drain-request`. Until then, no new turn starts and the
        running turns finish. When none is left, this writes `drained`, and the updater stops the service. A request that
        nobody removes ends by itself, so a failed update cannot leave the service paused.
        """
        request, drained = self.root / DRAIN_REQUEST_FILE, self.root / DRAINED_FILE
        try:
            until = float(request.read_text()) if request.is_file() else 0.0
        except (OSError, ValueError):
            until = 0.0
        if time.time() >= until:
            self._draining = False
            if getattr(self, '_drain_holds_dispatch', False):
                self._drain_holds_dispatch = False
                # A new Hermes or a workspace recovery keeps the pause that it also needs.
                if self.hermes_restart_deadline is None and not getattr(self, '_workspace_recovery_hold', False):
                    self.restart_pending = False
                with self.changed:
                    self.changed.notify_all()
            for path in (request, drained):
                path.unlink(missing_ok=True)
            return
        self._draining = True
        if not self.restart_pending:
            self._drain_holds_dispatch = True
            self.restart_pending = True
        with self.lock:
            active = self._service_busy()
        if not active and not drained.exists():
            drained.write_text('1')

    def _service_busy(self):
        # Caller holds self.lock. The work that keeps a restart for a new Hermes from taking effect: turns, claims and
        # uncertain dispatch.
        return (any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()])
                or bool(self._kanban_claiming) or bool(self._kanban_dispatch_uncertain))

    def poll_hermes_change(self):
        """After a change of Hermes, start no new turn until the restart. See hermes_changed and take_service_restart."""
        if self.hermes_restart_deadline is None:
            if not self.hermes_changed():
                return
            self.hermes_restart_deadline = time.monotonic() + HERMES_DRAIN_SECONDS
            print('Hermes changed. Coconut restarts when the running turns finish.', flush=True)
        # The end of another pause releases dispatch (see poll_service_drain). This pause holds until the restart.
        self.restart_pending = True

    def clear_service_drain(self):
        for name in (DRAIN_REQUEST_FILE, DRAINED_FILE):
            (self.root / name).unlink(missing_ok=True)

    def take_service_restart(self):
        # An update is about to stop the service. A restart now would start a process that removes the drain request,
        # and the updater would wait for an idle mark that never comes. The update supersedes the restart.
        if getattr(self, '_draining', False):
            return False
        # The HTTP accept loop must remain available while another request owns the queue.
        if not self.dispatch_gate.acquire(blocking=False):
            return False
        if not self.lock.acquire(blocking=False):
            self.dispatch_gate.release()
            return False
        try:
            recovery = self._pending_workspace_recovery()
            # A workspace recovery restarts the service too, so a new Hermes waits for it.
            hermes = self.hermes_restart_deadline is not None and not recovery
            if ((not recovery and not hermes) or self.stopping.is_set()
                    or getattr(self, '_workspace_recovery_hold', False)):
                return False
            active_turns = any(t.is_alive() for t in [*self.running.values(), *self.context_threads.values(), *self.gateway_cron_threads.values()])
            if hermes:
                # As an update does: wait for the running work, but not longer than HERMES_DRAIN_SECONDS.
                if self._service_busy() and time.monotonic() < self.hermes_restart_deadline:
                    return False
            elif active_turns:
                return False
            if recovery:
                try:
                    self.kanban.call({'op': 'snapshot'})
                    self._reconcile_kanban()
                    # No tick/reclaim here: read the native ledger before anything
                    # can discard evidence of surviving or ambiguous writers.
                    self._check_recovery_writers()
                except Exception as error:
                    self._refuse_workspace_recovery(recovery, error)
                    return False
            self.restart_pending = False
            self.stopping.set()
            self.commits.notify()
            return True
        finally:
            self.lock.release()
            self.dispatch_gate.release()
