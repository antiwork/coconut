import XCTest
@testable import Coconut

/// The Coconut chat: the agent of the default profile, at the top of the lists, apart from the other agents.
final class CoconutChatTests: XCTestCase {
    private func bot(_ id: String, coconut: Bool? = nil) throws -> Bot {
        let flag = coconut.map { ", \"coconut\": \($0)" } ?? ""
        let json = """
        {"id": "\(id)", "session_id": "s", "name": "\(id)", "role": "", "avatar": "", "color": "", "instructions": "",
         "status": "idle", "last_message": "", "unread_count": 0, "archived": false, "created_at": "2026-11-16T00:00:00Z"\(flag)}
        """
        return try APIJSON.decoder().decode(Bot.self, from: Data(json.utf8))
    }

    func testTheMacMarksTheCoconutChatAndAnOlderMacDoesNot() throws {
        XCTAssertTrue(try bot("a", coconut: true).coconut)
        XCTAssertFalse(try bot("b").coconut)
    }

    func testTheCoconutChatStandsApartFromTheAgents() throws {
        let bots = [try bot("Scout"), try bot("Coconut", coconut: true), try bot("Desk")]
        XCTAssertEqual(bots.coconutChat?.id, "Coconut")
        XCTAssertEqual(bots.agents.map(\.id), ["Scout", "Desk"])
        XCTAssertNil([try bot("Scout")].coconutChat)
    }
}
