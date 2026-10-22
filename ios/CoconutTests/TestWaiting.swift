import XCTest

extension XCTestCase {
    /// A CI simulator can stall for several seconds, mostly just after it boots. One stalled request costs the app
    /// its foreground budget (6 s), a retry and a backoff, so the default wait is longer than that cycle.
    /// The wait ends as soon as the condition holds, so a fast run does not get slower.
    @MainActor
    func waitUntil(timeout: Duration = .seconds(20), _ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + timeout
        while !condition(), ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(50))
        }
    }
}
