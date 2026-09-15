import XCTest
@testable import Coconut

final class WorkPresentationTests: XCTestCase {
    private func work(_ id: String, state: String, created: String) -> Artifact {
        Artifact(id: id, botId: "dev", title: "Inspect the assigned issue", kind: "work", status: state,
                 preview: "", createdAt: created, workState: state)
    }

    func testDecisionMovesFirstWithoutReorderingOtherActiveWork() {
        let first = work("first", state: "working", created: "2026-09-15T01:00:00Z")
        var second = work("second", state: "queued", created: "2026-09-15T02:00:00Z")
        var decision = work("decision", state: "needs_decision", created: "2026-09-15T03:00:00Z")
        decision.workDecisionId = "card"
        let done = work("done", state: "completed", created: "2026-09-15T00:00:00Z")
        let expected = ["work:decision", "work:first", "work:second", "work:done"]
        XCTAssertEqual(TrackedWorkItem.tracked(in: [done, second, first, decision]).map(\.id), expected)
        second.activity = "Checking the changed file"
        second.workState = "working"
        XCTAssertEqual(TrackedWorkItem.tracked(in: [second, decision, done, first]).map(\.id), expected)
    }

    func testQueuePositionNamesTheBotAndNeverShowsAWorkingSpinner() {
        var artifact = work("queued", state: "queued", created: "2026-09-15T01:00:00Z")
        artifact.workQueuePosition = 1
        var row = TrackedWorkItem(artifact: artifact, pullRequest: nil)
        XCTAssertEqual(row.queueLabel(botName: "Gumroad Dev"), "Next for Gumroad Dev")
        XCTAssertFalse(row.isWorking(at: Date()))
        artifact.workQueuePosition = 3
        row = TrackedWorkItem(artifact: artifact, pullRequest: nil)
        XCTAssertEqual(row.queueLabel(botName: "Gumroad Dev"), "3rd in Gumroad Dev’s queue")
        artifact.workState = "working"
        XCTAssertNil(TrackedWorkItem(artifact: artifact, pullRequest: nil).queueLabel(botName: "Gumroad Dev"))
    }
}
