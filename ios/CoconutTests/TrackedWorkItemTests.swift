import XCTest
@testable import Coconut

final class TrackedWorkItemTests: XCTestCase {
    private let recoveryPairing = CheckoutRecoveryPairing(deviceId: "phone", route: "https://original.example:443",
                                                          macKey: String(repeating: "a", count: 64))

    private func recoveryPreview(key: String = "same-key", owner: String = "execution") throws -> CheckoutRecoveryPreview {
        let fixture = """
        {"key":"same-key","dirty_count":3,"resume_consent":"fresh","request":{"task_id":"owner","native_task_id":"native","expected_owner":"execution","repository":"/repo","path":"/worktree","saved_branch":"saved","attached_branch":"attached","saved_head":"a","attached_head":"b","recovery_branch":"recovery","message":"Continue"}}
        """
        return try APIJSON.decoder().decode(CheckoutRecoveryPreview.self,
            from: Data(fixture.replacingOccurrences(of: "same-key", with: key)
                .replacingOccurrences(of: "\"expected_owner\":\"execution\"", with: "\"expected_owner\":\"\(owner)\"").utf8))
    }

    private func roundtripPendingCheckout(_ confirmation: CheckoutRecoveryConfirmation) throws -> CheckoutRecoveryConfirmation {
        let location = FileManager.default.temporaryDirectory.appendingPathComponent("checkout-recovery-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: location) }
        try ProtectedCache.write(confirmation, name: "checkout-recovery.json", in: location)
        return try XCTUnwrap(ProtectedCache.readPendingCheckout(for: confirmation.pairing, in: location))
    }

    func testCheckoutConfirmationSummaryIdentifiesPreviewWithoutOwnerUUID() throws {
        let ownerId = "fa0a6cbc-1111-2222-3333-444444444444"
        let preview = try recoveryPreview(owner: ownerId)
        let summary = preview.confirmationSummary
        for detail in ["/repo", "/worktree", "saved", "attached", "(a)", "(b)", "Tracked edits: 3"] {
            XCTAssertTrue(summary.contains(detail), detail)
        }
        XCTAssertFalse(summary.contains(ownerId))
        XCTAssertFalse(summary.contains(preview.key))
    }

    @MainActor
    func testRecoveryUncertaintyPersistsWithExactConfirmationAndKnownResultsClearIt() throws {
        let location = FileManager.default.temporaryDirectory.appendingPathComponent("checkout-recovery-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: location) }
        let original = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                    workItemId: "export")
        try ProtectedCache.write(original, name: "checkout-recovery.json", in: location)
        let originalBody = try APIJSON.encode(original.request)
        let store = AppStore(demo: true)
        store.checkoutConfirmation = original
        XCTAssertNil(store.recordCheckoutRecoveryResult(.uncertain("Network timed out"), for: original, in: location))
        let restored = try XCTUnwrap(ProtectedCache.readPendingCheckout(for: recoveryPairing, in: location))
        XCTAssertTrue(restored.isUncertain)
        XCTAssertEqual(restored.key, original.key)
        XCTAssertEqual(restored.workItemId, original.workItemId)
        XCTAssertEqual(restored.pairing, original.pairing)
        XCTAssertEqual(try APIJSON.encode(restored.request), originalBody)
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(deviceId: "other",
            route: recoveryPairing.route, macKey: recoveryPairing.macKey), in: location))

        let relaunched = AppStore(demo: true)
        relaunched.checkoutConfirmation = restored
        XCTAssertNil(relaunched.recordCheckoutRecoveryResult(.accepted(.draining), for: restored, in: location))
        let draining = try XCTUnwrap(ProtectedCache.readPendingCheckout(for: recoveryPairing, in: location))
        XCTAssertFalse(draining.isUncertain)
        XCTAssertEqual(draining.key, original.key)
        XCTAssertEqual(try APIJSON.encode(draining.request), originalBody)
        XCTAssertNil(relaunched.recordCheckoutRecoveryResult(.accepted(.applied), for: draining, in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: recoveryPairing, in: location))

