import XCTest
@testable import Coconut

final class TrackedWorkItemTests: XCTestCase {
    private let recoveryPairing = CheckoutRecoveryPairing(deviceId: "phone", serverURL: "https://original.example:443",
                                                          fingerprint: String(repeating: "a", count: 64))

    private func recoveryPreview(key: String = "same-key") throws -> CheckoutRecoveryPreview {
        let fixture = """
        {"key":"same-key","dirty_count":3,"resume_consent":"fresh","request":{"task_id":"owner","native_task_id":"native","expected_owner":"execution","repository":"/repo","path":"/worktree","saved_branch":"saved","attached_branch":"attached","saved_head":"a","attached_head":"b","recovery_branch":"recovery","message":"Continue"}}
        """
        return try APIJSON.decoder().decode(CheckoutRecoveryPreview.self,
            from: Data(fixture.replacingOccurrences(of: "same-key", with: key).utf8))
    }

    private func roundtripPendingCheckout(_ confirmation: CheckoutRecoveryConfirmation) throws -> CheckoutRecoveryConfirmation {
        let location = FileManager.default.temporaryDirectory.appendingPathComponent("checkout-recovery-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: location) }
        try ProtectedCache.write(confirmation, name: "checkout-recovery.json", in: location)
        return try XCTUnwrap(ProtectedCache.readPendingCheckout(for: confirmation.pairing, in: location))
    }

    private func item(status: String = "working") -> Artifact {
        Artifact(id: "export", botId: "bot", title: "Export account data", kind: "work", status: status,
                 preview: "", createdAt: "2026-09-13T00:00:00Z", activity: "Preparing the export",
                 activityState: status, activityObservedAt: "2026-09-13T00:00:00Z")
    }

    private func owner(status: String, id: String = "owner") -> AgentTask {
        AgentTask(id: id, botId: "bot", sessionId: "session", title: "Export account data",
                  status: status, summary: "", createdAt: "", updatedAt: "", threadId: "thread")
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
        stopped.checkoutRecoveryStopped = true
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")])
            .first?.canOfferCheckoutRecovery == true)
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot").first?.canOfferCheckoutRecovery == true)
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "completed")])
            .first?.canOfferCheckoutRecovery == true)
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled", id: "other")])
            .first?.canOfferCheckoutRecovery == true)
        stopped.taskId = nil
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")])
            .first?.canOfferCheckoutRecovery == true)
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

    func testRecoveryStopMarkerDecodesAndMissingOrFalseFailsClosed() throws {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"; stopped.workState = "cancelled"
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(stopped)) as? [String: Any])
        func decoded() throws -> Artifact {
            try APIJSON.decoder().decode(Artifact.self, from: JSONSerialization.data(withJSONObject: json))
        }
        let cancelledOwner = [owner(status: "cancelled")]
        json["checkout_recovery_stopped"] = true
        let explicitlyStopped = try decoded()
        XCTAssertEqual(explicitlyStopped.checkoutRecoveryStopped, true)
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [explicitlyStopped], botId: "bot", tasks: cancelledOwner)
            .first?.canOfferCheckoutRecovery == true)
        json["checkout_recovery_stopped"] = false
        let taskOnlyStop = try decoded()
        XCTAssertEqual(taskOnlyStop.checkoutRecoveryStopped, false)
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [taskOnlyStop], botId: "bot", tasks: cancelledOwner)
            .first?.canOfferCheckoutRecovery == true)
        json.removeValue(forKey: "checkout_recovery_stopped")
        let olderServer = try decoded()
        XCTAssertNil(olderServer.checkoutRecoveryStopped)
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [olderServer], botId: "bot", tasks: cancelledOwner)
            .first?.canOfferCheckoutRecovery == true)
    }

    func testLinkedPullRequestRecoversCanonicalStoppedWorkNotPRTask() {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "original-owner"; stopped.workState = "cancelled"
        stopped.checkoutRecoveryStopped = true
        var pr = stopped
        pr.id = "pr"; pr.kind = "pr"; pr.botId = "reviewer"; pr.taskId = "review-task"
        pr.checkoutRecoveryStopped = nil
        pr.nativeTaskId = nil; pr.workOutcomeId = stopped.id
        pr.url = "https://github.com/example/project/pull/42"
        let row = TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot",
                                              tasks: [owner(status: "cancelled", id: "original-owner"),
                                                      owner(status: "completed", id: "review-task")]).first
        XCTAssertEqual(row?.artifact.id, "pr")
        XCTAssertTrue(row?.canOfferCheckoutRecovery == true)
        XCTAssertEqual(row?.checkoutRecoveryWork?.id, stopped.id)
        XCTAssertEqual(row?.checkoutRecoveryWork?.taskId, "original-owner")
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot",
            tasks: [owner(status: "completed", id: "original-owner"), owner(status: "cancelled", id: "review-task")])
            .first?.canOfferCheckoutRecovery == true)
        XCTAssertFalse(TrackedWorkItem.tracked(in: [pr]).first?.canOfferCheckoutRecovery == true)
    }

    func testNativeDetailsOnlyIncludesRecoverableDismissedStoppedWork() throws {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"
        stopped.workState = "cancelled"; stopped.workDismissalId = "dismissal"
        stopped.checkoutRecoveryStopped = true
        XCTAssertTrue(TrackedWorkItem.tracked(in: [stopped]).isEmpty)
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")]).isEmpty)
        let details = TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")],
                                                  includeDismissed: true)
        XCTAssertEqual(details.map(\.artifact.id), ["export"])
        XCTAssertTrue(details[0].canOfferCheckoutRecovery)
        XCTAssertEqual(details[0].checkoutRecoveryWork?.id, "export")
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "completed")],
                                                   includeDismissed: true).isEmpty)
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", includeDismissed: true).isEmpty)
        stopped.workState = "completed"
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")],
                                                   includeDismissed: true).isEmpty)
    }

    func testDismissedStoppedWorkWithLinkedDismissedPRRemainsInDetails() {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"
        stopped.workState = "cancelled"; stopped.workDismissalId = "dismissal"
        stopped.checkoutRecoveryStopped = true
        var pr = stopped
        pr.id = "pr"; pr.kind = "pr"; pr.nativeTaskId = nil
        pr.checkoutRecoveryStopped = nil
        pr.workOutcomeId = stopped.id; pr.url = "https://github.com/example/project/pull/42"
        XCTAssertTrue(TrackedWorkItem.tracked(in: [stopped, pr]).isEmpty)
        let details = TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot", tasks: [owner(status: "cancelled")],
                                                  includeDismissed: true)
        XCTAssertEqual(details.map(\.artifact.id), ["export"])
        XCTAssertTrue(details[0].canOfferCheckoutRecovery)
    }

    func testFreshStopUsesCanonicalPersistedWorkAndDoesNotReplaceRecoveryConfirmation() throws {
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        let restored = try roundtripPendingCheckout(confirmation)
        let requestBytes = try APIJSON.encode(restored.request)
        let first = try XCTUnwrap(CheckoutRecoveryStop(confirmation: restored))
        let second = try XCTUnwrap(CheckoutRecoveryStop(confirmation: restored))
        XCTAssertEqual(first.path, "work/items/export/cancel")
        XCTAssertEqual(second.path, first.path)
        XCTAssertNotEqual(first.key, second.key)
        XCTAssertNotEqual(first.key, restored.key)
        XCTAssertEqual(restored.workItemId, "export")
        XCTAssertEqual(restored.key, confirmation.key)
        XCTAssertEqual(try APIJSON.encode(restored.request), requestBytes)
        XCTAssertFalse(String(decoding: requestBytes, as: UTF8.self).contains("work_item_id"))
        var saved = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(restored)) as? [String: Any])
        saved.removeValue(forKey: "workItemId")
        let legacy = try JSONDecoder().decode(CheckoutRecoveryConfirmation.self,
                                              from: JSONSerialization.data(withJSONObject: saved))
        XCTAssertEqual(legacy.key, restored.key)
        XCTAssertEqual(legacy.request, restored.request)
        XCTAssertNil(CheckoutRecoveryStop(confirmation: legacy))
    }

    func testPendingStopOnlyTargetsVerifiedActiveSuccessorAfterAdmission() throws {
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        let stop = try XCTUnwrap(CheckoutRecoveryStop(confirmation: try roundtripPendingCheckout(confirmation)))
        var original = item(status: "cancelled")
        original.taskId = "owner"; original.nativeTaskId = "native-export"; original.workState = "cancelled"
        XCTAssertNil(stop.admittedSuccessor(in: [original], tasks: [owner(status: "cancelled")]))
        var resumed = original
        resumed.taskId = "successor"; resumed.workState = "cancelled"
        let running = owner(status: "queued", id: "successor")
        XCTAssertEqual(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"), running])?.id,
                       "successor")
        XCTAssertNil(stop.admittedSuccessor(in: [], tasks: [owner(status: "cancelled"), running]))
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [running]))
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "completed"), running]))
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"),
                                                          owner(status: "completed", id: "successor")]))
        var foreign = running
        foreign.botId = "other"
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"), foreign]))
        resumed.workOutcomeId = "another"
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"), running]))
    }

    func testMissingCanonicalWorkCannotOfferRecoveryFromAlias() {
        var alias = item(status: "cancelled")
        alias.id = "alias"; alias.workOutcomeId = "export"
        alias.taskId = "owner"; alias.nativeTaskId = "native-export"; alias.workState = "cancelled"
        XCTAssertFalse(TrackedWorkItem.nativeTasks(in: [alias], botId: "bot", tasks: [owner(status: "cancelled")])
            .first?.canOfferCheckoutRecovery == true)
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [alias], botId: "bot", tasks: [owner(status: "cancelled")],
                                                   includeDismissed: true).first?.canOfferCheckoutRecovery != true)
    }

    @MainActor
    func testUncertainConfirmationRetainsExactBodyAndKeyForRetry() throws {
        let preview = try recoveryPreview()
        let store = AppStore(demo: true)
        store.checkoutConfirmation = CheckoutRecoveryConfirmation(preview: preview, pairing: recoveryPairing)
        let initialBody = try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request)
        let transport = CheckoutRecoveryConfirmationResult.classify(error: URLError(.timedOut))
        guard case .uncertain = transport else { return XCTFail("Transport must remain uncertain") }
        store.recordCheckoutRecoveryResult(transport, for: XCTUnwrap(store.checkoutConfirmation))
        XCTAssertTrue(store.checkoutConfirmation?.isUncertain == true)
        XCTAssertEqual(store.checkoutConfirmation?.key, "same-key")
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request), initialBody)
        store.recordCheckoutRecoveryResult(.classify(error: ServiceError.response(503, "Unavailable")),
                                           for: XCTUnwrap(store.checkoutConfirmation))
        XCTAssertEqual(store.checkoutConfirmation?.key, preview.key)
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request), initialBody)
        XCTAssertEqual(CheckoutRecoveryConfirmationResult.classify(error: ServiceError.invalidResponse),
                       .uncertain("The Mac returned an unreadable response."))
        XCTAssertEqual(CheckoutRecoveryConfirmationResult.classify(error: ServiceError.response(408, "Request timed out.")),
                       .uncertain("Request timed out."))
        XCTAssertEqual(CheckoutRecoveryConfirmationResult.classify(receipt: CheckoutRecoveryReceipt(state: "unexpected", error: nil)),
                       .uncertain("The Mac returned an unrecognized recovery state. Retry the same confirmation to check it."))
        XCTAssertTrue(String(decoding: initialBody, as: UTF8.self).contains("\"resume_confirmed\":true"))
        XCTAssertTrue(String(decoding: initialBody, as: UTF8.self).contains("\"resume_consent\":\"fresh\""))
    }

    @MainActor
    func testHeldReceiptShowsReasonAndReleasesConfirmationForFreshPreview() throws {
        let receipt = try APIJSON.decoder().decode(CheckoutRecoveryReceipt.self,
            from: Data(#"{"state":"held","error":"The Work stop changed before admission."}"#.utf8))
        let result = CheckoutRecoveryConfirmationResult.classify(receipt: receipt)
        XCTAssertEqual(result, .rejected("The Work stop changed before admission."))
        XCTAssertEqual(result.failureReason, "The Work stop changed before admission.")
        let store = AppStore(demo: true)
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        store.checkoutConfirmation = confirmation
        store.recordCheckoutRecoveryResult(result, for: confirmation)
        XCTAssertNil(store.checkoutConfirmation)
    }

    @MainActor
    func testExplicit409ReleasesStaleConfirmationForFreshPreview() throws {
        let result = CheckoutRecoveryConfirmationResult.classify(error: ServiceError.response(409, "Stop changed."))
        XCTAssertEqual(result, .rejected("Stop changed."))
        XCTAssertEqual(CheckoutRecoveryConfirmationResult.classify(error: ServiceError.response(401, "Pair again.")),
                       .rejected("Pair again."))
        let store = AppStore(demo: true)
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        store.checkoutConfirmation = confirmation
        store.recordCheckoutRecoveryResult(result, for: confirmation)
        XCTAssertNil(store.checkoutConfirmation)
    }

    @MainActor
    func testDrainingThenHeldAllowsFreshStopPreview() throws {
        let store = AppStore(demo: true)
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        store.checkoutConfirmation = confirmation
        for state in [CheckoutRecoveryAcceptedState.accepted, .draining] {
            store.recordCheckoutRecoveryResult(.classify(receipt: CheckoutRecoveryReceipt(state: state.rawValue, error: nil)),
                                               for: confirmation)
            XCTAssertTrue(store.checkoutConfirmation?.matches(confirmation) == true)
        }
        let restored = try roundtripPendingCheckout(XCTUnwrap(store.checkoutConfirmation))
        XCTAssertEqual(CheckoutRecoveryStop(confirmation: restored)?.path, "work/items/export/cancel")
        store.checkoutConfirmation = restored
        let held = CheckoutRecoveryConfirmationResult.classify(receipt: CheckoutRecoveryReceipt(state: "held", error: "Stop changed."))
        store.recordCheckoutRecoveryResult(held, for: restored)
        XCTAssertEqual(held, .rejected("Stop changed."))
        XCTAssertNil(store.checkoutConfirmation)
        let fresh = CheckoutRecoveryConfirmation(preview: try recoveryPreview(key: "new-stop-key"), pairing: recoveryPairing)
        store.checkoutConfirmation = fresh
        XCTAssertEqual(store.checkoutConfirmation?.key, "new-stop-key")
    }

    @MainActor
    func testDrainingThenAppliedAllowsFreshStopPreview() throws {
        let store = AppStore(demo: true)
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        store.checkoutConfirmation = confirmation
        store.recordCheckoutRecoveryResult(.classify(receipt: CheckoutRecoveryReceipt(state: "draining", error: nil)),
                                           for: confirmation)
        XCTAssertEqual(store.checkoutConfirmation?.key, confirmation.key)
        let restored = try roundtripPendingCheckout(XCTUnwrap(store.checkoutConfirmation))
        XCTAssertEqual(CheckoutRecoveryStop(confirmation: restored)?.path, "work/items/export/cancel")
        store.checkoutConfirmation = restored
        store.recordCheckoutRecoveryResult(.classify(receipt: CheckoutRecoveryReceipt(state: "applied", error: nil)),
                                           for: restored)
        XCTAssertNil(store.checkoutConfirmation)
        let fresh = CheckoutRecoveryConfirmation(preview: try recoveryPreview(key: "later-stop-key"), pairing: recoveryPairing)
        store.checkoutConfirmation = fresh
        XCTAssertEqual(store.checkoutConfirmation?.key, "later-stop-key")
    }

    @MainActor
    func testLostReplyRestoresExactRequestAndPairingFromPrivateCache() throws {
        let location = FileManager.default.temporaryDirectory.appendingPathComponent("checkout-recovery-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: location) }
        let original = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                    workItemId: "export")
        let requestBytes = try APIJSON.encode(original.request)
        try ProtectedCache.write(original, name: "checkout-recovery.json", in: location)
        let persisted = try Data(contentsOf: location.appendingPathComponent("checkout-recovery.json"))
        XCTAssertFalse(String(decoding: persisted, as: UTF8.self).contains("token"))
        let restored = try XCTUnwrap(ProtectedCache.readPendingCheckout(for: recoveryPairing, in: location))
        XCTAssertEqual(restored.key, original.key)
        XCTAssertEqual(restored.pairing, original.pairing)
        XCTAssertEqual(restored.workItemId, original.workItemId)
        XCTAssertEqual(try APIJSON.encode(restored.request), requestBytes)
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(
            deviceId: "re-paired", serverURL: recoveryPairing.serverURL, fingerprint: recoveryPairing.fingerprint), in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, serverURL: "https://different.example", fingerprint: recoveryPairing.fingerprint), in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, serverURL: recoveryPairing.serverURL,
            fingerprint: String(repeating: "b", count: 64)), in: location))
        let relaunchedStore = AppStore(demo: true)
        relaunchedStore.checkoutConfirmation = restored
        relaunchedStore.recordCheckoutRecoveryResult(.classify(error: URLError(.networkConnectionLost)), for: restored)
        XCTAssertEqual(relaunchedStore.checkoutConfirmation?.key, original.key)
        XCTAssertEqual(relaunchedStore.checkoutConfirmation?.workItemId, original.workItemId)
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(relaunchedStore.checkoutConfirmation).request), requestBytes)
    }

    @MainActor
    func testRetainedConfirmationCannotTargetDifferentPairing() async throws {
        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing)
        let newHost = CheckoutRecoveryPairing(deviceId: "new-phone", serverURL: "https://new.example:443",
                                              fingerprint: String(repeating: "b", count: 64))
        XCTAssertTrue(confirmation.matches(pairing: recoveryPairing))
        XCTAssertFalse(confirmation.matches(pairing: newHost))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, serverURL: newHost.serverURL, fingerprint: recoveryPairing.fingerprint)))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, serverURL: recoveryPairing.serverURL, fingerprint: newHost.fingerprint)))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: newHost.deviceId, serverURL: recoveryPairing.serverURL, fingerprint: recoveryPairing.fingerprint)))
        let store = AppStore(demo: true)
        store.checkoutConfirmation = confirmation
        let result = await store.confirmCheckoutRecovery(confirmation)
        XCTAssertEqual(result, .rejected("The pairing changed. Check the task again."))
        XCTAssertEqual(store.checkoutConfirmation?.key, confirmation.key)
        store.recordCheckoutRecoveryResult(result, for: confirmation)
        XCTAssertNil(store.checkoutConfirmation)
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
