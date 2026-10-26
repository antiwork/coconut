import XCTest
@testable import Coconut

final class WorkActivityTests: XCTestCase {
    private func artifact(id: String = "a") -> Artifact {
        Artifact(id: id, botId: "bot", title: "Review", kind: "pr", status: "merged", preview: "",
                 url: "https://github.com/team/repo/pull/42", createdAt: "2026-09-13T00:00:00Z", prState: "merged")
    }

    func testDismissalHidesAllCopiesAndUndoRestoresOneRow() {
        var dismissed = artifact()
        dismissed.workDismissalId = "action"
        let copies = [dismissed, artifact(id: "b")]
        XCTAssertTrue(TrackedPullRequest.tracked(in: copies).isEmpty)
        XCTAssertEqual(TrackedPullRequest.tracked(in: copies, visibility: [dismissed.url!: false]).count, 1)
    }

    @MainActor
    func testRapidDismissUndoAndRepeatedDismiss() async throws {
        let store = AppStore(demo: true)
        store.artifacts = [artifact()]
        let row = try XCTUnwrap(TrackedPullRequest.tracked(in: store.artifacts).first)
        store.dismissWork(row)
        XCTAssertTrue(TrackedPullRequest.tracked(in: store.artifacts, visibility: store.workVisibility).isEmpty)
        store.undoWorkDismissal()
        XCTAssertEqual(TrackedPullRequest.tracked(in: store.artifacts, visibility: store.workVisibility).count, 1)
        store.dismissWork(row)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertTrue(TrackedPullRequest.tracked(in: store.artifacts, visibility: store.workVisibility).isEmpty)
        store.undoWorkDismissal()
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(TrackedPullRequest.tracked(in: store.artifacts, visibility: store.workVisibility).count, 1)
    }
}
