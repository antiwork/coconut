import XCTest
@testable import Coconut

final class NotificationRoutingTests: XCTestCase {
    private let decision = UUID().uuidString
    private let message = UUID().uuidString
    private let bot = UUID().uuidString

    private func target(decisionId: String? = nil, messageId: String? = nil) throws -> NotificationTarget {
        var data: [String: Any] = ["version": 1, "device_id": UUID().uuidString, "bot_id": bot, "task_id": UUID().uuidString]
        data["decision_id"] = decisionId
        data["message_id"] = messageId
        return try XCTUnwrap(NotificationTarget(userInfo: ["coconut": data]))
    }

    func testPendingDecisionOpensInbox() throws {
        XCTAssertEqual(try target(decisionId: decision).destination(pendingDecisionIds: [decision], snapshotReady: true), .inbox(decision))
    }

    func testCachedPendingDecisionStillWaitsForFreshState() throws {
        XCTAssertNil(try target(decisionId: decision).destination(pendingDecisionIds: [decision], snapshotReady: false))
    }

    func testColdStartWaitsForSnapshotBeforeFallingBackToChat() throws {
        let target = try target(decisionId: decision)
        XCTAssertNil(target.destination(pendingDecisionIds: [], snapshotReady: false))
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: true), .conversation(ConversationRoute(botId: bot, decisionId: decision)))
    }

    func testMessageOpensExactMessageWithoutWaitingForSnapshot() throws {
        XCTAssertEqual(try target(messageId: message).destination(pendingDecisionIds: [], snapshotReady: false), .conversation(ConversationRoute(botId: bot, messageId: message)))
    }

    func testMalformedMessageTargetIsRejected() {
        XCTAssertNil(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": UUID().uuidString,
            "bot_id": bot, "task_id": UUID().uuidString, "message_id": "bad-id"]]))
    }
}
