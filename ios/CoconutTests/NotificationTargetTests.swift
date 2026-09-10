import XCTest
@testable import Coconut

final class NotificationTargetTests: XCTestCase {
    private let device = "00000000-0000-4000-8000-000000000001"
    private let bot = "00000000-0000-4000-8000-000000000002"
    private let task = "00000000-0000-4000-8000-000000000003"
    private let decision = "00000000-0000-4000-8000-000000000004"

    func testDecisionNotificationKeepsStableDestination() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: payload(decisionID: decision)))
        XCTAssertEqual(target.deviceId, device)
        XCTAssertEqual(target.botId, bot)
        XCTAssertEqual(target.taskId, task)
        XCTAssertEqual(target.decisionId, decision)
        XCTAssertEqual(target.conversationRoute, ConversationRoute(botId: bot, decisionId: decision))
    }

    func testCompletionAllowsNoDecision() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: payload()))
        XCTAssertNil(target.decisionId)
        XCTAssertEqual(target.conversationRoute, ConversationRoute(botId: bot))
    }

    func testRejectsUnknownSchemaAndMalformedIdentifiers() {
        for (key, value) in [("version", 2 as Any), ("task_id", "https://example.com" as Any),
                             ("decision_id", "unknown" as Any), ("device_id", "" as Any)] {
            var data = payload()["coconut"] as! [String: Any]
            data[key] = value
            XCTAssertNil(NotificationTarget(userInfo: ["coconut": data]))
        }
        XCTAssertNil(NotificationTarget(userInfo: [:]))
    }

    @MainActor
    func testUnpairedAppDoesNotAcceptNotificationDestination() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: payload()))
        XCTAssertFalse(AppStore(demo: true).acceptsNotification(target))
    }

    private func payload(decisionID: String? = nil) -> [AnyHashable: Any] {
        var data: [String: Any] = ["version": 1, "device_id": device, "bot_id": bot, "task_id": task]
        if let decisionID { data["decision_id"] = decisionID }
        return ["coconut": data]
    }
}
