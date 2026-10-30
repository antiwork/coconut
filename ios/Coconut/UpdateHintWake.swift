import Foundation

/// The pause of AppStore's poll loop after a failure. The Mac's sidecar sends an UPDATE_HINT frame when its event cursor
/// moves (docs/relay-transport.md). A long poll in flight returns by itself then, so a hint matters only here: it ends
/// the pause, and the loop fetches the updates at once. A Mac without hints leaves the pause as it was.
@MainActor
final class UpdateHintWake {
    /// A hint never ends a pause sooner than this after it starts. The sidecar sends at most one hint in this time,
    /// so a Mac that fails each request while its cursor moves gets at most four requests a second.
    static let minimumPause: Duration = .milliseconds(250)

    private var pending = false
    private var waiter: (id: UUID, started: ContinuousClock.Instant, continuation: CheckedContinuation<Void, Never>)?

    /// The number of pauses that a hint ended. Tests read it.
    private(set) var wakes = 0

    func hint() {
        guard let waiter else {
            pending = true
            return
        }
        let earliest = waiter.started + Self.minimumPause
        if ContinuousClock.now >= earliest { return end(waiter.id, byHint: true) }
        Task { [weak self] in
            try? await Task.sleep(until: earliest)
            self?.end(waiter.id, byHint: true)
        }
    }

    /// Forgets a hint that came before the next request, which reads that change anyway.
    func clear() { pending = false }

    /// Waits `duration`, or less after a hint. A hint since the last `clear()` also counts. Returns early when the task
    /// is cancelled.
    func pause(for duration: Duration) async {
        let started = ContinuousClock.now
        let id = UUID()
        let timer = Task { [weak self] in
            try? await Task.sleep(for: duration)
            self?.end(id, byHint: false)
        }
        defer { timer.cancel() }
        let hinted = pending
        pending = false
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                if let previous = waiter { previous.continuation.resume() }
                waiter = (id, started, continuation)
                if hinted { hint() }
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.end(id, byHint: false) }
        }
    }

    private func end(_ id: UUID, byHint: Bool) {
        guard let waiter, waiter.id == id else { return }
        self.waiter = nil
        if byHint { wakes += 1 }
        waiter.continuation.resume()
    }
}
