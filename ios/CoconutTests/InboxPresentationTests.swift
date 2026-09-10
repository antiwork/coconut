import XCTest
@testable import Coconut

final class InboxPresentationTests: XCTestCase {
    func testTasksWithoutPendingDecisionsDoNotCreateInboxRows() {
        let statuses = ["working", "running", "queued", "waiting", "needs_you", "scheduled",
                        "cancelling", "completed", "failed", "cancelled"]
        let tasks = statuses.map { task($0, threadID: "thread-\($0)", status: $0) }

        XCTAssertTrue(InboxPresentation.threads(decisions: [], tasks: tasks).isEmpty)
    }

    func testResolvedDecisionsDoNotCreateInboxRows() {
        let statuses = ["approved", "rejected", "modified", "executed", "failed", "cancelled", "expired"]
        let decisions = statuses.map { decision($0, status: $0) }

        XCTAssertTrue(InboxPresentation.threads(decisions: decisions, tasks: [task("task")]).isEmpty)
    }

    func testMultiplePendingDecisionsInOneTaskCreateOneRow() {
        let older = decision("older", createdAt: "2026-09-08T09:00:00Z")
        let newer = decision("newer", createdAt: "2026-09-08T10:00:00Z")

        let rows = InboxPresentation.threads(decisions: [newer, older], tasks: [task("task")])

        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.id, "thread")
        XCTAssertEqual(rows.first?.decision, older)
        XCTAssertEqual(rows.first?.decisions, [older, newer])
    }

    func testDifferentTasksInOneThreadShareOneRow() {
        let first = decision("first", taskID: "first-task", createdAt: "2026-09-08T09:00:00Z")
        let second = decision("second", taskID: "second-task", createdAt: "2026-09-08T10:00:00Z")
        let tasks = [task("first-task"), task("second-task")]

        let rows = InboxPresentation.threads(decisions: [second, first], tasks: tasks)

        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.id, "thread")
        XCTAssertEqual(rows.first?.decisions, [first, second])
    }

    func testDifferentThreadsKeepSeparateRows() {
        let first = decision("first", taskID: "first-task", createdAt: "2026-09-08T09:00:00Z")
        let second = decision("second", taskID: "second-task", createdAt: "2026-09-08T10:00:00Z")
        let tasks = [task("first-task", threadID: "first-thread"),
                     task("second-task", threadID: "second-thread")]

        let rows = InboxPresentation.threads(decisions: [second, first], tasks: tasks)

        XCTAssertEqual(rows.map(\.id), ["first-thread", "second-thread"])
        XCTAssertEqual(rows.map(\.decisions), [[first], [second]])
    }

    func testResolvingOneDecisionKeepsOtherPendingDecisionsInItsThread() {
        var first = decision("first", urgency: "high")
        var second = decision("second")
        let tasks = [task("task")]
        let initial = InboxPresentation.threads(decisions: [first, second], tasks: tasks)

        first.status = "executed"
        let afterFirst = InboxPresentation.threads(decisions: [first, second], tasks: tasks)

        XCTAssertEqual(afterFirst.map(\.id), initial.map(\.id))
        XCTAssertEqual(afterFirst.first?.decision, second)
        XCTAssertEqual(afterFirst.first?.decisions, [second])

        second.status = "rejected"
        XCTAssertTrue(InboxPresentation.threads(decisions: [first, second], tasks: tasks).isEmpty)
    }

    func testMissingTaskKeepsItsPendingDecision() {
        let orphan = decision("orphan", taskID: "missing-task")

        let rows = InboxPresentation.threads(decisions: [orphan], tasks: [task("unrelated-task")])

        XCTAssertEqual(rows.map(\.id), ["missing-task"])
        XCTAssertEqual(rows.first?.decision, orphan)
        XCTAssertEqual(rows.first?.decisions, [orphan])
    }

    func testPendingDecisionRemainsVisibleWhenTaskStatusIsStale() {
        let pending = decision("pending")

        for status in ["running", "completed", "failed", "cancelled"] {
            let rows = InboxPresentation.threads(decisions: [pending], tasks: [task("task", status: status)])

            XCTAssertEqual(rows.first?.decision, pending, status)
        }
    }

    func testPrimaryDecisionUsesUrgencyThenAgeThenID() {
        let low = decision("low", urgency: "low", createdAt: "2026-09-08T01:00:00Z")
        let normal = decision("normal", createdAt: "2026-09-08T02:00:00Z")
        let highNew = decision("high-new", urgency: "high", createdAt: "2026-09-08T11:00:00Z")
        let highOldB = decision("high-old-b", urgency: "high", createdAt: "2026-09-08T10:00:00Z")
        let highOldA = decision("high-old-a", urgency: "high", createdAt: "2026-09-08T10:00:00Z")
        let decisions = [low, highNew, highOldB, normal, highOldA]

        let rows = InboxPresentation.threads(decisions: decisions, tasks: [task("task")])

        XCTAssertEqual(rows.first?.decision, highOldA)
        XCTAssertEqual(rows.first?.decisions, [highOldA, highOldB, highNew, normal, low])
    }

    func testThreadOrderUsesUrgencyThenPrimaryDecisionAge() {
        let low = decision("low", taskID: "low-task", urgency: "low", createdAt: "2026-09-08T01:00:00Z")
        let normal = decision("normal", taskID: "normal-task", createdAt: "2026-09-08T02:00:00Z")
        let highNew = decision("high-new", taskID: "new-task", urgency: "high", createdAt: "2026-09-08T11:00:00Z")
        let highOld = decision("high-old", taskID: "old-task", urgency: "high", createdAt: "2026-09-08T10:00:00Z")
        let oldLow = decision("old-low", taskID: "new-task", urgency: "low", createdAt: "2026-09-08T00:00:00Z")
        let tasks = [task("low-task", threadID: "low-thread"), task("normal-task", threadID: "normal-thread"),
                     task("new-task", threadID: "new-thread"), task("old-task", threadID: "old-thread")]

        let rows = InboxPresentation.threads(decisions: [low, highNew, oldLow, normal, highOld], tasks: tasks)

        XCTAssertEqual(rows.map(\.id), ["old-thread", "new-thread", "normal-thread", "low-thread"])
        XCTAssertEqual(rows.map(\.decision), [highOld, highNew, normal, low])
    }

    func testTiedRowsAndDecisionsKeepTheirOrderWhenInputOrderChanges() {
        let decisions = [decision("decision-z", taskID: "task-a"),
                         decision("decision-a", taskID: "task-z"),
                         decision("decision-b", taskID: "task-z")]
        let tasks = [task("task-a", threadID: "thread-a"), task("task-z", threadID: "thread-z")]
        let forward = InboxPresentation.threads(decisions: decisions, tasks: tasks)
        let reversed = InboxPresentation.threads(decisions: decisions.reversed(), tasks: tasks.reversed())

        XCTAssertEqual(forward.map(\.id), reversed.map(\.id))
        XCTAssertEqual(forward.map(\.decision), reversed.map(\.decision))
        XCTAssertEqual(forward.map(\.decisions), reversed.map(\.decisions))
        XCTAssertEqual(forward.first(where: { $0.id == "thread-z" })?.decision.id, "decision-a")
    }

    private func task(_ id: String, threadID: String = "thread", status: String = "needs_you") -> AgentTask {
        AgentTask(id: id, botId: "bot", sessionId: "session", title: "Review the result", status: status, summary: "Review the proposed file.", createdAt: "2026-09-08T08:00:00Z",
                  updatedAt: "2026-09-08T12:00:00Z", scheduledAt: nil, threadId: threadID)
    }

    private func decision(_ id: String, taskID: String = "task", status: String = "pending",
                          urgency: String = "normal", createdAt: String = "2026-09-08T12:00:00Z") -> Decision {
        Decision(id: id, botId: "bot", taskId: taskID, title: "Create the report?", explanation: "The report is ready.",
                 actionType: "file.create", resource: "report.txt", consequences: "Creates one file.",
                 recommended: "approve", options: [], status: status, urgency: urgency, createdAt: createdAt,
                 decidedAt: nil, decidedBy: nil, correction: nil, outcome: nil, revision: 1)
    }
}
