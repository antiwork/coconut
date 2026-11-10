import XCTest
@testable import Coconut

final class MessageInteractionTests: XCTestCase {
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

    /// The index must find what a scan from the start finds, after each kind of change of the messages.
    @MainActor
    func testMessageIndexFollowsEveryChangeOfTheMessages() {
        let store = AppStore(demo: true)
        func message(_ id: String, request: String? = nil) -> Message {
            Message(id: id, botId: "desk", role: "user", kind: "text", text: "Text \(id)",
                    createdAt: "2026-11-10T12:00:00Z", streaming: false, clientRequestId: request)
        }
        func assertMatchesScan(_ step: String, file: StaticString = #filePath, line: UInt = #line) {
            for id in ["a", "b", "c", "d", "r1", "r2", "missing"] {
                XCTAssertEqual(store.savedMessage(id: id), store.messages.first { $0.id == id }, "\(step): \(id)", file: file, line: line)
                for request in [nil, "r1", "r2", "missing"] {
                    let probe = message(id, request: request)
                    let scan = store.messages.first { $0.id == id || (request != nil && $0.clientRequestId == request) }
                    XCTAssertEqual(store.messageIndex.first(id: id, clientRequestId: request).map { store.messages[$0] }, scan,
                                   "\(step): \(id) \(request ?? "-")", file: file, line: line)
                    XCTAssertEqual(store.messageReference(for: probe)?.messageId, scan?.id, "\(step): reference", file: file, line: line)
                }
                XCTAssertEqual(store.displayedMessageId(for: id),
                               store.messages.first { $0.id == id }?.clientRequestId.map { "outgoing-" + $0 } ?? id, step, file: file, line: line)
            }
        }
        store.messages = [message("a"), message("b", request: "r1"), message("c")]
        assertMatchesScan("replace")
        store.messages.append(message("d", request: "r2"))
        assertMatchesScan("append")
        store.messages.insert(message("r1"), at: 0)
        assertMatchesScan("insert at the start")
        // A second message with the same ID: the first one wins, as in a scan.
        store.messages.append(message("a", request: "r1"))
        assertMatchesScan("duplicate")
        store.messages.removeAll { $0.id == "b" }
        assertMatchesScan("remove")
        store.messages[0].clientRequestId = "r2"
        assertMatchesScan("change in place")
        store.messages = []
        assertMatchesScan("empty")
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
