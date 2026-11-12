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

    private let room = String(repeating: "ab", count: 32)

    func testATeamMessageOpensTheChatOfItsTeam() throws {
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "team", "team": room]]))
        XCTAssertEqual(target.deviceId, device)
        XCTAssertEqual(target.team, room)
        XCTAssertFalse(target.group)
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false), .team(room))
    }

    func testAGroupOfOneTeamOpensItsChatAndEachOtherGroupOpensInbox() throws {
        let team = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 3, "team": room]]))
        XCTAssertTrue(team.group)
        XCTAssertEqual(team.destination(pendingDecisionIds: [], snapshotReady: false), .team(room))
        // A mixed group names no team. Its fields for older apps name the first card.
        let mixed = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": device, "kind": "group", "count": 2, "bot_id": bot, "task_id": task,
            "decision_id": decision, "message_id": NSNull()]]))
        XCTAssertNil(mixed.team)
        XCTAssertEqual(mixed.destination(pendingDecisionIds: [decision], snapshotReady: true), .inboxList)
    }

    func testATeamTargetNeedsTheIDOfARoom() {
        for value in ["", "AB" + room.dropFirst(2), String(room.dropLast()), room + "a", "https://example.com"] {
            for kind in ["team", "group"] {
                XCTAssertNil(NotificationTarget(userInfo: ["coconut": [
                    "version": 1, "device_id": device, "kind": kind, "team": value]]), "\(kind) \(value)")
            }
        }
        XCTAssertNil(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": device, "kind": "team"]]))
    }

    /// An app before team notifications reads a target only with a `bot_id` and a `task_id`, or as a group. The Mac
    /// sends neither for one team message, so that app opens and stays where it was: the rule of the code above the
    /// check of `kind == "team"`, which this payload without `kind` follows.
    func testATeamMessageWithoutItsKindIsNoTarget() {
        XCTAssertNil(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": device, "team": room]]))
    }

    @MainActor
    func testATeamThatTheAppNoLongerHasOpensTheChatsList() {
        let store = AppStore(demo: true)
        let other = String(repeating: "cd", count: 32)
        XCTAssertEqual(store.teamNotificationPlace(other), .team(other), "A list that is not loaded keeps each chat.")
        store.team = TeamRooms(rooms: [TeamRoom(room: room, name: "Example Team", state: "active")], helper: true)
        XCTAssertEqual(store.teamNotificationPlace(room), .team(room))
        XCTAssertEqual(store.teamNotificationPlace(other), .chatList)
    }

    private func payload(decisionID: String? = nil) -> [AnyHashable: Any] {
        var data: [String: Any] = ["version": 1, "device_id": device, "bot_id": bot, "task_id": task]
        if let decisionID { data["decision_id"] = decisionID }
        return ["coconut": data]
    }
}
