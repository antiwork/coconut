import XCTest
@testable import Coconut

final class WorkPresentationTests: XCTestCase {
    private func work(_ id: String, state: String, created: String) -> Artifact {
        Artifact(id: id, botId: "dev", title: "Inspect the assigned issue", kind: "work", status: state,
                 preview: "", createdAt: created, activityObservedAt: ISO8601DateFormatter().string(from: Date()), workState: state)
    }

    func testWorkingItemsMoveAboveQueueAndDecisions() {
        let first = work("first", state: "working", created: "2026-09-15T01:00:00Z")
        var second = work("second", state: "queued", created: "2026-09-15T02:00:00Z")
        var decision = work("decision", state: "needs_decision", created: "2026-09-15T03:00:00Z")
        decision.workDecisionId = "card"
        let done = work("done", state: "completed", created: "2026-09-15T00:00:00Z")
        let expected = ["work:first", "work:second", "work:decision", "work:done"]
        XCTAssertEqual(TrackedWorkItem.tracked(in: [done, second, first, decision]).map(\.id), expected)
        var updatedFirst = first
        updatedFirst.workState = "waiting"
        second.activity = "Checking the changed file"
        second.workState = "working"
        XCTAssertEqual(TrackedWorkItem.tracked(in: [second, decision, done, updatedFirst]).map(\.id),
                       ["work:second", "work:first", "work:decision", "work:done"])
    }

    func testStaleWorkingStateCannotStayAboveConfirmedExecution() {
        var stale = work("stale", state: "working", created: "2026-09-15T01:00:00Z")
        stale.activityObservedAt = "2026-09-15T01:00:00Z"
        let active = work("active", state: "working", created: "2026-09-15T02:00:00Z")
        XCTAssertEqual(TrackedWorkItem.tracked(in: [stale, active]).map(\.id), ["work:active", "work:stale"])
    }

    func testQueueUsesPositionBeforeCreationTime() {
        var first = work("old", state: "queued", created: "2026-09-15T01:00:00Z")
        first.workQueuePosition = 4
        var second = work("new", state: "queued", created: "2026-09-15T02:00:00Z")
        second.workQueuePosition = 1
        let unknown = work("unknown", state: "queued", created: "2026-09-14T01:00:00Z")
        XCTAssertEqual(TrackedWorkItem.tracked(in: [first, unknown, second]).map(\.id),
                       ["work:new", "work:old", "work:unknown"])
    }

    func testProjectedStateOverridesOlderCompletedReport() {
        var artifact = work("resumed", state: "working", created: "2026-09-15T01:00:00Z")
        artifact.status = "completed"
        artifact.activity = "Reviewing the new result"
        artifact.activityObservedAt = "2026-09-15T02:00:00Z"
        let row = TrackedWorkItem(artifact: artifact, pullRequests: [])
        XCTAssertEqual(row.activity(at: artifact.activityObservedAt!.apiDate!), "Reviewing the new result")
    }

    func testQueuePositionNamesTheBotAndNeverShowsAWorkingSpinner() {
        var artifact = work("queued", state: "queued", created: "2026-09-15T01:00:00Z")
        artifact.workQueuePosition = 1
        var row = TrackedWorkItem(artifact: artifact, pullRequests: [])
        XCTAssertEqual(row.queueLabel(botName: "Gumroad Dev"), "Next for Gumroad Dev")
        XCTAssertFalse(row.isWorking(at: Date()))
        artifact.workQueuePosition = 3
        row = TrackedWorkItem(artifact: artifact, pullRequests: [])
        XCTAssertEqual(row.queueLabel(botName: "Gumroad Dev"), "3rd in Gumroad Dev’s queue")
        artifact.workState = "working"
        XCTAssertNil(TrackedWorkItem(artifact: artifact, pullRequests: []).queueLabel(botName: "Gumroad Dev"))
    }
}
