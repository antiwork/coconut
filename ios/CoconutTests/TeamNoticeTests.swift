import XCTest
@testable import Coconut

/// The notice board of a team: what the app reads of `notices`, the banner of a chat, the count of the sidebar, and
/// the countdown of an end.
final class TeamNoticeTests: XCTestCase {
    static let rooms = #"""
    {"rooms": [{"room": "example", "name": "Example Team", "state": "active", "unread": 0, "notices": [
      {"id": "n1", "type": "status", "text": "Deploys are frozen while we fix the relay. Don't merge to main.", "posted_at": "2026-11-16T21:02:17Z", "ends_at": null, "closed": false, "active": true, "author": "m2", "author_name": "Grace", "own": false, "seen": false, "seen_by": ["Lin"]},
      {"id": "n2", "type": "deadline", "text": "Q4 plan is due. One page per team in the Library.", "posted_at": "2026-11-16T15:30:00Z", "ends_at": "2026-11-20T22:00:00Z", "closed": false, "active": true, "author": "m1", "author_name": "Ada", "own": true, "seen": true, "seen_by": ["Grace", "Lin"]},
      {"id": "n3", "type": "rule", "text": "Every PR needs a real screenshot of the app.", "posted_at": "2026-11-12T09:00:00Z", "ends_at": null, "closed": false, "active": true, "author": "m1", "author_name": "Ada", "own": true, "seen": true, "seen_by": ["Grace"]},
      {"id": "n4", "type": "announcement", "text": "Coconut 0.2 ships Thursday.", "posted_at": "2026-11-10T18:00:00Z", "ends_at": "2026-11-13T18:00:00Z", "closed": false, "active": false, "author": "m2", "author_name": "Grace", "own": false, "seen": true, "seen_by": ["Ada", "Lin"]}
    ]}]}
    """#

    func testTheBoardReadsTheNoticesOfTheMac() throws {
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(Self.rooms.utf8))
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertEqual(room.noticeList.map(\.kind), [.status, .deadline, .rule, .announcement])
        XCTAssertEqual(room.noticeList[1].seenBy, ["Grace", "Lin"])
        XCTAssertEqual(room.unseenNotices, 1, "Only the active notices that the person has not seen count.")
        XCTAssertEqual(room.bannerNotice?.id, "n1", "The newest active Status or Deadline is the banner.")
    }

    func testARuleOrAnAnnouncementIsNoBanner() throws {
        var room = try XCTUnwrap(try APIJSON.decoder().decode(TeamRooms.self, from: Data(Self.rooms.utf8)).rooms.first)
        room.notices = room.noticeList.filter { !$0.kind.isBanner }
        XCTAssertNil(room.bannerNotice)
        room.notices = nil
        XCTAssertEqual(room.unseenNotices, 0)
    }

    func testTheCountdownGetsPreciseNearTheEnd() {
        let now = Date(timeIntervalSince1970: 0)
        XCTAssertEqual(NoticeCountdown.text(until: now.addingTimeInterval(2 * 86_400 + 4 * 3600), now: now), "2d 04h")
        XCTAssertEqual(NoticeCountdown.text(until: now.addingTimeInterval(3 * 3600 + 5 * 60 + 9), now: now), "3h 05m")
        XCTAssertEqual(NoticeCountdown.text(until: now.addingTimeInterval(4 * 60 + 9), now: now), "4m 09s")
        XCTAssertEqual(NoticeCountdown.text(until: now, now: now), "Ended")
    }

    func testTheEndOfANoticeGoesOutAsUTCText() {
        XCTAssertEqual(AppStore.noticeTime(Date(timeIntervalSince1970: 1_800_000_000)), "2027-01-15T08:00:00Z")
    }
}
