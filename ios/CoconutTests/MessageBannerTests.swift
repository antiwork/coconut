import XCTest
@testable import Coconut

/// The banners of the Mac app for a new team message and a new direct message (MessageBanners): which message makes a
/// banner, its content, its identifier and thread, the banners to remove, and what a click shows.
final class MessageBannerTests: XCTestCase {
    private let grace = "grace"
    private let lin = "lin"

    private func last(_ first: Int, member: String? = "grace", name: String? = "Grace", own: Bool = false,
                      bot: String? = nil, botName: String? = nil, text: String = "See you at 3") -> TeamLastMessage {
        TeamLastMessage(first: first, member: member, memberName: name, thisMac: own, bot: bot, botName: botName,
                        text: text, createdAt: "2026-11-12T10:00:00Z")
    }

    private func direct(_ member: String = "grace", name: String = "Grace", unread: Int, last: TeamLastMessage?) -> TeamDirect {
        TeamDirect(member: member, memberName: name, unread: unread, lastMessage: last)
    }

    private func room(_ id: String = "r1", state: String = "active", notifications: Bool? = true, unread: Int = 0,
                      last: TeamLastMessage? = nil, directs: [TeamDirect]? = []) -> TeamRoom {
        TeamRoom(room: id, name: "Example Team", state: state,
                 members: [TeamMember(member: "ada", name: "Ada", thisMac: true),
                           TeamMember(member: grace, name: "Grace", direct: true),
                           TeamMember(member: lin, name: "Lin", direct: true)],
                 unread: unread, lastMessage: last, notifications: notifications, directs: directs)
    }

    private func rooms(_ rooms: TeamRoom...) -> TeamRooms { TeamRooms(rooms: rooms, helper: true) }

    // MARK: Which message makes a banner

    func testANewMessageOfAnotherPersonInTheTeamChatMakesABanner() {
        let plan = MessageBanners.plan(before: rooms(room(unread: 0, last: last(1))), after: rooms(room(unread: 1, last: last(2))))
        XCTAssertEqual(plan.post.map(\.chat), [TeamChatKey(room: "r1")])
    }

