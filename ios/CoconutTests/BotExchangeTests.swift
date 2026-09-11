import XCTest
@testable import Coconut

final class BotExchangeTests: XCTestCase {
    @MainActor
    func testAutomaticSendUsesTheOpenBotWithoutASelectedTask() async throws {
        let store = AppStore(demo: true)
        let bot = try XCTUnwrap(store.bots.first)
        let other = try XCTUnwrap(store.bots.first { $0.id != bot.id })
        let task = AgentTask(id: "mentioned", botId: other.id, sessionId: other.sessionId,
                             title: "Other work", status: "working", summary: "", createdAt: "", updatedAt: "",
                             threadId: "mentioned", conversationBotId: bot.id)
        store.tasks = [task]
        store.messages = []
        let sent = await store.send(bot: bot, text: "New request")
        XCTAssertTrue(sent)
        XCTAssertEqual(store.tasks.last?.botId, bot.id)
        XCTAssertNotEqual(store.messages.first?.taskId, task.id)
        let current = try XCTUnwrap(store.tasks.last)
        let followup = await store.send(bot: bot, text: "Also include this")
        XCTAssertTrue(followup)
        XCTAssertEqual(store.tasks.count, 2)
        XCTAssertEqual(store.messages.last?.taskId, current.id)
    }

    @MainActor
    func testDelegatedTaskAcceptsSteeringFromItsExecutorConversation() async throws {
        let store = AppStore(demo: true)
        let origin = try XCTUnwrap(store.bots.first)
        let executor = try XCTUnwrap(store.bots.first { $0.id != origin.id })
        let task = AgentTask(id: "delegated", botId: executor.id, sessionId: executor.sessionId,
                             title: "Research", status: "working", summary: "", createdAt: "", updatedAt: "",
                             threadId: "thread", conversationBotId: origin.id, sourceBotId: origin.id)
        store.tasks = [task]
        store.messages = []

        let accepted = await store.send(bot: executor, text: "Include the sources", mode: "steer", taskId: task.id)
        XCTAssertTrue(accepted)
        XCTAssertEqual(store.tasks, [task])
        XCTAssertEqual(store.messages.first?.taskId, task.id)
        XCTAssertEqual(store.messages.first?.botId, executor.id)

        let count = store.messages.count
        let wrongConversation = await store.send(bot: origin, text: "Follow up", mode: "steer", taskId: task.id)
        XCTAssertFalse(wrongConversation)
        XCTAssertEqual(store.messages.count, count)
    }

    @MainActor
    func testMentionedTaskAcceptsSteeringFromItsOriginalConversation() async throws {
        let store = AppStore(demo: true)
        let origin = try XCTUnwrap(store.bots.first)
        let executor = try XCTUnwrap(store.bots.first { $0.id != origin.id })
        let task = AgentTask(id: "mentioned", botId: executor.id, sessionId: executor.sessionId,
                             title: "Research", status: "working", summary: "", createdAt: "", updatedAt: "",
                             threadId: "thread", conversationBotId: origin.id)
        store.tasks = [task]
        store.messages = []

        let accepted = await store.send(bot: origin, text: "Include the sources", mode: "steer", taskId: task.id)
        XCTAssertTrue(accepted)
        XCTAssertEqual(store.tasks, [task])
        XCTAssertEqual(store.messages.first?.taskId, task.id)
        let wrongConversation = await store.send(bot: executor, text: "Follow up", mode: "steer", taskId: task.id)
        XCTAssertFalse(wrongConversation)
    }

    @MainActor
    func testDecisionDestinationFollowsTheConversationThatReceivesItsContext() throws {
        let store = AppStore(demo: true)
        var decision = try XCTUnwrap(store.decisions.first)
        var task = try XCTUnwrap(store.tasks.first)
        task.botId = "executor"
        task.conversationBotId = "origin"
        task.sourceBotId = nil
        decision.taskId = task.id
        decision.botId = task.botId
        store.tasks = [task]
        XCTAssertEqual(store.conversationBotId(for: decision), "origin")
        task.sourceBotId = "origin"
        store.tasks = [task]
        XCTAssertEqual(store.conversationBotId(for: decision), "executor")
        store.tasks = []
        XCTAssertEqual(store.conversationBotId(for: decision), "executor")
    }

    private func message(_ id: String, from sender: String, to recipient: String?, kind: String = "bot_exchange") -> Message {
        Message(id: id, botId: sender, role: "assistant", kind: kind, text: "Body \(id)",
                createdAt: "2026-09-10T12:00:00Z", streaming: false,
                senderBotId: sender, recipientBotIds: recipient.map { [$0] } ?? [])
    }

    func testReturnedNativeReplyStaysCollapsedInBothConversations() {
        let request = message("request", from: "a", to: "b")
        var reply = message("reply", from: "b", to: nil, kind: "text")
        reply.exchangeRecipientBotIds = ["a"]
        reply.streaming = true
        let own = message("own", from: "b", to: nil, kind: "text")
        for botId in ["a", "b"] {
            let rows = TranscriptEntry.grouped([request, reply, own], botId: botId)
            XCTAssertEqual(rows.filter(\.isExchange).count, 1)
            XCTAssertEqual(rows.first?.messages.map(\.id), ["request", "reply"])
            XCTAssertFalse(rows.contains { !$0.isExchange && $0.message.id == reply.id })
            XCTAssertEqual(rows.filter { !$0.isExchange }.map(\.id), botId == "b" ? ["own"] : [])
        }
    }

    func testBothDirectionsGroupWithoutAbsorbingWorkUpdatesOrOtherBots() {
        let messages = [message("1", from: "a", to: "b"), message("2", from: "b", to: "a"),
                        message("3", from: "a", to: nil, kind: "text"),
                        message("4", from: "a", to: "b"), message("5", from: "a", to: "c")]
        let rows = TranscriptEntry.grouped(messages)
        XCTAssertEqual(rows.map(\.id), ["1-exchange", "3", "4-exchange", "5-exchange"])
        XCTAssertEqual(rows[0].messages.map(\.id), ["1", "2"])
        XCTAssertEqual(rows[0].message.exchangeBotIds, ["a", "b"])
        XCTAssertEqual(messages[1].exchangeBotIds, ["a", "b"])
        XCTAssertEqual(rows[1].message.text, "Body 3")
    }
}
