import XCTest
@testable import Coconut

final class TrackedWorkItemTests: XCTestCase {
    private func item(status: String = "working") -> Artifact {
        Artifact(id: "export", botId: "bot", title: "Export account data", kind: "work", status: status,
                 preview: "", createdAt: "2026-09-13T00:00:00Z", activity: "Preparing the export",
                 activityState: status, activityObservedAt: "2026-09-13T00:00:00Z")
    }

    func testNativeTaskDetailsKeepLinksReportedByAnotherBot() {
        var native = item()
        native.nativeTaskId = "native-export"
        var pr = native
        pr.id = "pr"; pr.botId = "reviewer"; pr.kind = "pr"
        pr.nativeTaskId = nil; pr.workOutcomeId = native.id
        pr.url = "https://github.com/example/project/pull/42"
        var legacy = item(); legacy.id = "legacy"
        let rows = TrackedWorkItem.nativeTasks(in: [native, pr, legacy], botId: "bot")
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.pullRequest?.number, "42")
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [native, pr, legacy], botId: "unrelated").isEmpty)
    }

    func testNativeTaskDetailsIncludeDelegatedWorkInItsOriginalConversation() {
        var native = item(); native.nativeTaskId = "native-export"; native.taskId = "owner"
        let owner = AgentTask(id: "owner", botId: "bot", sessionId: "session", title: "Export account data",
                              status: "working", summary: "", createdAt: "", updatedAt: "", threadId: "thread",
                              conversationBotId: "desk", sourceBotId: "desk")
        XCTAssertEqual(TrackedWorkItem.nativeTasks(in: [native], botId: "desk", tasks: [owner]).count, 1)
        XCTAssertEqual(TrackedWorkItem.nativeTasks(in: [native], botId: "bot", tasks: [owner]).count, 1)
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [native], botId: "unrelated", tasks: [owner]).isEmpty)
    }

    func testStoppedNativeTaskOffersRecoveryOnlyWithOwnerAndExplicitConfirmation() throws {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"; stopped.workState = "cancelled"
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot").first?.canOfferCheckoutRecovery == true)
        stopped.taskId = nil
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot").first?.canOfferCheckoutRecovery == true)
        let json = """
        {"key":"123","dirty_count":3,"resume_consent":"fresh","request":{"task_id":"owner","native_task_id":"native","expected_owner":"execution","repository":"/repo","path":"/worktree","saved_branch":"saved","attached_branch":"attached","saved_head":"a","attached_head":"b","recovery_branch":"recovery","message":"Continue"}}
        """
        let preview = try APIJSON.decoder().decode(CheckoutRecoveryPreview.self, from: Data(json.utf8))
        let unconfirmed = try APIJSON.encode(preview.request)
        XCTAssertFalse(String(decoding: unconfirmed, as: UTF8.self).contains("resume_confirmed"))
        var confirmed = preview.request
        confirmed.resumeConsent = preview.resumeConsent; confirmed.resumeConfirmed = true
        let body = try APIJSON.encode(confirmed)
        XCTAssertTrue(String(decoding: body, as: UTF8.self).contains("\"resume_confirmed\":true"))
        XCTAssertTrue(String(decoding: body, as: UTF8.self).contains("\"resume_consent\":\"fresh\""))
    }

    func testLinkedPullRequestRecoversCanonicalStoppedWorkNotPRTask() {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "original-owner"; stopped.workState = "cancelled"
        var pr = stopped
        pr.id = "pr"; pr.kind = "pr"; pr.botId = "reviewer"; pr.taskId = "review-task"
        pr.nativeTaskId = nil; pr.workOutcomeId = stopped.id
        pr.url = "https://github.com/example/project/pull/42"
        let row = TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot").first
        XCTAssertEqual(row?.artifact.id, "pr")
        XCTAssertTrue(row?.canOfferCheckoutRecovery == true)
        XCTAssertEqual(row?.checkoutRecoveryWork?.id, stopped.id)
        XCTAssertEqual(row?.checkoutRecoveryWork?.taskId, "original-owner")
        XCTAssertFalse(TrackedWorkItem.tracked(in: [pr]).first?.canOfferCheckoutRecovery == true)
    }

    @MainActor
    func testUncertainConfirmationRetainsExactBodyAndKeyForRetry() throws {
        let fixture = """
        {"key":"same-key","dirty_count":3,"resume_consent":"fresh","request":{"task_id":"owner","native_task_id":"native","expected_owner":"execution","repository":"/repo","path":"/worktree","saved_branch":"saved","attached_branch":"attached","saved_head":"a","attached_head":"b","recovery_branch":"recovery","message":"Continue"}}
        """
        let preview = try APIJSON.decoder().decode(CheckoutRecoveryPreview.self, from: Data(fixture.utf8))
        let store = AppStore(demo: true)
        store.checkoutConfirmation = CheckoutRecoveryConfirmation(preview: preview)
        let initialBody = try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request)
        store.checkoutConfirmation?.markUncertain()
        XCTAssertTrue(store.checkoutConfirmation?.isUncertain == true)
        XCTAssertEqual(store.checkoutConfirmation?.key, "same-key")
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request), initialBody)
        store.checkoutConfirmation?.markUncertain()
        XCTAssertEqual(store.checkoutConfirmation?.key, preview.key)
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request), initialBody)
        XCTAssertTrue(String(decoding: initialBody, as: UTF8.self).contains("\"resume_confirmed\":true"))
        XCTAssertTrue(String(decoding: initialBody, as: UTF8.self).contains("\"resume_consent\":\"fresh\""))
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

    func testAliasUsesOneRowWithOrWithoutItsPullRequest() throws {
        let work = item()
        var alias = work
        alias.id = "alias"
        alias.workOutcomeId = work.id
        alias.title = "Old coordination report"
        let alone = TrackedWorkItem.tracked(in: [alias, work])
        XCTAssertEqual(alone.count, 1)
        XCTAssertEqual(alone.first?.artifact.id, work.id)
        XCTAssertEqual(TrackedWorkItem.tracked(in: [alias]).first?.id, "work:export")
        var pr = work
        pr.id = "pr"
        pr.kind = "pr"
        pr.url = "https://github.com/example/project/pull/42"
        pr.workOutcomeId = work.id
        let linked = TrackedWorkItem.tracked(in: [alias, pr, work])
        XCTAssertEqual(linked.count, 1)
        XCTAssertEqual(linked.first?.pullRequest?.number, "42")
        XCTAssertTrue(TrackedWorkItem.tracked(in: [alias, pr, work], visibility: ["work:export": true]).isEmpty)
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
