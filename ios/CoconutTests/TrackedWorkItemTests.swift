import XCTest
@testable import Coconut

final class TrackedWorkItemTests: XCTestCase {
    private func item(status: String = "working") -> Artifact {
        Artifact(id: "export", botId: "bot", title: "Export account data", kind: "work", status: status,
                 preview: "", createdAt: "2026-09-13T00:00:00Z", activity: "Preparing the export",
                 activityState: status, activityObservedAt: "2026-09-13T00:00:00Z")
    }

    func testWorkIncludesExplicitItemsButNotFiles() {
        let file = Artifact(id: "file", botId: "bot", title: "Notes", kind: "file", status: "ready", preview: "", createdAt: "")
        XCTAssertEqual(TrackedWorkItem.tracked(in: [file, item()]).map(\.id), ["work:export"])
    }

    func testActiveWorkPrecedesFinishedWork() {
        var finished = item(status: "completed")
        finished.id = "done"; finished.createdAt = "2026-09-14T00:00:00Z"
        XCTAssertEqual(TrackedWorkItem.tracked(in: [finished, item()]).map(\.id), ["work:export", "work:done"])
    }

    func testPullRequestRepresentsItsExistingOutcome() throws {
        let work = item()
        var pr = work
        pr.id = "pr"
        pr.kind = "pr"
        pr.url = "https://github.com/example/project/pull/42"
        pr.workOutcomeId = work.id
        let rows = TrackedWorkItem.tracked(in: [work, pr])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.pullRequest?.number, "42")
        var second = pr
        second.id = "second-pr"
        second.url = "https://github.com/example/project/pull/43"
        let multiple = TrackedWorkItem.tracked(in: [work, pr, second])
        XCTAssertEqual(multiple.map(\.id), ["work:export"])
        XCTAssertNil(multiple.first?.pullRequest)
        XCTAssertEqual(multiple.first?.pullRequests.map(\.number), ["42", "43"])
        let hiddenOutcome = TrackedWorkItem.tracked(in: [second, pr])
        XCTAssertEqual(hiddenOutcome.first?.pullRequests.map(\.number), ["42", "43"])
        pr.workOutcomeId = nil
        XCTAssertEqual(TrackedWorkItem.tracked(in: [work, pr]).count, 2)
    }

    @MainActor
    func testLinkedPullRequestDismissalAndUndoKeepBothCopiesHidden() async throws {
        let work = item(status: "completed")
        var pr = work
        pr.id = "pr"
        pr.kind = "pr"
        pr.url = "https://github.com/example/project/pull/42"
        pr.workOutcomeId = work.id
        pr.workState = "completed"
        let store = AppStore(demo: true)
        store.artifacts = [work, pr]
        let row = try XCTUnwrap(TrackedWorkItem.tracked(in: store.artifacts).first)
        store.dismissWork(row)
        XCTAssertTrue(TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility).isEmpty)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertTrue(TrackedWorkItem.tracked(in: store.artifacts).isEmpty)
        store.undoWorkDismissal()
        XCTAssertEqual(TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility).count, 1)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(TrackedWorkItem.tracked(in: store.artifacts).count, 1)
    }

    func testDisconnectedActivityDoesNotKeepClaimingWork() throws {
        let row = try XCTUnwrap(TrackedWorkItem.tracked(in: [item()]).first)
        let date = try XCTUnwrap(row.artifact.createdAt.apiDate)
        XCTAssertEqual(row.activity(at: date), "Preparing the export")
        XCTAssertEqual(row.activity(at: date.addingTimeInterval(61)), "Status unavailable")
    }

    func testProjectedActivityExpiresAfterDisconnect() throws {
        var artifact = item()
        artifact.workState = "working"
        let row = try XCTUnwrap(TrackedWorkItem.tracked(in: [artifact]).first)
        let date = try XCTUnwrap(artifact.createdAt.apiDate)
        XCTAssertTrue(row.isWorking(at: date))
        XCTAssertFalse(row.isWorking(at: date.addingTimeInterval(61)))
        XCTAssertEqual(row.activity(at: date.addingTimeInterval(61)), "Status unavailable")
    }

    @MainActor
    func testDismissUndoAndRepeatedDismissUseStableItemIdentity() async throws {
        let store = AppStore(demo: true)
        store.artifacts = [item(status: "completed")]
        let row = try XCTUnwrap(TrackedWorkItem.tracked(in: store.artifacts).first)
        store.dismissWork(row)
        XCTAssertTrue(TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility).isEmpty)
        store.undoWorkDismissal()
        store.dismissWork(row)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertTrue(TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility).isEmpty)
        store.undoWorkDismissal()
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(TrackedWorkItem.tracked(in: store.artifacts).count, 1)
    }
}
