import XCTest
@testable import Coconut

/// The queue of a chat: the messages that wait for the next turn of its running task.
@MainActor
final class MessageQueueTests: XCTestCase {
    private func queued(_ id: String, _ text: String, files: [String] = [], state: String = "queued") -> QueuedMessage {
        QueuedMessage(id: id, text: text, createdAt: "2026-11-16T10:00:0\(id.suffix(1))Z", attachmentIds: files, state: state)
    }

    /// The preview store with a queue on the running task of the Support agent.
    private func store(_ queue: [QueuedMessage]) throws -> (AppStore, AgentTask) {
        let store = AppStore(demo: true)
        let index = try XCTUnwrap(store.tasks.firstIndex { $0.isRunning })
        store.tasks[index].queuedMessages = queue
        return (store, store.tasks[index])
    }

    func testATaskDecodesItsQueue() throws {
        let json = #"""
        {"id":"t","bot_id":"b","session_id":"s","title":"T","status":"working","summary":"","created_at":"c","updated_at":"u",
         "thread_id":"t","queued_messages":[
           {"id":"m1","text":"Check the totals","created_at":"2026-11-16T10:00:00Z","attachment_ids":["a1","a2"],"state":"queued","can_steer":true},
           {"id":"m2","text":"Then reply"}]}
        """#
        let task = try APIJSON.decoder().decode(AgentTask.self, from: Data(json.utf8))
        XCTAssertEqual(task.queuedMessages?.map(\.id), ["m1", "m2"])
        XCTAssertEqual(task.queuedMessages?[0].attachmentIds, ["a1", "a2"])
        XCTAssertEqual(task.queuedMessages?[0].canSteer, true)
        // An item with only its ID and text is a queued message without files.
        XCTAssertEqual(task.queuedMessages?[1], QueuedMessage(id: "m2", text: "Then reply", createdAt: ""))
        let plain = try APIJSON.decoder().decode(AgentTask.self, from: Data(json.replacingOccurrences(of: "queued_messages", with: "other").utf8))
        XCTAssertNil(plain.queuedMessages)
    }

    func testTheLabelCountsTheQueue() {
        XCTAssertNil(MessageQueue.label(0))
        XCTAssertEqual(MessageQueue.label(1), "1 queued")
        XCTAssertEqual(MessageQueue.label(2), "2 queued")
    }

    func testThePreviewIsTheFirstLineWithText() {
        XCTAssertEqual(MessageQueue.preview("\n  Check   the totals \nand reply", attachments: 0), "Check the totals")
        XCTAssertEqual(MessageQueue.preview(AttachmentOnly.text, attachments: 1), "1 attachment")
        XCTAssertEqual(MessageQueue.preview(AttachmentOnly.text, attachments: 3), "3 attachments")
    }

    func testARowMovesToThePlaceOfTheRowUnderIt() {
        let ids = ["a", "b", "c", "d"]
        XCTAssertEqual(MessageQueue.moved(ids, id: "a", onto: "c"), ["b", "c", "a", "d"])
        XCTAssertEqual(MessageQueue.moved(ids, id: "d", onto: "b"), ["a", "d", "b", "c"])
        XCTAssertEqual(MessageQueue.moved(ids, id: "b", onto: "b"), ids)
        XCTAssertEqual(MessageQueue.moved(ids, id: "x", onto: "b"), ids)
        XCTAssertEqual(MessageQueue.moved(ids, id: "b", by: -1), ["b", "a", "c", "d"])
        XCTAssertEqual(MessageQueue.moved(ids, id: "b", by: 1), ["a", "c", "b", "d"])
        XCTAssertEqual(MessageQueue.moved(ids, id: "a", by: -1), ids)
        XCTAssertEqual(MessageQueue.moved(ids, id: "d", by: 1), ids)
    }

    func testTheQueueShowsItsMessagesInOrderWithTheirState() throws {
        let (store, task) = try store([queued("m1", "One", files: ["f1", "f2"]), queued("m2", "Two", state: "steering")])
        let items = store.messageQueue(task.chatBotId)
        XCTAssertEqual(items.map(\.text), ["One", "Two"])
        XCTAssertEqual(items.map(\.attachments), [2, 0])
        XCTAssertEqual(items.map(\.state), [.queued, .steering])
        XCTAssertEqual(items.map(\.canChange), [true, false])
        XCTAssertEqual(store.queuedCount(task.chatBotId), 2)
        XCTAssertEqual(store.conversationActivity(task.chatBotId)?.queued, 2)
        // Another chat has no queue.
        let other = try XCTUnwrap(store.bots.first { $0.id != task.chatBotId })
        XCTAssertEqual(store.queuedCount(other.id), 0)
        XCTAssertTrue(store.messageQueue(other.id).isEmpty)
    }

    func testTheWorkingLineAndTheRowOfTheChatSayHowManyWait() throws {
        let (store, task) = try store([queued("m1", "One"), queued("m2", "Two")])
        let bot = try XCTUnwrap(store.bots.first { $0.id == task.chatBotId })
        let activity = try XCTUnwrap(store.conversationActivity(bot.id))
        XCTAssertTrue(BotStatusRow(bot: bot, activity: activity).meta(at: .now).hasSuffix("· 2 queued"))
        XCTAssertEqual(BotRowSubtitle.phase(.thinking).accessibilityLabel(bot: bot, preview: "", queued: 2),
                       "\(bot.name), \(BotPhase.thinking.label), 2 queued")
        XCTAssertEqual(BotRowSubtitle.phase(.thinking).accessibilityLabel(bot: bot, preview: ""),
                       "\(bot.name), \(BotPhase.thinking.label)")
    }

    func testAQueuedMessageWaitsAboveTheFieldAndNotInTheTranscript() async throws {
        let (store, task) = try store([])
        let bot = try XCTUnwrap(store.bots.first { $0.id == task.chatBotId })
        let before = store.conversationTranscript(for: bot.id).count
        let sent = await store.send(bot: bot, text: "Also check the Friday payouts")
        XCTAssertTrue(sent)
        XCTAssertEqual(store.messageQueue(bot.id).map(\.text), ["Also check the Friday payouts"])
        XCTAssertEqual(store.conversationTranscript(for: bot.id).count, before)
        // A turn takes the message: it leaves the queue and shows in the chat.
        let index = try XCTUnwrap(store.tasks.firstIndex { $0.id == task.id })
        store.tasks[index].queuedMessages = nil
        XCTAssertEqual(store.conversationTranscript(for: bot.id).last?.text, "Also check the Friday payouts")
    }

    func testTheFilesOfAQueuedMessageWaitWithIt() {
        func message(_ id: String, kind: String = "text", role: String = "user", file: String? = nil) -> Message {
            Message(id: id, botId: "b", taskId: "t", role: role, kind: kind, text: id, decisionId: nil, artifactId: file,
                    createdAt: "2026-11-16T10:00:00Z", streaming: false)
        }
        let rows = [message("m0"), message("m1"), message("f1", kind: "artifact", file: "a1"),
                    message("r1", kind: "artifact", role: "assistant", file: "a1"), message("f2", kind: "artifact", file: "a2")]
        XCTAssertEqual(MessageQueue.transcript(rows, without: ["m1"], attachments: ["a1"]).map(\.id), ["m0", "r1", "f2"])
        XCTAssertEqual(MessageQueue.transcript(rows, without: [], attachments: []).map(\.id), rows.map(\.id))
    }

    func testEditRemoveAndOrderChangeTheQueue() async throws {
        let (store, task) = try store([queued("m1", "One"), queued("m2", "Two"), queued("m3", "Three")])
        let bot = task.chatBotId
        await store.orderQueued(["m3", "m1", "m2"], task: task.id)
        XCTAssertEqual(store.messageQueue(bot).map(\.text), ["Three", "One", "Two"])
        // The messages keep the times of the queue, in the new order.
        let times = try XCTUnwrap(store.tasks.first { $0.id == task.id }?.queuedMessages?.map(\.createdAt))
        XCTAssertEqual(times, times.sorted())
        // A list that is not the queue changes nothing.
        await store.orderQueued(["m3", "m1"], task: task.id)
        XCTAssertEqual(store.messageQueue(bot).map(\.text), ["Three", "One", "Two"])
        await store.editQueued("m1", task: task.id, text: "  One, with the totals ")
        XCTAssertEqual(store.messageQueue(bot).map(\.text), ["Three", "One, with the totals", "Two"])
        await store.removeQueued("m3", task: task.id)
        XCTAssertEqual(store.messageQueue(bot).map(\.text), ["One, with the totals", "Two"])
        await store.removeQueued("m1", task: task.id)
        await store.removeQueued("m2", task: task.id)
        XCTAssertTrue(store.messageQueue(bot).isEmpty)
        XCTAssertNil(store.tasks.first { $0.id == task.id }?.queuedMessages)
    }

    func testAMessageOnItsWayToAWorkingAgentShowsInTheQueue() throws {
        let (store, task) = try store([queued("m1", "One")])
        let bot = task.chatBotId
        func outgoing(_ id: String, mode: String = "auto", status: OutgoingMessage.Status = .sending, scheduledAt: String? = nil) -> OutgoingMessage {
            OutgoingMessage(id: id, botId: bot, executorIds: [bot],
                            request: MessageRequest(text: "Text \(id)", mode: mode, taskId: nil, scheduledAt: scheduledAt, attachmentIds: ["a"], recipientBotIds: []),
                            createdAt: "2026-11-16T10:01:00Z", status: status)
        }
        store.outgoingMessages = [outgoing("o1"), outgoing("o2", status: .failed), outgoing("o3", scheduledAt: "2026-11-17T10:00:00Z"),
                                  outgoing("o4", mode: "queue")]
        let items = store.messageQueue(bot)
        XCTAssertEqual(items.map(\.id), ["m1", "outgoing-o1"])
        XCTAssertEqual(items.last?.state, .sending)
        XCTAssertEqual(items.last?.canChange, false)
        XCTAssertEqual(items.last?.attachments, 1)
        XCTAssertEqual(store.queuedCount(bot), 2)
        XCTAssertTrue(store.queueHidden(bot).ids.contains("outgoing-o1"))
        // A message that did not send stays in the chat with its Retry.
        XCTAssertFalse(store.queueHidden(bot).ids.contains("outgoing-o2"))
        // An agent that does not work takes the message as a new turn.
        let index = try XCTUnwrap(store.tasks.firstIndex { $0.id == task.id })
        store.tasks[index].status = "completed"
        store.tasks[index].queuedMessages = nil
        XCTAssertTrue(store.messageQueue(bot).isEmpty)
    }
}
