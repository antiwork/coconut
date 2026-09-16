import XCTest
@testable import Coconut

final class WorkTests: XCTestCase {
    func testCancellationReceiptCannotOverwriteLaterResume() throws {
        var current = artifact()
        current.workState = "queued"
        var stopped = current
        stopped.workState = "cancelled"
        let receipt = WorkCancellationReceipt(ok: true, artifacts: [stopped], cursor: 12)
        XCTAssertEqual(receipt.applying(to: [current], after: 11)?.first?.workState, "cancelled")
        XCTAssertNil(receipt.applying(to: [current], after: 13))
        XCTAssertNil(receipt.applying(to: [current], after: 12))
        let legacy = try JSONDecoder().decode(WorkCancellationReceipt.self, from: Data(#"{"ok":true}"#.utf8))
        XCTAssertNil(legacy.applying(to: [current], after: 11))
    }

    private func artifact(id: String = "a", bot: String = "bot-a", url: String = "https://github.com/team/repo/pull/42") -> Artifact {
        Artifact(id: id, botId: bot, title: "Fix retry", kind: "pr", status: "preparing", preview: "", url: url,
                 createdAt: "2026-09-13T00:00:00Z", checkedAt: "2026-09-13T00:00:00Z", observedAt: "2026-09-13T00:00:00Z",
                 prState: "open", draft: true, checksState: "running")
    }

    func testSamePRHasOneRowAndBothBots() throws {
        let rows = TrackedPullRequest.active(in: [artifact(), artifact(id: "b", bot: "bot-b", url: "https://github.com/team/repo/pull/42/")])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(try XCTUnwrap(rows.first).botIds, ["bot-a", "bot-b"])
    }

    func testLatestClosedObservationRemovesPR() {
        var closed = artifact(id: "b")
        closed.checkedAt = "2026-09-13T00:01:00Z"; closed.prState = "closed"
        XCTAssertTrue(TrackedPullRequest.active(in: [artifact(), closed]).isEmpty)
    }

    func testFailureFromAnotherBotCannotRestoreMergedPR() {
        var closed = artifact(id: "closed", bot: "bot-b")
        closed.observedAt = "2026-09-13T00:01:00Z"; closed.checkedAt = closed.observedAt; closed.prState = "merged"
        var failed = artifact(id: "failed")
        failed.checkedAt = "2026-09-13T00:02:00Z"; failed.refreshError = "No access"
        XCTAssertTrue(TrackedPullRequest.active(in: [closed, failed]).isEmpty)
    }

    func testDraftShowsRunningCIAndBecomesStale() throws {
        let row = try XCTUnwrap(TrackedPullRequest.active(in: [artifact()]).first)
        XCTAssertEqual(row.checksLabel(at: try XCTUnwrap(row.artifact.observedAt?.apiDate)), "Checks running")
        XCTAssertEqual(row.checksLabel(at: try XCTUnwrap(row.artifact.observedAt?.apiDate).addingTimeInterval(121)), "Status out of date")
    }

    func testRefreshFailureDoesNotClaimChecksPassed() throws {
        var failed = artifact(); failed.checksState = "passed"; failed.refreshError = "Unavailable"
        let row = try XCTUnwrap(TrackedPullRequest.active(in: [failed]).first)
        XCTAssertEqual(row.checksLabel(at: try XCTUnwrap(failed.observedAt?.apiDate)), "Status out of date")
    }

    func testInvalidDestinationsNeverBecomeLinks() {
        for url in ["https://github.com.evil.test/a/b/pull/1", "https://token@github.com/a/b/pull/1", "http://github.com/a/b/pull/1"] {
            XCTAssertTrue(TrackedPullRequest.active(in: [artifact(url: url)]).isEmpty)
        }
    }
    func testFinishedPRsStayVisibleAfterPollingStops() throws {
        var merged = artifact(id: "merged")
        merged.prState = "merged"; merged.checksState = nil
        var closed = artifact(id: "closed", url: "https://github.com/team/repo/pull/43")
        closed.prState = "closed"; closed.checksState = nil
        let rows = TrackedPullRequest.tracked(in: [merged, closed, artifact(id: "open", url: "https://github.com/team/repo/pull/44")])
        XCTAssertEqual(rows.count, 3)
        XCTAssertFalse(try XCTUnwrap(rows.first).isFinished)
        let later = try XCTUnwrap(merged.observedAt?.apiDate).addingTimeInterval(86400)
        XCTAssertEqual(try XCTUnwrap(rows.first { $0.artifact.id == "merged" }).checksLabel(at: later), "Merged")
        XCTAssertEqual(try XCTUnwrap(rows.first { $0.artifact.id == "closed" }).checksLabel(at: later), "Closed")
    }

}
