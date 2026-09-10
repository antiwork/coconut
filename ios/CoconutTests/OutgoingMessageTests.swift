import XCTest
@testable import Coconut

final class OutgoingMessageTests: XCTestCase {
    @MainActor
    func testConfirmationReplacesOnlyItsOwnLocalMessageAndKeepsRowIdentity() {
        let store = AppStore(demo: true)
        let first = outgoing("first")
        let second = outgoing("second")
        store.messages = []
        store.outgoingMessages = [first, second]
        XCTAssertEqual(store.conversationMessages(for: "bot").count, 2)
        var confirmed = first.message
        confirmed.id = "server-id"
        confirmed.taskId = "server-task"
        store.messages = [confirmed]
        let visible = store.conversationMessages(for: "bot")
        XCTAssertEqual(visible.count, 2)
        XCTAssertEqual(visible.first { $0.clientRequestId == first.id }?.id, first.message.id)
        XCTAssertEqual(visible.first { $0.clientRequestId == first.id }?.taskId, "server-task")
    }

    func testFailedAndScheduledMessagesDoNotAnimate() throws {
        var message = outgoing("first")
        XCTAssertTrue(message.expectsResponse)
        message.status = .failed
        XCTAssertFalse(message.expectsResponse)
        let restored = try JSONDecoder().decode(OutgoingMessage.self, from: JSONEncoder().encode(message))
        XCTAssertEqual(restored.id, message.id)
        XCTAssertEqual(restored.request.text, message.request.text)
        XCTAssertFalse(restored.expectsResponse)
        message.status = .rejected
        XCTAssertFalse(message.expectsResponse)
        let scheduled = OutgoingMessage(id: "scheduled", botId: "bot", executorIds: ["bot"],
            request: MessageRequest(text: "Later", mode: "queue", taskId: nil, scheduledAt: "2030-01-01T09:00:00Z",
                                    attachmentIds: [], recipientBotIds: []), createdAt: "2026-09-10T12:00:00Z", status: .sending)
        XCTAssertFalse(scheduled.expectsResponse)
        XCTAssertEqual(scheduled.afterRestart.status, .failed)
        XCTAssertEqual(scheduled.afterRestart.request.scheduledAt, scheduled.request.scheduledAt)
        XCTAssertEqual(message.afterRestart.status, .rejected)
    }

    private func outgoing(_ id: String) -> OutgoingMessage {
        OutgoingMessage(id: id, botId: "bot", executorIds: ["bot"],
            request: MessageRequest(text: "Same text", mode: "queue", taskId: nil, scheduledAt: nil,
                                    attachmentIds: [], recipientBotIds: []), createdAt: "2026-09-10T12:00:00Z", status: .sending)
    }
}
