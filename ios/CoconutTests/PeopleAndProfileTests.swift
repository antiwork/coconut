import XCTest
@testable import Coconut

/// The People tab and the Profile tab as data.
final class PeopleAndProfileTests: XCTestCase {
    private func member(_ id: String, _ name: String, own: Bool = false, botOnly: Bool = false) -> TeamMember {
        TeamMember(member: id, name: name, thisMac: own, botOnly: botOnly)
    }

    func testATeamHasItsChatsItsBoardAndItsPeople() {
        var team = IAData.room("r1", last: "2026-11-16T10:00:00Z", notices: [IAData.notice("freeze", .status, seen: false)])
        team.members = [member("m3", "Lin"), member("m1", "Sahil", own: true), member("m2", "Grace"), member("m4", "Build Mac", botOnly: true)]
        team.chats = [TeamNamedChat(id: "c1", name: "Launch", createdAt: "2026-11-10T00:00:00Z")]
        team.directs = [TeamDirect(member: "m2", memberName: "Grace", unread: 1,
                                   lastMessage: TeamLastMessage(text: "Hi", createdAt: "2026-11-16T11:30:00Z"))]
        let left = IAData.room("left", state: "removed")
        let sections = PeopleSections(rooms: [team, left])
        XCTAssertEqual(sections.teams.map(\.id), ["r1", "left"])
        XCTAssertEqual(sections.teams[0].chats.map(\.id), ["team-r1", "team-r1-c1"], "The Team chat, then the named chats.")
        XCTAssertEqual(sections.teams[0].members.map(\.name), ["Sahil", "Grace", "Lin"], "This person first. No Mac for a team only.")
        XCTAssertTrue(sections.teams[0].hasBoard)
        XCTAssertEqual(sections.teams[1].chats, [])
        XCTAssertFalse(sections.teams[1].hasBoard)
        XCTAssertEqual(sections.directs.map(\.id), ["direct-r1-m2"])
        XCTAssertEqual(sections.detail, "2 teams · 3 people")
    }

    func testProfileNamesThePersonAsTheTeamDoes() {
        var team = IAData.room("r1")
        team.members = [member("m2", "Grace"), member("m1", "Sahil", own: true)]
        XCTAssertEqual(ProfileSummary(rooms: [team], remembered: "Old name").name, "Sahil")
        XCTAssertEqual(ProfileSummary(rooms: [team], remembered: "").member?.member, "m1")
        XCTAssertEqual(ProfileSummary(rooms: [], remembered: " Ada ").name, "Ada")
        XCTAssertNil(ProfileSummary(rooms: [], remembered: "").name)
    }

    func testProfileSaysHowFreshTheDataIs() {
        let now = IAData.now
        XCTAssertEqual(ProfileSummary.connection(.connected, lastSync: now.addingTimeInterval(-120), demo: false, now: now),
                       "Connected · synced 2m ago")
        XCTAssertEqual(ProfileSummary.connection(.macOffline, lastSync: nil, demo: false, now: now), "Mac offline")
        XCTAssertEqual(ProfileSummary.connection(.preview, lastSync: nil, demo: true, now: now), "Preview data")
    }
}
