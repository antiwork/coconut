import XCTest
@testable import Coconut

/// The data of the tests of Home, of "Needs attention" and of the agent page.
enum IAData {
    static let now = "2026-11-16T12:00:00Z".apiDate!

    /// A time before `now`, as the Mac writes it.
    static func ago(_ seconds: TimeInterval) -> String {
        ISO8601DateFormatter().string(from: now.addingTimeInterval(-seconds))
    }

    static func bot(_ id: String, unread: Int = 0, pinned: Bool = false, archived: Bool = false,
                    health: BotHealth? = nil, coconut: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id.capitalized, role: "Role of \(id)", avatar: "sparkle",
            color: "777777", instructions: "", status: "idle", lastMessage: "", unreadCount: unread,
            archived: archived, createdAt: "2026-11-01T00:00:00Z", pinned: pinned, health: health, coconut: coconut)
    }

    static func decision(_ id: String, bot: String = "planner", status: String = "pending", created: String) -> Decision {
        Decision(id: id, botId: bot, taskId: "task-\(id)", title: "Ship \(id)?", explanation: "", actionType: "ask",
                 resource: "", consequences: "", recommended: "approve", options: [], status: status,
                 urgency: "normal", createdAt: created, decidedAt: nil, decidedBy: nil, correction: nil, outcome: nil,
                 revision: 1)
    }

    static func work(_ id: String, _ status: String, bot: String = "planner", task: String? = nil,
                     created: String, kind: String = "work") -> Artifact {
        Artifact(id: id, botId: bot, taskId: task, title: "Work \(id)", kind: kind, status: status, preview: "",
                 url: nil, createdAt: created)
    }

    static func task(_ id: String, _ status: String, bot: String = "planner", updated: String) -> AgentTask {
        AgentTask(id: id, botId: bot, sessionId: "session", title: "Task \(id)", status: status, summary: "",
                  createdAt: updated, updatedAt: updated, scheduledAt: nil, threadId: "thread-\(id)")
    }

    static func notice(_ id: String, _ kind: NoticeKind, seen: Bool = true, active: Bool = true,
                       ends: String? = nil) -> WorkspaceNotice {
        WorkspaceNotice(id: id, type: kind.rawValue, text: "Notice \(id)", postedAt: "2026-11-16T09:00:00Z",
                        endsAt: ends, closed: !active, active: active, author: "m2", authorName: "Grace", own: false,
                        seen: seen, seenBy: [])
    }

    static func room(_ id: String, unread: Int = 0, last: String? = nil, notices: [WorkspaceNotice]? = nil,
                     state: String = "active") -> TeamRoom {
        TeamRoom(room: id, name: "Team \(id)", state: state, createdAt: "2026-11-02T00:00:00Z", unread: unread,
                 lastMessage: last.map { TeamLastMessage(text: "Hi", createdAt: $0) }, notices: notices)
    }
}

/// The rules of "Needs attention" on Home: what breaches, what does not, and where each row leads.
final class AttentionRulesTests: XCTestCase {
    private let now = IAData.now

    private func kinds(_ input: AttentionRules.Input) -> [AttentionItem.Kind] {
        AttentionRules.items(input, now: now).map(\.kind)
    }

    func testAClearDayShowsNoRow() {
        var input = AttentionRules.Input()
        input.bots = [IAData.bot("planner")]
        input.decisions = [IAData.decision("fresh", created: IAData.ago(60))]
        input.artifacts = [IAData.work("w", "working", created: IAData.ago(60))]
        input.lastSync = now.addingTimeInterval(-30)
        input.rooms = [IAData.room("r", notices: [IAData.notice("seen", .status), IAData.notice("rule", .rule, seen: false)])]
        XCTAssertEqual(AttentionRules.items(input, now: now), [])
    }

