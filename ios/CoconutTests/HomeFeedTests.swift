import XCTest
@testable import Coconut

/// The lists of Home: "Unread" with each unread chat and each pending card, and "In progress".
final class HomeFeedTests: XCTestCase {
    func testUnreadHasEachUnreadChatAndEachPendingCardNewestFirst() {
        var team = IAData.room("r1", unread: 3, last: "2026-11-16T10:00:00Z")
        team.directs = [TeamDirect(member: "m2", memberName: "Grace", unread: 1,
                                   lastMessage: TeamLastMessage(text: "Hi", createdAt: "2026-11-16T11:30:00Z"))]
        let bots = [IAData.bot("planner", unread: 2), IAData.bot("writer"), IAData.bot("starred", unread: 1, pinned: true),
                    IAData.bot("coconut", unread: 4, coconut: true), IAData.bot("old", unread: 5, archived: true)]
        let latest = ["planner": "2026-11-16T09:00:00Z".apiDate!, "starred": "2026-11-16T08:00:00Z".apiDate!,
                      "coconut": "2026-11-16T07:00:00Z".apiDate!]
        let decisions = [IAData.decision("ship", created: "2026-11-16T11:00:00Z"),
                         IAData.decision("done", status: "approved", created: "2026-11-16T11:45:00Z"),
                         IAData.decision("sent", created: "2026-11-16T11:50:00Z")]
        let unread = HomeFeed.unread(bots: bots, latest: latest, rooms: [team, IAData.room("quiet")],
                                     decisions: decisions, answering: ["sent"])
        XCTAssertEqual(unread.map(\.id),
                       ["direct-r1-m2", "card-ship", "team-r1", "bot-planner", "bot-starred", "bot-coconut"],
                       "Newest first. A read chat, an archived agent, an answered card and a card whose answer travels do not show. A starred agent and the Coconut chat do.")
        XCTAssertEqual(HomeFeed.badge(unread), 1 + 1 + 3 + 2 + 1 + 4, "The unread messages, and one for each card.")
    }

    func testEachUnreadRowLeadsToItsPlace() {
        let team = IAData.room("r1", unread: 1, last: "2026-11-16T10:00:00Z")
        let unread = HomeFeed.unread(bots: [IAData.bot("planner", unread: 1)], latest: [:], rooms: [team],
                                     decisions: [IAData.decision("ship", created: "2026-11-16T11:00:00Z")])
        XCTAssertEqual(unread.map(\.target), [
            .cards("ship"), .teamChat(TeamChatKey(room: "r1")), .conversation(ConversationRoute(botId: "planner")),
        ])
    }

    func testAMutedChatIsNotUnread() {
        var chat = TeamNamedChat(id: "c1", name: "Launch", createdAt: "2026-11-10T00:00:00Z")
        chat.unread = 2
        chat.notifications = false
        var team = IAData.room("r1")
        team.chats = [chat]
        XCTAssertEqual(HomeFeed.unread(bots: [], latest: [:], rooms: [team], decisions: []).map(\.id), [])
        team.chats?[0].notifications = true
        XCTAssertEqual(HomeFeed.unread(bots: [], latest: [:], rooms: [team], decisions: []).map(\.id), ["team-r1-c1"])
    }

    func testAnEmptyDayHasNoRows() {
        XCTAssertEqual(HomeFeed.unread(bots: [IAData.bot("planner")], latest: [:], rooms: [IAData.room("r1")], decisions: []), [])
        XCTAssertEqual(HomeFeed.badge([]), 0)
        XCTAssertEqual(HomeFeed.inProgress(bots: [IAData.bot("planner")], tasks: [], artifacts: [], rooms: []), [])
    }

    func testInProgressHasTheTurnsThenTheToDosThenTheNotices() {
        let bots = [IAData.bot("planner"), IAData.bot("writer"), IAData.bot("old", archived: true)]
        let tasks = [
            IAData.task("t-covered", "running", bot: "planner", updated: "2026-11-16T11:00:00Z"),
            IAData.task("t-first", "running", bot: "writer", updated: "2026-11-16T11:00:00Z"),
            IAData.task("t-newest", "working", bot: "writer", updated: "2026-11-16T11:30:00Z"),
            IAData.task("t-done", "completed", bot: "writer", updated: "2026-11-16T11:40:00Z"),
            IAData.task("t-old", "running", bot: "old", updated: "2026-11-16T11:40:00Z"),
        ]
        let artifacts = [
            IAData.work("ready", "ready", created: "2026-11-16T08:00:00Z"),
            IAData.work("doing", "working", task: "t-covered", created: "2026-11-16T09:00:00Z"),
            IAData.work("finished", "completed", created: "2026-11-16T09:00:00Z"),
            IAData.work("stuck", "blocked", bot: "writer", created: "2026-11-16T07:00:00Z"),
        ]
        let rooms = [IAData.room("r1", notices: [IAData.notice("freeze", .status), IAData.notice("ended", .rule, active: false)]),
                     IAData.room("left", notices: [IAData.notice("gone", .status)], state: "removed")]
        let rows = HomeFeed.inProgress(bots: bots, tasks: tasks, artifacts: artifacts, rooms: rooms)
        XCTAssertEqual(rows.map(\.id), ["turn-t-newest", "todo-doing", "todo-stuck", "todo-ready", "notice-r1-freeze"],
                       "An agent shows its newest turn. A turn whose To-do shows does not show again.")
        XCTAssertEqual(rows[1].target, .conversation(ConversationRoute(botId: "planner", workContext: WorkReference(id: "doing", title: "Work doing"))))
        XCTAssertEqual(rows.last?.target, .notices("r1"))
    }
}
