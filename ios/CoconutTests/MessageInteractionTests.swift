import XCTest
@testable import Coconut

final class MessageInteractionTests: XCTestCase {
    @MainActor
    func testWorkerQuoteKeepsMainConversationAndRespondingState() throws {
        let store = AppStore(demo: true)
        let main = try XCTUnwrap(store.conversationBot)
        let worker = try XCTUnwrap(store.bots.first { $0.id != main.id })
        var task = try XCTUnwrap(store.tasks.first)
        task.botId = worker.id
        task.conversationBotId = worker.id
        task.status = "working"
        store.tasks = [task]
        let message = Message(id: "source", botId: worker.id, taskId: task.id, role: "assistant", kind: "text", text: "Ready.", decisionId: nil, artifactId: nil, createdAt: "2026-09-22T10:00:00Z", streaming: false)
        store.messages = [message]
        store.replyInCoconut(to: message)
        XCTAssertEqual(store.coconutReply?.messageId, message.id)
        XCTAssertEqual(store.coconutReply?.botId, worker.id)
        var request = message
        request.id = "request"; request.botId = main.id; request.role = "user"; request.replyTo = store.coconutReply
        store.messages = [request]
        XCTAssertEqual(store.referenceBotId(message.id, in: request), worker.id)
        XCTAssertTrue(store.isConversationResponding(main.id))
        store.tasks[0].status = "completed"
        XCTAssertFalse(store.isConversationResponding(main.id))
    }

    @MainActor
    func testReplyUsesServerIdentityAfterOptimisticReplacement() throws {
        let store = AppStore(demo: true)
        var saved = try XCTUnwrap(store.messages.first { $0.kind == "text" })
        saved.clientRequestId = "request-id"
        saved.role = "user"
        saved.senderBotId = nil
        saved.recipientBotIds = []
        store.messages = [saved]
        let displayed = try XCTUnwrap(store.conversationMessages(for: saved.botId).first)
        XCTAssertEqual(displayed.id, "outgoing-request-id")
        let reference = try XCTUnwrap(store.messageReference(for: displayed))
        XCTAssertEqual(reference.messageId, saved.id)
        XCTAssertEqual(reference.text, saved.text)
        XCTAssertEqual(reference.author, "You")
        store.messages = []
        XCTAssertNil(store.messageReference(for: displayed))
    }

    func testReplySurvivesOutgoingCacheAndReactionEventsStayOutOfTranscript() throws {
        let reference = MessageReference(messageId: "original", text: "Original text", author: "Desk", role: "assistant")
        let request = MessageRequest(text: "That one", mode: "auto", taskId: nil, scheduledAt: nil, attachmentIds: [], recipientBotIds: [], replyToMessageId: "original")
        let outgoing = OutgoingMessage(id: "request", botId: "desk", executorIds: ["desk"], request: request, createdAt: "2026-09-12T00:00:00Z", status: .sending, replyTo: reference)
        let restored = try JSONDecoder().decode(OutgoingMessage.self, from: JSONEncoder().encode(outgoing)).afterRestart
        XCTAssertEqual(restored.message.replyTo, reference)
        XCTAssertEqual(restored.request.replyToMessageId, reference.messageId)
        XCTAssertEqual(restored.status, .failed)
        var reaction = restored.message
        reaction.reactionEvent = MessageReactionEvent(emoji: "👍", action: "added")
        reaction.createdAt = "2026-09-12T01:00:00Z"
        XCTAssertEqual(latestConversationMessage(in: [restored.message, reaction]), restored.message)
        XCTAssertEqual(TranscriptPresentation.visibleMessages([reaction, restored.message], artifacts: []), [restored.message])
    }

    @MainActor
    func testFixedReactionsCanBeAddedAndRemoved() async throws {
        let store = AppStore(demo: true)
        var message = try XCTUnwrap(store.messages.first { $0.kind == "text" })
        message.role = "assistant"
        message.senderBotId = message.botId
        message.recipientBotIds = []
        message.exchangeRecipientBotIds = nil
        store.messages = [message]
        await store.react(to: message, emoji: "✅")
        XCTAssertEqual(store.reactions(for: message), [])
        for emoji in MessageReactions.all { await store.react(to: message, emoji: emoji) }
        XCTAssertEqual(store.reactions(for: message), MessageReactions.all)
        await store.react(to: message, emoji: "👍")
        XCTAssertFalse(store.reactions(for: message).contains("👍"))
        XCTAssertEqual(store.reactions(for: message).count, 7)
    }
}
