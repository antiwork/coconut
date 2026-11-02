import XCTest
@testable import Coconut

final class UnsentMessageTests: XCTestCase {
    private func outgoing(_ id: String, _ status: OutgoingMessage.Status, sequence: Int) -> OutgoingMessage {
        OutgoingMessage(id: id, botId: "bot", executorIds: ["bot"],
                        request: MessageRequest(text: id, mode: "queue", taskId: nil, scheduledAt: nil, attachmentIds: [],
                                                recipientBotIds: []),
                        createdAt: "2026-11-02T00:00:00Z", status: status, sequence: sequence)
    }

    @MainActor
    func testOnlyAMessageThatDidNotSendCanBeDeleted() {
        let store = AppStore(demo: true)
        store.outgoingMessages = [outgoing("failed", .failed, sequence: 1), outgoing("rejected", .rejected, sequence: 2),
                                  outgoing("sending", .sending, sequence: 3), outgoing("accepted", .accepted, sequence: 4)]
        // A message that is on its way, or that the Mac took, stays.
        XCTAssertFalse(store.deleteUnsent("sending"))
        XCTAssertFalse(store.deleteUnsent("accepted"))
        XCTAssertTrue(store.deleteUnsent("failed"))
        XCTAssertTrue(store.deleteUnsent("rejected"))
        XCTAssertEqual(store.outgoingMessages.map(\.id), ["sending", "accepted"])
        XCTAssertFalse(store.deleteUnsent("failed"), "Nothing is left to delete.")
    }
}
