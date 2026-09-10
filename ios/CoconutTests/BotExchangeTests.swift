import XCTest
@testable import Coconut

final class BotExchangeTests: XCTestCase {
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

    private func message(_ id: String, from sender: String, to recipient: String?, kind: String = "bot_exchange") -> Message {
        Message(id: id, botId: sender, role: "assistant", kind: kind, text: "Body \(id)",
                createdAt: "2026-09-10T12:00:00Z", streaming: false,
                senderBotId: sender, recipientBotIds: recipient.map { [$0] } ?? [])
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
