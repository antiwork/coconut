import XCTest
@testable import Coconut

final class MessageReplayTests: XCTestCase {
    private var message: Message {
        Message(id: "message", botId: "bot", taskId: "task", role: "assistant", kind: "text", text: "Hello",
                decisionId: nil, artifactId: nil, createdAt: "2026-09-09T12:00:00Z", streaming: true)
    }

    func testStreamingReplayUpdatesOneMessageWithoutDuplicates() throws {
        var first = message; first.text = "Hello there"
        var second = first; second.text += "."; second.streaming = false
        let page = try page([event(11, first), event(12, second)], cursor: 12)
        XCTAssertEqual(MessageReplay.applying(page, after: 10, to: [message]), [second])
        XCTAssertEqual(MessageReplay.applying(page, after: 12, to: [second]), [second])
    }

    func testDeliveryTraceExcludesHistoryAndRepeatedSnapshots() throws {
        var previous = try PreviewStateFixture.load()
        previous.cursor = 10
        var reply = message
        reply.streaming = false
        var incoming = previous
        incoming.messages.append(reply)
        XCTAssertTrue(AppStore.newReplies(in: incoming, after: previous).isEmpty)
        incoming.cursor = 11
        XCTAssertEqual(AppStore.newReplies(in: incoming, after: previous).map(\.id), [reply.id])
        XCTAssertTrue(AppStore.newReplies(in: incoming, after: nil).isEmpty)
        XCTAssertTrue(AppStore.newReplies(in: incoming, after: incoming).isEmpty)
        var streaming = reply
        streaming.streaming = true
        previous.messages.append(streaming)
        XCTAssertEqual(AppStore.newReplies(in: incoming, after: previous).map(\.id), [reply.id])
        previous.messages[previous.messages.count - 1].streaming = false
        XCTAssertTrue(AppStore.newReplies(in: incoming, after: previous).isEmpty)
    }

    func testLateResponseCannotReplaceNewerText() throws {
        var current = message; current.text = "Hello there."
        let stale = try page([event(10, message)], cursor: 10)
        XCTAssertEqual(MessageReplay.applying(stale, after: 12, to: [current]), [current])
    }

    func testOverlappingReplayStartsAfterCurrentCursor() throws {
        var update = message; update.text = "New"
        let replay = try page([event(10, message), event(11, update)], cursor: 11)
        XCTAssertEqual(MessageReplay.applying(replay, after: 10, to: [message]), [update])
    }

    func testMissingEventsRequireSnapshot() throws {
        for page in [try page([event(12, message)], cursor: 12), try page([], cursor: 12),
                     try page([event(11, message)], cursor: 12)] {
            XCTAssertNil(MessageReplay.applying(page, after: 10, to: [message]))
        }
    }

    func testStructuralChangesRequireSnapshotForWholeBatch() throws {
        let page = try page([event(11, message), event(12, message, type: "task.updated")], cursor: 12)
        XCTAssertNil(MessageReplay.applying(page, after: 10, to: [message]))
    }

    func testUnknownMessageOrChangedOwnerRequiresSnapshot() throws {
        var unknown = message; unknown.id = "another-message"
        var moved = message; moved.botId = "another-bot"
        for update in [unknown, moved] {
            XCTAssertNil(MessageReplay.applying(try page([event(11, update)], cursor: 11), after: 10, to: [message]))
        }
    }

    func testHiddenMessageCannotReappearFromReplay() throws {
        var event = try event(11, message)
        var data = event["data"] as! [String: Any]
        data["_hidden"] = true
        event["data"] = data
        XCTAssertNil(MessageReplay.applying(try page([event], cursor: 11), after: 10, to: [message]))
    }

    func testMalformedMessageFallsBackToSnapshot() throws {
        var event = try event(11, message)
        event["data"] = ["id": "message"]
        XCTAssertNil(MessageReplay.applying(try page([event], cursor: 11), after: 10, to: [message]))
    }

    @MainActor
    func testHomeRowShowsNeedsYouOnlyWhileACardWaitsInThatChat() throws {
        let store = AppStore(demo: true)
        let fixture = try PreviewStateFixture.load()
        let bot = fixture.bots[0]
        var card = try XCTUnwrap(fixture.decisions.first)
        card.botId = bot.id; card.status = "pending"
        store.tasks = []
        store.decisions = [card]
        XCTAssertTrue(store.botNeedsYou(bot))
        XCTAssertEqual(BotRowSubtitle(needsYou: store.botNeedsYou(bot), phase: .thinking), .needsYou,
                       "The face shows the work, so the row names the card.")
        store.answeringDecisionIds = [card.id]
        XCTAssertFalse(store.botNeedsYou(bot), "An answer on its way does not need you again.")
        store.answeringDecisionIds = []
        card.status = "approved"
        store.decisions = [card]
        XCTAssertFalse(store.botNeedsYou(bot))
        XCTAssertEqual(BotRowSubtitle(needsYou: false, phase: .thinking), .phase(.thinking))
        XCTAssertEqual(BotRowSubtitle(needsYou: false, phase: nil), .preview)
    }

    func testHomeRowReadsTheUnreadCountOnlyAboveZero() throws {
        var bot = try PreviewStateFixture.load().bots[0]
        bot.unreadCount = 0
        XCTAssertEqual(BotRowSubtitle.needsYou.accessibilityLabel(bot: bot, preview: ""), "\(bot.name), Needs you")
        bot.unreadCount = 2; bot.status = "idle"
        XCTAssertEqual(BotRowSubtitle.preview.accessibilityLabel(bot: bot, preview: "You: Ship it"),
                       "\(bot.name), idle, You: Ship it, 2 unread")
    }

