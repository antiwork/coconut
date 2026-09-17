import XCTest
@testable import Coconut

final class ConversationCardPositionTests: XCTestCase {
    @MainActor
    func testPendingCardKeepsItsDateAcrossUpdatesAndHistoryLoading() throws {
        let store = AppStore(demo: true)
        var card = try XCTUnwrap(store.decisions.first)
        card.status = "pending"
        card.createdAt = "2026-09-17T09:00:00Z"
        store.tasks = []
        store.decisions = [card]
        store.outgoingMessages = []
        let before = Message(id: "before", botId: card.botId, role: "user", kind: "text", text: "Prepare the report.",
                             createdAt: "2026-09-17T08:00:00Z", streaming: false)
        let after = Message(id: "after", botId: card.botId, role: "assistant", kind: "text", text: "The next report is ready.",
                            createdAt: "2026-09-17T10:00:00Z", streaming: false)
        store.messages = [after]
        XCTAssertEqual(store.conversationTranscript(for: card.botId).map(\.id), [card.id, after.id])
        store.decisions[0].title = "Updated report question?"
        XCTAssertEqual(store.conversationTranscript(for: card.botId).map(\.id), [card.id, after.id])
        store.messages.insert(before, at: 0)
        let original = Message(id: "original-card-message", botId: card.botId, taskId: card.taskId,
                               role: "assistant", kind: "decision", text: "", decisionId: card.id,
                               createdAt: card.createdAt, streaming: false)
        store.messages.append(original)
        let loaded = store.conversationTranscript(for: card.botId)
        XCTAssertEqual(loaded.map(\.id), [before.id, card.id, after.id])
        XCTAssertEqual(loaded.filter { $0.decisionId == card.id }.count, 1)
    }

    @MainActor
    func testMissingCardUsesItsConversationAndOnlyFocusRestoresAnAnsweredCard() throws {
        let store = AppStore(demo: true)
        var card = try XCTUnwrap(store.decisions.first)
        var task = try XCTUnwrap(store.tasks.first)
        task.id = card.taskId
        task.conversationBotId = "requester"
        store.tasks = [task]
        store.messages = []
        store.outgoingMessages = []
        card.status = "pending"
        store.decisions = [card]
        XCTAssertTrue(store.conversationTranscript(for: card.botId).isEmpty)
        XCTAssertEqual(store.conversationTranscript(for: "requester").map(\.decisionId), [card.id])
        store.decisions[0].status = "approved"
        XCTAssertTrue(store.conversationTranscript(for: "requester").isEmpty)
        XCTAssertEqual(store.conversationTranscript(for: "requester", focusDecisionId: card.id).map(\.decisionId), [card.id])
        store.decisions[0].status = "replied"
        XCTAssertTrue(store.conversationTranscript(for: "requester", focusDecisionId: card.id).isEmpty)
    }
}
