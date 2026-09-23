import XCTest
@testable import Coconut

final class DecisionInboxTests: XCTestCase {
    func testAssessmentDecodesOptionalLevelsAndStaleStateWithoutChangingPermissions() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        decision.status = "pending"
        decision.riskLevel = "medium"
        decision.risk = "The release date can be wrong."
        decision.evidence = "Compared release notes."
        decision.context = "Publish the release notes."
        decision.uncertainty = "The release time remains undecided."
        decision.options[0].riskLevel = "low"
        decision.options[0].risk = "Only the private draft changes."
        decision.assessmentStale = true
        let decoded = try APIJSON.decoder().decode(Decision.self, from: APIJSON.encoder().encode(decision))
        XCTAssertEqual(decoded.riskLevel, "medium")
        XCTAssertEqual(decoded.risk, decision.risk)
        XCTAssertEqual(decoded.evidence, decision.evidence)
        XCTAssertEqual(decoded.options[0].riskLevel, "low")
        XCTAssertEqual(decoded.options[0].risk, decision.options[0].risk)
        XCTAssertEqual(decoded.assessmentStale, true)
        XCTAssertEqual(decoded.cardContext, "Publish the release notes.")
        decision.assessmentStale = false
        XCTAssertTrue(decision.cardContext?.contains("The release time remains undecided.") == true)
        XCTAssertTrue(decoded.isPending, "An assessment must not become a permission gate.")
        XCTAssertEqual(DecisionRisk.label("not_assessed"), "Risk not assessed")
        XCTAssertEqual(DecisionRisk.label("unknown-future-value"), "Risk")
        XCTAssertEqual(DecisionRisk.label(nil), "Risk")
    }

    @MainActor
    func testBadgeWaitsForSnapshotAndClearsOnUnpair() {
        let store = AppStore(demo: true)
        var writes: [Int] = []
        store.setBadge = { writes.append($0) }
        store.lastSync = nil
        store.reconcileBadge()
        XCTAssertNil(store.reconciledBadgeCount)
        XCTAssertTrue(writes.isEmpty, "Opening offline without a snapshot must retain the push badge.")

        store.lastSync = Date()
        store.reconcileBadge()
        XCTAssertEqual(writes, [store.inboxBadgeCount])
        store.decisions = []
        store.reconcileBadge()
        XCTAssertEqual(writes.last, 0)
        store.disconnect()
        XCTAssertEqual(writes.last, 0)
        XCTAssertEqual(writes.count, 3, "Unpairing must explicitly clear the badge.")
    }

    func testRefreshingWireFlagPreservesUpdatingPresentation() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        decision.status = "pending"
        XCTAssertTrue(decision.isPending)
        XCTAssertFalse(decision.isRefreshing)
        decision.sourceRefreshing = true
        let decoded = try APIJSON.decoder().decode(Decision.self, from: APIJSON.encoder().encode(decision))
        XCTAssertEqual(decoded.status, "pending")
        XCTAssertTrue(decoded.isRefreshing)
        XCTAssertFalse(decoded.isPending)
        decision.sourceRefreshing = nil
        decision.status = "refreshing"
        XCTAssertTrue(decision.isRefreshing)
        decision.status = "dismissed"
        decision.sourceRefreshing = true
        XCTAssertFalse(decision.isRefreshing)
    }

    @MainActor
    func testRefreshingCardOutsideLoadedHistoryKeepsItsChatPosition() throws {
        let store = AppStore(demo: true)
        var decision = try XCTUnwrap(store.decisions.first)
        store.messages = []
        store.decisions = [decision]
        let botId = store.conversationBotId(for: decision)
        for legacyStatus in [false, true] {
            decision.status = legacyStatus ? "refreshing" : "pending"
            decision.sourceRefreshing = !legacyStatus
            store.decisions = [decision]
            let rows = store.conversationTranscript(for: botId)
            XCTAssertEqual(rows.map(\.decisionId), [decision.id])
            XCTAssertEqual(rows.first?.createdAt, decision.createdAt)
            XCTAssertEqual(store.inboxBadgeCount, 0)
            store.messages = rows
            XCTAssertEqual(store.conversationTranscript(for: botId).count, 1)
            store.messages = []
        }
        decision.status = "rejected"
        store.decisions = [decision]
        XCTAssertTrue(store.conversationTranscript(for: botId).isEmpty)
    }

    @MainActor
    func testTextReplyRemovesCardAndKeepsMessage() async throws {
        let store = AppStore(demo: true)
        let decision = try XCTUnwrap(store.decisions.first)
        let before = store.inboxBadgeCount
        let sent = await store.replyToDecision(decision, text: "What did you verify?")
        XCTAssertTrue(sent)
        XCTAssertEqual(store.inboxBadgeCount, before - 1)
        XCTAssertEqual(store.decisions.first { $0.id == decision.id }?.status, "replied")
        let visible = TranscriptPresentation.visibleMessages(store.messages, artifacts: store.artifacts, decisions: store.decisions)
        XCTAssertFalse(visible.contains { $0.kind == "decision" && $0.decisionId == decision.id })
        XCTAssertEqual(visible.filter { $0.role == "user" && $0.decisionId == decision.id }.map(\.text), ["What did you verify?"])
    }

    func testSwipesOnlyOrganizeAndRespectCancellation() {
        XCTAssertNil(InboxSwipeAction.resolve(translation: 12, projected: 500, width: 390, isProposal: true))
        XCTAssertNil(InboxSwipeAction.resolve(translation: -40, projected: -50, width: 390, isProposal: true))
        XCTAssertEqual(InboxSwipeAction.resolve(translation: 200, projected: 400, width: 390, isProposal: false), .dismiss)
        XCTAssertEqual(InboxSwipeAction.resolve(translation: -60, projected: -200, width: 390, isProposal: false), .later)
        XCTAssertEqual(InboxSwipeAction.resolve(translation: 160, projected: 200, width: 390, isProposal: true), .dismiss)
        XCTAssertNil(InboxSwipeAction.resolve(translation: -40, projected: 400, width: 390, isProposal: true))
    }

    func testNativeApprovalNamesExactCommandInTheContext() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        decision.actionType = "native_approval"
        decision.context = "Update the service."
        decision.resource = "launchctl kickstart gui/501/local.coconut.agent"
        XCTAssertEqual(decision.cardContext, "Update the service.\n" + decision.resource)
        decision.context = decision.resource
        XCTAssertEqual(decision.cardContext, decision.resource)
    }

    func testFileWithoutNamedOptionsHasVisibleDirectAnswers() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        let namedOptions = decision.options
        decision.actionType = "create_file"
        decision.resource = "report.md"
        decision.options = []
        XCTAssertEqual(decision.cardOptions.map(\.answer), ["approve", "reject"])
        XCTAssertEqual(decision.cardOptions.first?.explanation, "Create report.md on the Mac.")
        XCTAssertTrue(decision.options.isEmpty)
        decision.options = namedOptions
        XCTAssertEqual(decision.cardOptions, namedOptions)
        decision.options = []
        decision.actionType = "native_question"
        XCTAssertTrue(decision.cardOptions.isEmpty)
    }

    @MainActor
    func testInboxBadgeIncludesLaterAndExcludesResolvedOrAnswering() throws {
        let store = AppStore(demo: true)
        var decision = try XCTUnwrap(store.decisions.first)
        decision.status = "pending"
        store.decisions = [decision]
        XCTAssertEqual(store.inboxBadgeCount, 1)
        store.answeringDecisionIds.insert(decision.id)
        XCTAssertEqual(store.inboxBadgeCount, 0)
        store.answeringDecisionIds.remove(decision.id)
        XCTAssertEqual(store.inboxBadgeCount, 1)
        for status in ["dismissed", "approved", "executed", "rejected"] {
            decision.status = status
            store.decisions = [decision]
            XCTAssertEqual(store.inboxBadgeCount, 0)
        }
    }

    @MainActor
    func testChatLinkTargetsTheCardWithinItsConversation() throws {
        let store = AppStore(demo: true)
        var decision = try XCTUnwrap(store.decisions.first)
        decision.requestBotId = "original-bot"
        decision.requestMessageId = "original-request"
        XCTAssertEqual(store.decisionChatRoute(for: decision), ConversationRoute(botId: store.conversationBotId(for: decision), decisionId: decision.id))
        decision.requestMessageId = nil
        XCTAssertEqual(store.decisionChatRoute(for: decision), ConversationRoute(botId: store.conversationBotId(for: decision), decisionId: decision.id))
    }

    @MainActor
    func testRestoreReceiptKeepsSnapshotContextAndRejectsNewerSwipes() throws {
        let store = AppStore(demo: true)
        var original = try XCTUnwrap(store.decisions.first)
        original.status = "dismissed"
        original.inboxSwipeId = "my-swipe"
        original.replyPending = nil
        var snapshot = original
        snapshot.replyPending = false
        snapshot.requestText = "Original request from the conversation."
        store.decisions = [snapshot]
        var restored = original
        restored.status = "pending"
        restored.inboxSwipeId = nil
        store.applyInboxReceipt(restored, replacing: original)
        XCTAssertTrue(store.decisions[0].isPending)
        XCTAssertEqual(store.decisions[0].replyPending, false)
        XCTAssertEqual(store.decisions[0].requestText, snapshot.requestText)
        snapshot.inboxSwipeId = "another-device-swipe"
        store.decisions = [snapshot]
        store.applyInboxReceipt(restored, replacing: original)
        XCTAssertEqual(store.decisions[0], snapshot)
    }

    func testFailedSkipRestoresOnlyItsOwnItemAndKeepsNewArrivals() throws {
        var first = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        first.id = "first"; first.status = "pending"
        var second = first; second.id = "second"
        var arrived = first; arrived.id = "new"
        var queue = DecisionQueue()
        queue.reconcile([first, second], sort: .oldest)
        queue.remove(first.id)
        queue.reconcile([first, second, arrived], sort: .oldest)
        XCTAssertFalse(queue.ids.contains(first.id))
        queue.restoreFirst(first.id)
        XCTAssertEqual(queue.ids.first, first.id)
        XCTAssertEqual(Set(queue.ids), Set([first.id, second.id, arrived.id]))
        queue.remove(first.id)
        XCTAssertFalse(queue.ids.contains(first.id))
    }

    func testNewReviewPassRestartsDeferralOrderAndCounter() throws {
        var first = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        first.id = "first"; first.status = "pending"
        var second = first; second.id = "second"
        var queue = DecisionQueue()
        queue.reconcile([first, second], sort: .oldest)
        for id in [first.id, second.id, first.id, second.id] { queue.remove(id) }
        XCTAssertTrue(queue.ids.isEmpty)
        queue.reviewAgain([first, second], sort: .oldest)
        XCTAssertEqual(queue.remainingCount, 2)
        queue.remove(first.id)
        XCTAssertEqual(queue.ids, [second.id])
        XCTAssertEqual(queue.remainingCount, 1)
    }

    func testEveryDecisionHasItsOwnEntryAndResolvedItemsAreAbsent() throws {
        let state = try PreviewStateFixture.load()
        var first = try XCTUnwrap(state.decisions.first)
        first.status = "pending"
        var second = first
        second.id = "second"
        var resolved = first
        resolved.id = "resolved"
        resolved.status = "approved"
        XCTAssertEqual(InboxPresentation.entries(decisions: [first, second, resolved]).count, 2)
    }

    func testDefaultPutsRequiredOldestThenOptionalNewest() throws {
        var old = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        old.id = "old"; old.status = "pending"; old.createdAt = "2026-09-10T09:00:00Z"
        var new = old
        new.id = "new"; new.createdAt = "2026-09-11T09:00:00Z"
        var proposalOld = old
        proposalOld.id = "proposal-old"; proposalOld.inboxKind = "proposal"
        var proposalNew = new
        proposalNew.id = "proposal-new"; proposalNew.inboxKind = "proposal"
        let rows = [proposalOld, new, proposalNew, old]
        XCTAssertEqual(InboxPresentation.entries(decisions: rows).map(\.id), ["old", "new", "proposal-new", "proposal-old"])
        XCTAssertEqual(InboxPresentation.entries(decisions: rows, sort: .newest).first?.createdAt, new.createdAt)
    }

    func testKeepDoesNotRepeatUntilTheNextReview() throws {
        var first = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        first.id = "first"; first.status = "pending"
        var second = first; second.id = "second"
        var queue = DecisionQueue()
        queue.reconcile([first, second], sort: .oldest)
        queue.remove("first")
        XCTAssertEqual(queue.ids, ["second"])
        XCTAssertEqual(queue.remainingCount, 1)
        second.status = "approved"
        queue.reconcile([first, second], sort: .oldest)
        XCTAssertTrue(queue.ids.isEmpty)
        queue.remove("first")
        queue.reconcile([first, second], sort: .oldest)
        XCTAssertTrue(queue.ids.isEmpty)
        queue.reviewAgain([first, second], sort: .oldest)
        XCTAssertEqual(queue.ids, ["first"])
    }

    func testNewDecisionsDoNotMoveTheCurrentDecision() throws {
        var first = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        first.id = "first"; first.status = "pending"; first.createdAt = "2026-09-12T10:00:00Z"
        var earlier = first; earlier.id = "earlier"; earlier.createdAt = "2026-09-11T10:00:00Z"
        var queue = DecisionQueue()
        queue.reconcile([first], sort: .oldest)
        queue.reconcile([earlier, first], sort: .oldest)
        XCTAssertEqual(queue.ids, ["first", "earlier"])
    }

    func testRevisedOrRestoredDecisionReturnsAfterLater() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        decision.status = "pending"
        var queue = DecisionQueue()
        queue.reconcile([decision], sort: .oldest)
        queue.remove(decision.id)
        decision.inboxDeferredAt = "2026-09-12T10:00:00Z"
        queue.reconcile([decision], sort: .oldest)
        XCTAssertTrue(queue.ids.isEmpty)
        decision.revision += 1
        decision.inboxDeferredAt = nil
        queue.reconcile([decision], sort: .oldest)
        XCTAssertEqual(queue.ids, [decision.id])
        queue.remove(decision.id)
        decision.inboxDeferredAt = "2026-09-12T11:00:00Z"
        queue.reconcile([decision], sort: .oldest)
        decision.inboxDeferredAt = nil
        queue.reconcile([decision], sort: .oldest)
        XCTAssertEqual(queue.ids, [decision.id])
    }

    func testOptionalFieldsDecodeFromOldHosts() throws {
        let decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        XCTAssertNil(decision.inboxKind)
        XCTAssertNil(decision.replyPending)
        XCTAssertFalse(decision.isProposal)
    }
    @MainActor
    func testOriginalRequestUsesTheSameStableIdAsTheTranscript() throws {
        let store = AppStore(demo: true)
        var message = try XCTUnwrap(store.messages.first)
        message.id = "server-request"
        message.clientRequestId = "phone-request"
        store.messages = [message]
        let target = store.displayedMessageId(for: "server-request")
        XCTAssertEqual(target, "outgoing-phone-request")
        XCTAssertTrue(store.conversationMessages(for: message.botId).contains { $0.id == target })
        message.clientRequestId = nil
        store.messages = [message]
        XCTAssertEqual(store.displayedMessageId(for: message.id), message.id)
    }
}