        try ProtectedCache.write(original, name: "checkout-recovery.json", in: location)
        relaunched.checkoutConfirmation = original
        XCTAssertNil(relaunched.recordCheckoutRecoveryResult(.rejected("Held"), for: original, in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: recoveryPairing, in: location))
    }

    @MainActor
    func testRecoveryUncertaintyWriteFailureKeepsOriginalKey() throws {
        let location = FileManager.default.temporaryDirectory.appendingPathComponent("checkout-recovery-file-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: location) }
        try Data("not a directory".utf8).write(to: location)
        let original = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing)
        let store = AppStore(demo: true)
        store.checkoutConfirmation = original
        let warning = store.recordCheckoutRecoveryResult(.uncertain("Timed out"), for: original, in: location)
        XCTAssertNotNil(warning)
        XCTAssertTrue(warning?.contains("could not save") == true)
        XCTAssertEqual(store.checkoutConfirmation?.key, original.key)
        XCTAssertEqual(store.checkoutConfirmation?.request, original.request)
        XCTAssertTrue(store.checkoutConfirmation?.isUncertain == true)
    }

    @MainActor
    func testRequestNotSentRetainsOriginalConfirmationWithoutUncertainty() throws {
        let original = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing)
        let store = AppStore(demo: true)
        store.checkoutConfirmation = original
        let result: CheckoutRecoveryConfirmationResult = .notSent("The phone could not save the warning. No request was sent.")
        XCTAssertEqual(result.failureReason, "The phone could not save the warning. No request was sent.")
        XCTAssertNil(store.recordCheckoutRecoveryResult(result, for: original))
        XCTAssertEqual(store.checkoutConfirmation?.key, original.key)
        XCTAssertEqual(store.checkoutConfirmation?.request, original.request)
        XCTAssertFalse(store.checkoutConfirmation?.isUncertain == true)
    }

    func testOnlyUncertainConfirmationErrorAllowsAlertRecoveryRetry() throws {
        var saved = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                 workItemId: "export")
        saved.markUncertain()
        let confirmationError = try XCTUnwrap(NativeTaskDetailsError(result: .uncertain("Timed out"),
                                                                    storageWarning: nil))
        XCTAssertEqual(confirmationError.origin, .confirmation)
        XCTAssertTrue(confirmationError.allowsRecoveryRetry(with: saved))

        for origin in [NativeTaskDetailsError.Origin.stopRecovery, .stopTask, .preview, .storage] {
            let otherError = NativeTaskDetailsError(origin: origin, message: "Timed out")
            XCTAssertFalse(otherError.allowsRecoveryRetry(with: saved), "\(origin) must not retry recovery")
        }
        let storageError = try XCTUnwrap(NativeTaskDetailsError(result: .accepted(.draining),
                                                               storageWarning: "Could not save status"))
        XCTAssertEqual(storageError.origin, .storage)
        XCTAssertFalse(storageError.allowsRecoveryRetry(with: saved))
        XCTAssertNil(NativeTaskDetailsError(result: .accepted(.applied), storageWarning: nil))

        saved.clearUncertain()
        XCTAssertFalse(confirmationError.allowsRecoveryRetry(with: saved))
        XCTAssertFalse(confirmationError.allowsRecoveryRetry(with: nil))
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

    func testDismissedTaskOnlyStopStaysHiddenWithoutRecoveryProvenance() {
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"
        stopped.workState = "cancelled"; stopped.workDismissalId = "dismissal"
        for provenance in [false, nil] as [Bool?] {
            stopped.checkoutRecoveryStopped = provenance
            XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [owner(status: "cancelled")],
                                                       includeDismissed: true).isEmpty)
        }
        stopped.checkoutRecoveryStopped = true
        stopped.workOutcomeId = "other"
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
        for provenance in [false, nil] as [Bool?] {
            stopped.checkoutRecoveryStopped = provenance
            XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot", tasks: [owner(status: "cancelled")],
                                                       includeDismissed: true).isEmpty)
        }
        stopped.checkoutRecoveryStopped = true
        XCTAssertTrue(TrackedWorkItem.nativeTasks(in: [stopped, pr], botId: "bot", tasks: [owner(status: "completed")],
                                                   includeDismissed: true).isEmpty)
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
        XCTAssertEqual(restored.targetSummary,
                       "Work: native\nRepository: /repo\nWorktree: /worktree\nSaved branch: saved\nAttached branch: attached")
        XCTAssertEqual(try APIJSON.encode(restored.request), requestBytes)
        XCTAssertFalse(String(decoding: requestBytes, as: UTF8.self).contains("work_item_id"))
        var saved = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(restored)) as? [String: Any])
        saved.removeValue(forKey: "workItemId")
        saved.removeValue(forKey: "isUncertain")
        let legacy = try JSONDecoder().decode(CheckoutRecoveryConfirmation.self,
                                              from: JSONSerialization.data(withJSONObject: saved))
        XCTAssertEqual(legacy.key, restored.key)
        XCTAssertEqual(legacy.request, restored.request)
        XCTAssertFalse(legacy.isUncertain)
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

    func testPendingStopRequestsCompleteStateBeforeSelectingSuccessor() throws {
        let client = APIClient.unconnected(token: "fixture-token")
        let recent = try client.makeTunnelRequest("state")
        let full = try client.makeTunnelRequest("state", fullState: true)
        XCTAssertEqual(recent.path, "/v1/state")
        XCTAssertEqual(recent.headers["X-Coconut-State"], "recent")
        XCTAssertEqual(full.path, "/v1/state")
        XCTAssertNil(full.headers["X-Coconut-State"])
        XCTAssertEqual(full.headers["Authorization"], "Bearer fixture-token")

        let confirmation = CheckoutRecoveryConfirmation(preview: try recoveryPreview(), pairing: recoveryPairing,
                                                        workItemId: "export")
        let stop = try XCTUnwrap(CheckoutRecoveryStop(confirmation: try roundtripPendingCheckout(confirmation)))
        var resumed = item(status: "cancelled")
        resumed.taskId = "successor"; resumed.nativeTaskId = "native-export"; resumed.workState = "cancelled"
        let running = owner(status: "queued", id: "successor")
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [running]))
        XCTAssertEqual(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"), running])?.id,
                       "successor")
        var foreign = running
        foreign.botId = "other"
        XCTAssertNil(stop.admittedSuccessor(in: [resumed], tasks: [owner(status: "cancelled"), foreign]))
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
        store.recordCheckoutRecoveryResult(transport, for: try XCTUnwrap(store.checkoutConfirmation))
        XCTAssertTrue(store.checkoutConfirmation?.isUncertain == true)
        XCTAssertEqual(store.checkoutConfirmation?.key, "same-key")
        XCTAssertEqual(try APIJSON.encode(XCTUnwrap(store.checkoutConfirmation).request), initialBody)
        store.recordCheckoutRecoveryResult(.classify(error: ServiceError.response(503, "Unavailable")),
                                           for: try XCTUnwrap(store.checkoutConfirmation))
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
            deviceId: "re-paired", route: recoveryPairing.route, macKey: recoveryPairing.macKey), in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, route: "https://different.example", macKey: recoveryPairing.macKey), in: location))
        XCTAssertNil(ProtectedCache.readPendingCheckout(for: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, route: recoveryPairing.route,
            macKey: String(repeating: "b", count: 64)), in: location))
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
        let newHost = CheckoutRecoveryPairing(deviceId: "new-phone", route: "https://new.example:443",
                                              macKey: String(repeating: "b", count: 64))
        XCTAssertTrue(confirmation.matches(pairing: recoveryPairing))
        XCTAssertFalse(confirmation.matches(pairing: newHost))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, route: newHost.route, macKey: recoveryPairing.macKey)))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: recoveryPairing.deviceId, route: recoveryPairing.route, macKey: newHost.macKey)))
        XCTAssertFalse(confirmation.matches(pairing: CheckoutRecoveryPairing(
            deviceId: newHost.deviceId, route: recoveryPairing.route, macKey: recoveryPairing.macKey)))
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

    func testIdleNativeWorkUsesHermesActivityAndFinishedWorkStaysForAWeek() throws {
        var waiting = item(status: "waiting")
        waiting.workState = "waiting"
        waiting.nativeTaskId = "native-export"
        waiting.nativeStatus = "blocked"
        waiting.nativeLastActivityAt = "2026-09-01T00:00:00Z"
        waiting.nativeDiagnostics = [NativeDiagnostic(kind: "stuck_in_blocked", severity: "warning", title: "Blocked with no comment")]
        let start = try XCTUnwrap("2026-09-01T00:00:00Z".apiDate)
        let row = TrackedWorkItem(artifact: waiting, pullRequests: [])
        XCTAssertFalse(row.isIdle(at: start.addingTimeInterval(6 * 86_400)))
        XCTAssertTrue(row.isIdle(at: start.addingTimeInterval(9 * 86_400)))
        XCTAssertEqual(row.idleLabel(at: start.addingTimeInterval(9 * 86_400)), "Idle 9 days · Blocked with no comment")
        waiting.nativeStatus = "running"
        XCTAssertFalse(TrackedWorkItem(artifact: waiting, pullRequests: []).isIdle(at: start.addingTimeInterval(9 * 86_400)))
        waiting.nativeStatus = "todo"
        waiting.nativeTaskId = nil
        XCTAssertFalse(TrackedWorkItem(artifact: waiting, pullRequests: []).isIdle(at: start.addingTimeInterval(9 * 86_400)))

        var finished = item(status: "completed")
        finished.workState = "completed"
        finished.nativeTaskId = "native-export"
        finished.nativeStatus = "blocked"
        finished.nativeLastActivityAt = "2026-09-01T00:00:00Z"
        let done = TrackedWorkItem(artifact: finished, pullRequests: [])
        XCTAssertFalse(done.isIdle(at: start.addingTimeInterval(9 * 86_400)))
        XCTAssertTrue(done.isRecent(at: start.addingTimeInterval(6 * 86_400)))
        XCTAssertFalse(done.isRecent(at: start.addingTimeInterval(8 * 86_400)))
    }

    func testTaskDetailsGroupDelegatedAndLinkedWorkByTheirHermesTask() throws {
        let start = try XCTUnwrap("2026-09-01T00:00:00Z".apiDate)
        let later = start.addingTimeInterval(9 * 86_400)
        func waiting(_ id: String, bot: String, status: String) -> Artifact {
            var work = item(status: "waiting")
            work.id = id; work.botId = bot; work.workState = "waiting"; work.nativeTaskId = "native-" + id
            work.nativeStatus = status; work.nativeLastActivityAt = "2026-09-01T00:00:00Z"
            return work
        }
        // Desk delegated this Work. Hermes keeps its task under the delegate.
        var delegated = waiting("delegated", bot: "delegate", status: "triage")
        delegated.taskId = "owner"
        let owner = AgentTask(id: "owner", botId: "delegate", sessionId: "session", title: "Export account data",
                              status: "completed", summary: "", createdAt: "", updatedAt: "", threadId: "thread",
                              conversationBotId: "desk", sourceBotId: "desk")
        // Desk owns this Work and its Hermes task, but the newest copy of its PR belongs to the delegate.
        let linked = waiting("linked", bot: "desk", status: "blocked")
        var deskCopy = linked
        // The Mac copies the Work item's native state onto each PR copy.
        deskCopy.id = "pr-desk"; deskCopy.kind = "pr"; deskCopy.workOutcomeId = linked.id
        deskCopy.url = "https://github.com/example/project/pull/42"; deskCopy.observedAt = "2026-09-01T00:00:00Z"
        var delegateCopy = deskCopy
        delegateCopy.id = "pr-delegate"; delegateCopy.botId = "delegate"; delegateCopy.observedAt = "2026-09-02T00:00:00Z"
        let rows = TrackedWorkItem.nativeTasks(in: [delegated, linked, deskCopy, delegateCopy], botId: "desk", tasks: [owner])
        XCTAssertEqual(rows.count, 2)
        let pullRequestRow = try XCTUnwrap(rows.first { $0.pullRequest != nil })
        XCTAssertEqual(pullRequestRow.artifact.botId, "delegate")
        XCTAssertEqual(pullRequestRow.nativeOwnerBotId, "desk")
        XCTAssertEqual(pullRequestRow.nativeTaskId, "native-linked")
        XCTAssertEqual(rows.first { $0.pullRequest == nil }?.nativeOwnerBotId, "delegate")
        let groups = TrackedWorkItem.Groups(rows, at: later)
        XCTAssertTrue(groups.active.isEmpty)
        XCTAssertEqual(Set(groups.idle.map(\.id)), Set(rows.map(\.id)))
        XCTAssertTrue(TrackedWorkItem.Groups(rows, at: start.addingTimeInterval(6 * 86_400)).idle.isEmpty)
        // Without its Work item, a PR copy cannot name the bot that owns the Hermes task, so it stays active.
        let orphan = TrackedWorkItem.nativeTasks(in: [deskCopy, delegateCopy], botId: "desk")
        XCTAssertEqual(orphan.count, 1)
        XCTAssertNil(orphan.first?.nativeOwnerBotId)
        XCTAssertEqual(TrackedWorkItem.Groups(orphan, at: later).active.count, 1)

        // A stopped checkout stays on top for a week. After that its recovery stays with older finished Work.
        var stopped = item(status: "cancelled")
        stopped.nativeTaskId = "native-export"; stopped.taskId = "owner"; stopped.workState = "cancelled"
        stopped.checkoutRecoveryStopped = true
        let recoverable = TrackedWorkItem.nativeTasks(in: [stopped], botId: "bot", tasks: [self.owner(status: "cancelled")])
        XCTAssertEqual(recoverable.first?.canOfferCheckoutRecovery, true)
        let created = try XCTUnwrap(stopped.createdAt.apiDate)
        XCTAssertEqual(TrackedWorkItem.Groups(recoverable, at: created.addingTimeInterval(86_400)).active.count, 1)
        XCTAssertEqual(TrackedWorkItem.Groups(recoverable, at: created.addingTimeInterval(8 * 86_400)).older.count, 1)
    }

    func testIdleArchiveSendsEachTaskToItsOwnerAndContinuesAfterAFailure() async throws {
        func work(_ id: String, bot: String) -> TrackedWorkItem {
            var artifact = item(status: "waiting")
            artifact.id = id; artifact.botId = bot; artifact.nativeTaskId = "native-" + id
            return TrackedWorkItem(artifact: artifact, pullRequests: [])
        }
        var linkedCopy = item(status: "waiting")
        linkedCopy.id = "pr"; linkedCopy.kind = "pr"; linkedCopy.botId = "delegate"; linkedCopy.nativeTaskId = "native-linked"
        var linkedWork = item(status: "waiting")
        linkedWork.id = "linked"; linkedWork.botId = "desk"; linkedWork.nativeTaskId = "native-linked"
        // The PR row repeats the Work row's task. The orphan PR copy has no Work item, so no owner.
        var orphan = linkedCopy
        orphan.id = "orphan"; orphan.nativeTaskId = "native-orphan"
        let items = [work("a", bot: "delegate"), TrackedWorkItem(artifact: linkedCopy, pullRequests: [], recoveryWork: linkedWork),
                     TrackedWorkItem(artifact: linkedWork, pullRequests: []), TrackedWorkItem(artifact: orphan, pullRequests: [])]
            + (0..<501).map { work(String(format: "bulk-%03d", $0), bot: "bulk") }
        var requests: [(String, [String])] = []
        let error = await AppStore.archiveIdleRequests(items) { botId, batch in
            requests.append((botId, batch))
            return botId == "bulk" ? "The Mac is offline." : nil
        }
        // The first failed batch stops only that bot. The other bots still archive.
        XCTAssertEqual(error, "The Mac is offline.")
        XCTAssertEqual(requests.map(\.0), ["bulk", "delegate", "desk"])
        XCTAssertEqual(requests[0].1.count, 500)
        XCTAssertEqual(requests[1].1, ["native-a"])
        XCTAssertEqual(requests[2].1, ["native-linked"])
        requests = []
        let success = await AppStore.archiveIdleRequests(items) { botId, batch in
            requests.append((botId, batch))
            return nil
        }
        XCTAssertNil(success)
        XCTAssertEqual(requests.map(\.0), ["bulk", "bulk", "delegate", "desk"])
        XCTAssertEqual(requests.map(\.1.count), [500, 1, 1, 1])
    }
}