    func testANewMessageOfAnotherPersonInADirectChatMakesABanner() {
        let before = rooms(room(directs: [direct(unread: 0, last: last(1))]))
        let after = rooms(room(directs: [direct(unread: 1, last: last(2))]))
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post.map(\.chat),
                       [TeamChatKey(room: "r1", member: grace)])
    }

    func testAnOwnMessageMakesNoBanner() {
        let own = last(2, member: "ada", name: "Ada", own: true)
        let plan = MessageBanners.plan(before: rooms(room(unread: 0, last: last(1), directs: [direct(unread: 0, last: last(1))])),
                                    after: rooms(room(unread: 1, last: own, directs: [direct(unread: 1, last: own)])))
        XCTAssertEqual(plan.post, [])
    }

    func testAMessageOfABotOfThisMacMakesABanner() {
        // The service counts it as unread: the person did not write the reply of a bot of this Mac.
        let reply = last(2, member: "ada", name: "Ada", own: true, bot: "t-r1-b1", botName: "Reporter")
        let plan = MessageBanners.plan(before: rooms(room(unread: 0, last: last(1))), after: rooms(room(unread: 1, last: reply)))
        XCTAssertEqual(plan.post.map(\.title), ["Reporter"])
    }

    func testTheFirstListMakesNoBanner() {
        let plan = MessageBanners.plan(before: nil, after: rooms(room(unread: 5, last: last(9), directs: [direct(unread: 2, last: last(4))])))
        XCTAssertEqual(plan.post, [], "The first list after a start or a new pairing is history.")
        XCTAssertTrue(plan.asksPermission)
    }

    func testACountThatDidNotGrowMakesNoBanner() {
        let plan = MessageBanners.plan(before: rooms(room(unread: 2, last: last(3))), after: rooms(room(unread: 2, last: last(4))))
        XCTAssertEqual(plan.post, [])
    }

    func testACountThatGrewWithTheSameNewestMessageMakesNoBanner() {
        let plan = MessageBanners.plan(before: rooms(room(unread: 1, last: last(4))), after: rooms(room(unread: 2, last: last(4))))
        XCTAssertEqual(plan.post, [])
    }

    func testSeveralNewMessagesMakeOneBannerForTheNewest() {
        let plan = MessageBanners.plan(before: rooms(room(unread: 0, last: last(1))),
                                    after: rooms(room(unread: 3, last: last(4, text: "The third one"))))
        XCTAssertEqual(plan.post.count, 1)
        XCTAssertEqual(plan.post.first?.body, "The third one")
    }

    func testATeamWithItsSwitchOffMakesNoBannerButItsDirectChatDoes() {
        let before = rooms(room(notifications: false, unread: 0, last: last(1), directs: [direct(unread: 0, last: last(1))]))
        let after = rooms(room(notifications: false, unread: 1, last: last(2), directs: [direct(unread: 1, last: last(2))]))
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post.map(\.chat),
                       [TeamChatKey(room: "r1", member: grace)])
    }

    func testATeamThatIsNotActiveMakesNoBanner() {
        for state in ["joining", "removed", "gone"] {
            let before = rooms(room(state: state, unread: 0, last: last(1), directs: [direct(unread: 0, last: last(1))]))
            let after = rooms(room(state: state, unread: 1, last: last(2), directs: [direct(unread: 1, last: last(2))]))
            XCTAssertEqual(MessageBanners.plan(before: before, after: after).post, [], state)
            XCTAssertFalse(MessageBanners.plan(before: before, after: after).asksPermission, state)
        }
    }

    func testAChatThatIsNewInTheListMakesABanner() {
        let before = rooms(room(directs: [direct(unread: 0, last: last(1))]))
        let after = rooms(room(directs: [direct(lin, name: "Lin", unread: 1, last: last(1, member: lin, name: "Lin")),
                                         direct(unread: 0, last: last(1))]))
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).post.map(\.chat), [TeamChatKey(room: "r1", member: lin)])
    }

    func testAChatThatLeftTheListMakesNoBannerAndLosesItsBanner() {
        let before = rooms(room(unread: 1, last: last(1), directs: [direct(unread: 1, last: last(1))]), room("r2", unread: 1, last: last(1)))
        let after = rooms(room(unread: 1, last: last(1)))
        let plan = MessageBanners.plan(before: before, after: after)
        XCTAssertEqual(plan.post, [])
        XCTAssertEqual(plan.remove, ["team-chat:r1:grace", "team-chat:r2"])
    }

    func testAnOlderServiceWithoutCountsMakesNoBanner() {
        var old = room(last: last(1)); old.unread = nil
        var new = room(last: last(2)); new.unread = nil
        XCTAssertEqual(MessageBanners.plan(before: rooms(old), after: rooms(new)).post, [])
    }

    // MARK: The content

    func testTheBannerOfAPersonInATeam() {
        let plan = MessageBanners.plan(before: rooms(room(unread: 0, last: last(1))),
                                    after: rooms(room(unread: 1, last: last(2, text: "**Lunch** at [noon](https://example.com)"))))
        XCTAssertEqual(plan.post, [MessageBanner(chat: "r1", title: "Grace", subtitle: "Example Team", body: "Lunch at noon")])
    }

    func testTheBannerOfABotInATeam() {
        let message = last(2, member: lin, name: "Lin", bot: "t-r1-b1", botName: "Reporter", text: "The report is ready.")
        XCTAssertEqual(MessageBanners.banner("r1", room: room(), last: message),
                       MessageBanner(chat: "r1", title: "Reporter", subtitle: "Example Team", body: "The report is ready."))
    }

    func testTheBannerOfAWriterWithoutANameHasTheTeamAsTitle() {
        for message in [last(2, name: nil), last(2, name: "  "), last(2, bot: "t-r1-b1", botName: nil)] {
            XCTAssertEqual(MessageBanners.banner("r1", room: room(), last: message),
                           MessageBanner(chat: "r1", title: "Example Team", subtitle: nil, body: "See you at 3"))
        }
    }

    func testTheBannerOfADirectMessageHasThePersonAndNoSubtitle() {
        let chat = TeamChatKey(room: "r1", member: grace)
        XCTAssertEqual(MessageBanners.banner(chat, room: room(), last: last(2)),
                       MessageBanner(chat: chat, title: "Grace", subtitle: nil, body: "See you at 3"))
        // A person who left keeps the last name of the list of the direct chats, as the row of the chat.
        var left = room(directs: [direct("gone", name: "Lin", unread: 1, last: last(2, member: "gone", name: nil))])
        left.members = []
        XCTAssertEqual(MessageBanners.banner(TeamChatKey(room: "r1", member: "gone"), room: left, last: last(2)).title, "Lin")
    }

    func testTheRowsOfTheChatsListKeepTheirNames() {
        XCTAssertEqual(TeamChatPreview.text(last(2, bot: "t-r1-b1", botName: nil)), "Bot: See you at 3")
        XCTAssertEqual(TeamChatPreview.text(last(2, name: nil)), "Former member: See you at 3")
        XCTAssertEqual(TeamChatPreview.text(last(2)), "Grace: See you at 3")
    }

    // MARK: The identifier, the thread and the removal

    func testTheIdentifierAndTheThreadOfAChat() {
        let team = TeamChatKey(room: "r1")
        let chat = TeamChatKey(room: "r1", member: grace)
        XCTAssertEqual(MessageBanners.identifier(team), "team-chat:r1")
        XCTAssertEqual(MessageBanners.identifier(chat), "team-chat:r1:grace")
        // The same threads as the push of the iPhone (backend/coconut/push.py).
        XCTAssertEqual(MessageBanners.thread(team), "r1")
        XCTAssertEqual(MessageBanners.thread(chat), "r1:grace")
        XCTAssertNotEqual(MessageBanners.identifier(team), MessageBanners.identifier(chat))
    }

    func testAChatWhoseCountIsZeroLosesItsBanner() {
        let before = rooms(room(unread: 2, last: last(2), directs: [direct(unread: 1, last: last(2)),
                                                                     direct(lin, name: "Lin", unread: 3, last: last(5))]))
        let after = rooms(room(unread: 0, last: last(2), directs: [direct(unread: 0, last: last(2)),
                                                                    direct(lin, name: "Lin", unread: 3, last: last(5))]))
        XCTAssertEqual(MessageBanners.plan(before: before, after: after).remove, ["team-chat:r1", "team-chat:r1:grace"])
        // The first list removes the banners of the chats that the person read meanwhile, for example on the iPhone.
        XCTAssertEqual(MessageBanners.plan(before: nil, after: after).remove, ["team-chat:r1", "team-chat:r1:grace"])
    }

    func testAPostThenAReadInTheAppThenAnEqualListRemovesTheBannerOfTheChat() {
        var posted = PostedBanners()
        let before = rooms(room(unread: 0, last: last(1)))
        let new = rooms(room(unread: 1, last: last(2)))
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: before, after: new)), [])
        XCTAssertEqual(posted.identifiers, ["team-chat:r1"])
        // The answer of the read sets the count to 0 in the list of the store. The next list of the Mac is equal.
        let read = rooms(room(unread: 0, last: last(2)))
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: read, after: read)), ["team-chat:r1"])
        XCTAssertEqual(posted.identifiers, [])
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: read, after: read)), [],
                       "A removed banner is not removed again.")
    }

    func testABannerOfAnEarlierRunGoesWhenThePersonReadsItsChat() {
        // macOS still has the banner of an unread chat from an earlier run.
        var posted = PostedBanners(delivered: ["team-chat:r1:grace"])
        let unread = rooms(room(directs: [direct(unread: 2, last: last(3))]))
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: nil, after: unread)), [],
                       "The first list leaves the banner of an unread chat.")
        let read = rooms(room(directs: [direct(unread: 0, last: last(3))]))
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: unread, after: read)), ["team-chat:r1:grace"])
    }

    func testTheSeedIgnoresANotificationThatIsNotTheBannerOfAChat() {
        let posted = PostedBanners(delivered: ["team-chat:r1", "inbox-1", "r1"])
        XCTAssertEqual(posted.identifiers, ["team-chat:r1"])
        XCTAssertTrue(MessageBanners.isIdentifier(MessageBanners.identifier(TeamChatKey(room: "r1", member: grace))))
    }

    func testAnEmptySeedAndAnEqualListAskMacOSForNothing() {
        var posted = PostedBanners(delivered: [])
        let list = rooms(room(unread: 0, last: last(1), directs: [direct(unread: 0, last: last(1))]))
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: nil, after: list)), [])
        XCTAssertEqual(posted.removals(for: MessageBanners.plan(before: list, after: list)), [])
        XCTAssertEqual(posted.identifiers, [])
    }

    func testPermissionWaitsForAnActiveTeam() {
        XCTAssertFalse(MessageBanners.plan(before: nil, after: rooms()).asksPermission)
        XCTAssertFalse(MessageBanners.plan(before: nil, after: rooms(room(state: "joining"))).asksPermission)
        XCTAssertTrue(MessageBanners.plan(before: nil, after: rooms(room())).asksPermission)
    }

    // MARK: A click

    func testAClickShowsTheChatOfTheBanner() {
        let list = rooms(room(directs: [direct(unread: 1, last: last(2))]))
        for chat in [TeamChatKey(room: "r1"), TeamChatKey(room: "r1", member: grace)] {
            let clicked = MessageBanners.chat(userInfo: MessageBanners.userInfo(chat))
            XCTAssertEqual(clicked, chat)
            XCTAssertEqual(MessageBanners.shownChat(clicked, rooms: list), chat)
        }
    }

    func testAClickOnAChatThatTheAppNoLongerHasShowsTheWindowOnly() {
        let list = rooms(room(directs: []))
        XCTAssertNil(MessageBanners.shownChat(TeamChatKey(room: "r2"), rooms: list))
        XCTAssertNil(MessageBanners.shownChat(TeamChatKey(room: "r1", member: grace), rooms: list))
        XCTAssertNil(MessageBanners.shownChat(TeamChatKey(room: "r1"), rooms: nil), "The app has no list yet.")
        XCTAssertNil(MessageBanners.chat(userInfo: ["coconut": ["version": 1]]), "Another notification names no chat.")
        XCTAssertNil(MessageBanners.shownChat(nil, rooms: list))
    }
}