    @MainActor
    func testDotRequiresWorkingBotAndConfirmedConnection() throws {
        let store = AppStore(demo: false)
        var bot = try PreviewStateFixture.load().bots[0]
        store.connection = .connected
        for status in ["idle", "needs_you", "scheduled", "failed", "queued"] {
            bot.status = status
            XCTAssertFalse(store.isBotActive(bot))
        }
        for status in ["working", "running"] {
            bot.status = status
            XCTAssertTrue(store.isBotActive(bot))
        }
        bot.archived = true
        XCTAssertFalse(store.isBotActive(bot))
        bot.archived = false
        for connection in [ConnectionState.disconnected, .connecting, .unconfigured] {
            store.connection = connection
            XCTAssertFalse(store.isBotActive(bot))
        }
    }

    @MainActor
    func testPendingDecisionDoesNotHideIndependentWork() throws {
        let store = AppStore(demo: false)
        let fixture = try PreviewStateFixture.load()
        var bot = fixture.bots[0]
        bot.status = "needs_you"
        var task = fixture.tasks[0]
        task.botId = bot.id
        task.status = "working"
        store.tasks = [task]
        store.connection = .connected
        XCTAssertTrue(store.isBotActive(bot))
        store.connection = .disconnected
        XCTAssertFalse(store.isBotActive(bot))
        store.connection = .connected
        bot.archived = true
        XCTAssertFalse(store.isBotActive(bot))
        bot.archived = false
        store.tasks[0].status = "waiting"
        XCTAssertFalse(store.isBotActive(bot))
        store.tasks[0].status = "working"
        store.tasks[0].botId = "another-bot"
        XCTAssertFalse(store.isBotActive(bot))
    }

    @MainActor
    func testConversationActivityTracksRealResponseStatesPhasesAndStreaming() throws {
        let store = AppStore(demo: false)
        let fixture = try PreviewStateFixture.load()
        let bot = fixture.bots[1]
        var task = fixture.tasks.first { $0.botId == bot.id }!
        store.connection = .connected
        store.tasks = [task]
        for status in ["idle", "queued", "waiting", "scheduled", "failed", "completed"] {
            store.tasks[0].status = status
            XCTAssertNil(store.conversationActivity(bot.id), status)
        }
        for status in ["working", "running"] {
            store.tasks[0].status = status
            XCTAssertNotNil(store.conversationActivity(bot.id), status)
        }
        task.status = "waiting"
        store.tasks = [task]
        var stream = fixture.messages.first { $0.botId == bot.id && $0.role == "assistant" }!
        stream.streaming = true
        store.messages = [stream]
        XCTAssertNotNil(store.conversationActivity(bot.id), "A real partial response still needs feedback.")
        stream.streaming = false
        store.messages = [stream]
        XCTAssertNil(store.conversationActivity(bot.id))
        store.tasks[0].status = "working"
        store.tasks[0].phase = "testing"
        XCTAssertEqual(store.conversationActivity(bot.id)?.phase, .testing)
        store.tasks[0].phase = "an unknown phase"
        XCTAssertEqual(store.conversationActivity(bot.id)?.phase, .thinking)
        store.messages[0].streaming = true
        XCTAssertEqual(store.conversationActivity(bot.id)?.phase, .writing)
        XCTAssertEqual(store.conversationActivity(bot.id)?.reconnecting, false)
        store.connection = .disconnected
        XCTAssertEqual(store.conversationActivity(bot.id)?.reconnecting, true, "Work in progress stays visible while the phone reconnects.")
    }

    @MainActor
    func testPendingOutgoingResponseRemainsVisibleWhileConnectionIsUnconfirmed() throws {
        let store = AppStore(demo: false)
        let fixture = try PreviewStateFixture.load()
        let sourceBot = fixture.bots[0]
        let executorBot = fixture.bots[1]
        let request = MessageRequest(text: "Please investigate", mode: "ask", taskId: nil, scheduledAt: nil,
                                     attachmentIds: [], recipientBotIds: [executorBot.id])
        store.outgoingMessages = [OutgoingMessage(id: "pending-request", botId: sourceBot.id,
                                                  executorIds: [executorBot.id], request: request,
                                                  createdAt: "2026-09-18T12:00:00Z", status: .sending)]

        for connection in [ConnectionState.disconnected, .connecting] {
            store.connection = connection
            for status in [OutgoingMessage.Status.sending, .accepted] {
                store.outgoingMessages[0].status = status
                XCTAssertNotNil(store.conversationActivity(sourceBot.id), "\(status) while \(connection)")
                XCTAssertNil(store.conversationActivity(executorBot.id),
                               "Conversation activity must stay scoped to the outgoing message's source bot.")
            }
            store.outgoingMessages[0].status = .failed
            XCTAssertNil(store.conversationActivity(sourceBot.id), "Failed outgoing work must not show activity.")
        }
    }

    private func event(_ seq: Int, _ message: Message, type: String = "message.updated") throws -> [String: Any] {
        ["seq": seq, "type": type, "bot_id": message.botId, "task_id": message.taskId ?? "task",
         "created_at": message.createdAt, "data": try JSONSerialization.jsonObject(with: APIJSON.encode(message))]
    }

    private func page(_ events: [[String: Any]], cursor: Int) throws -> EventPage {
        try APIJSON.decoder().decode(EventPage.self, from: JSONSerialization.data(withJSONObject:
            ["events": events, "cursor": cursor, "server_time": "2026-09-09T12:00:00Z"]))
    }
}