    func testTheThresholdsHaveTheirNames() {
        XCTAssertEqual(AttentionRules.cardWait, 4 * 3600)
        XCTAssertEqual(AttentionRules.workWindow, 24 * 3600)
        XCTAssertEqual(AttentionRules.staleSync, 600)
        XCTAssertEqual(AttentionRules.deadlineWindow, 24 * 3600)
    }

    func testACardThatWaitsPastFourHoursBreaches() {
        var input = AttentionRules.Input()
        input.decisions = [
            IAData.decision("at-limit", created: IAData.ago(AttentionRules.cardWait)),
            IAData.decision("old", created: IAData.ago(AttentionRules.cardWait + 60)),
            IAData.decision("older", created: IAData.ago(9 * 3600)),
            IAData.decision("answered", status: "approved", created: IAData.ago(9 * 3600)),
            IAData.decision("answering", created: IAData.ago(9 * 3600)),
        ]
        input.answering = ["answering"]
        XCTAssertEqual(AttentionRules.overdueCards(input, now: now).map(\.id), ["older", "old"], "Oldest first.")
        let item = try? XCTUnwrap(AttentionRules.items(input, now: now).first)
        XCTAssertEqual(item?.kind, .waiting)
        XCTAssertEqual(item?.count, 2)
        XCTAssertEqual(item?.detail, "Past 4h · oldest 9h")
        XCTAssertEqual(item?.target, .cards("older"), "The row opens the card that waits longest.")
    }

    func testAnAgentWithAProviderLimitCannotRun() {
        let limit = BotHealth(state: "rate_limited", errorCode: nil, until: nil, uncovered: 2)
        var input = AttentionRules.Input()
        input.bots = [IAData.bot("planner", health: limit), IAData.bot("writer"),
                      IAData.bot("old", archived: true, health: limit)]
        var item = AttentionRules.items(input, now: now).first
        XCTAssertEqual(item?.kind, .agents)
        XCTAssertEqual(item?.count, 1)
        XCTAssertEqual(item?.detail, "Planner · Rate limited · 2 waiting")
        XCTAssertEqual(item?.target, .agent("planner"), "One agent opens its page.")
        input.bots[1].health = BotHealth(state: "error", errorCode: "500", until: nil, uncovered: 0)
        item = AttentionRules.items(input, now: now).first
        XCTAssertEqual(item?.count, 2)
        XCTAssertEqual(item?.target, .agents, "More agents open the list.")
    }

    func testBlockedAndFailedWorkOfTheLastDayBreaches() {
        var input = AttentionRules.Input()
        input.bots = [IAData.bot("planner"), IAData.bot("old", archived: true)]
        input.artifacts = [
            IAData.work("blocked", "blocked", task: "t1", created: IAData.ago(3600)),
            IAData.work("stale-block", "blocked", created: IAData.ago(AttentionRules.workWindow + 60)),
            IAData.work("running", "working", created: IAData.ago(60)),
            IAData.work("archived", "blocked", bot: "old", created: IAData.ago(60)),
            IAData.work("pr", "failed", created: IAData.ago(60), kind: "pr"),
        ]
        input.tasks = [
            IAData.task("t1", "failed", updated: IAData.ago(1800)),
            IAData.task("t2", "failed", updated: IAData.ago(600)),
            IAData.task("t3", "failed", updated: IAData.ago(AttentionRules.workWindow + 60)),
            IAData.task("t4", "completed", updated: IAData.ago(60)),
        ]
        let stuck = AttentionRules.stuckWork(input, now: now)
        XCTAssertEqual(stuck.map { $0.workContext?.id ?? "task" }, ["task", "blocked"],
                       "Newest first. A To-do and its task count one time.")
        let item = AttentionRules.items(input, now: now).first
        XCTAssertEqual(item?.kind, .work)
        XCTAssertEqual(item?.count, 2)
        XCTAssertEqual(item?.target, .conversation(ConversationRoute(botId: "planner")))
    }

    func testStaleDataBreaches() {
        let fresh = now.addingTimeInterval(-AttentionRules.staleSync)
        let old = now.addingTimeInterval(-AttentionRules.staleSync - 1)
        XCTAssertFalse(AttentionRules.isStale(connection: .connected, lastSync: fresh, now: now))
        XCTAssertTrue(AttentionRules.isStale(connection: .connected, lastSync: old, now: now))
        for state in [ConnectionState.disconnected, .macOffline, .relayUnreachable, .needsRepair] {
            XCTAssertTrue(AttentionRules.isStale(connection: state, lastSync: now, now: now), state.rawValue)
        }
        for state in [ConnectionState.unconfigured, .preview, .connecting] {
            XCTAssertFalse(AttentionRules.isStale(connection: state, lastSync: nil, now: now), state.rawValue)
        }
        var input = AttentionRules.Input()
        input.connection = .macOffline
        input.lastSync = now.addingTimeInterval(-14 * 60)
        let item = AttentionRules.items(input, now: now).first
        XCTAssertEqual(item?.kind, .stale)
        XCTAssertEqual(item?.value, "14m", "The row shows the age of the data in the place of a count.")
        XCTAssertEqual(item?.detail, "Mac offline · last sync 14m ago")
        XCTAssertEqual(item?.target, .connection)
    }

    func testUnseenStatusAndCloseDeadlinesBreach() {
        let soon = ISO8601DateFormatter().string(from: now.addingTimeInterval(3 * 3600 + 5 * 60))
        let far = ISO8601DateFormatter().string(from: now.addingTimeInterval(3 * 86_400))
        var input = AttentionRules.Input()
        input.rooms = [
            IAData.room("r1", notices: [
                IAData.notice("status-unseen", .status, seen: false),
                IAData.notice("status-seen", .status),
                IAData.notice("deadline-soon", .deadline, ends: soon),
                IAData.notice("deadline-far", .deadline, ends: far),
                IAData.notice("deadline-ended", .deadline, seen: false, active: false, ends: far),
                IAData.notice("announcement", .announcement, seen: false),
            ]),
            IAData.room("left", notices: [IAData.notice("gone", .status, seen: false)], state: "removed"),
        ]
        XCTAssertEqual(AttentionRules.urgentNotices(input, now: now).map(\.notice.id), ["status-unseen", "deadline-soon"])
        let item = AttentionRules.items(input, now: now).first
        XCTAssertEqual(item?.kind, .notices)
        XCTAssertEqual(item?.count, 2)
        XCTAssertEqual(item?.detail, "Ends in 3h 05m · Notice deadline-soon")
        XCTAssertEqual(item?.target, .notices("r1"))
    }

    func testTheRowsKeepOneOrderAndThePillAddsThem() {
        var input = AttentionRules.Input()
        input.bots = [IAData.bot("planner", health: BotHealth(state: "error", errorCode: nil, until: nil, uncovered: 0))]
        input.decisions = [IAData.decision("a", created: IAData.ago(5 * 3600)), IAData.decision("b", created: IAData.ago(6 * 3600))]
        input.artifacts = [IAData.work("w", "blocked", created: IAData.ago(60))]
        input.connection = .disconnected
        input.rooms = [IAData.room("r", notices: [IAData.notice("n", .status, seen: false)])]
        let items = AttentionRules.items(input, now: now)
        XCTAssertEqual(items.map(\.kind), [.waiting, .agents, .work, .stale, .notices])
        XCTAssertEqual(AttentionRules.total(items), 6)
    }

    func testAShortAge() {
        XCTAssertEqual(AttentionRules.age(59), "0m")
        XCTAssertEqual(AttentionRules.age(14 * 60), "14m")
        XCTAssertEqual(AttentionRules.age(5 * 3600 + 59 * 60), "5h")
        XCTAssertEqual(AttentionRules.age(3 * 86_400), "3d")
    }
}
