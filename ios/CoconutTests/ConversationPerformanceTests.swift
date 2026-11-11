import XCTest
@testable import Coconut

final class ConversationPerformanceTests: XCTestCase {
    @MainActor
    func testLongHistoryPreviewAvoidsPerMessageFormatterSetup() throws {
        let store = AppStore(demo: true)
        let template = try XCTUnwrap(store.messages.first { $0.kind == "text" })
        let messages = (0..<2_000).map { index in
            var message = template
            message.id = "history-\(index)"
            message.role = "assistant"
            message.text = "History \(index)"
            message.createdAt = index == 1_999 ? "2026-09-13T13:00:00+01:00" : "2026-09-12T12:00:00.123456Z"
            return message
        }
        let start = ContinuousClock.now
        let latest = latestConversationMessage(in: messages)
        let elapsed = start.duration(to: .now)
        XCTAssertEqual(latest?.id, "history-1999")
        // Leave room for a loaded simulator while detecting formatter allocation per comparison.
        XCTAssertLessThan(elapsed, .milliseconds(200))
        XCTAssertEqual("2026-09-13T13:00:00+01:00".apiDate, "2026-09-13T12:00:00Z".apiDate)
        XCTAssertNotNil("2026-09-12T12:00:00.123456Z".apiDate)
        XCTAssertNil("invalid".apiDate)
        print("COCONUT_PREVIEW_DURATION=\(elapsed)")
    }

    /// The sidebar of the Mac sorts its bots in one pass over the messages. The order must stay the order of a scan for
    /// each bot. The sections are shared code, so the iPhone tests run it too.
    func testTheMacSidebarKeepsTheOrderOfAScanForEachBot() {
        // The test makes its own bots and messages, so a change of the demo data cannot change it.
        func bot(_ id: String, created: String, pinned: Bool = false, archived: Bool = false) -> Bot {
            Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
                instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: archived,
                createdAt: created, pinned: pinned)
        }
        func message(_ id: String, bot: String, at date: String) -> Message {
            Message(id: id, botId: bot, role: "user", kind: "text", text: id, createdAt: date, streaming: false)
        }
        let bots = [
            bot("desk", created: "2026-11-01T12:00:00Z", pinned: true),
            bot("forge", created: "2026-11-01T12:00:00Z"),
            bot("scout", created: "2026-11-06T12:00:00Z"),
            bot("old", created: "2026-11-02T12:00:00Z", pinned: true),
            bot("gone", created: "2026-11-01T12:00:00Z", archived: true),
            bot("quiet", created: "invalid"),
            bot("news", created: "2026-11-01T12:00:00Z", pinned: true),
        ]
        let messages = [
            message("d1", bot: "desk", at: "2026-11-05T12:00:00Z"),
            message("f1", bot: "forge", at: "2026-11-07T12:00:00Z"),
            message("d2", bot: "desk", at: "2026-11-03T12:00:00Z"),
            message("f2", bot: "forge", at: "invalid"),
            message("g1", bot: "gone", at: "2026-11-09T12:00:00Z"),
            message("n1", bot: "news", at: "2026-11-08T12:00:00Z"),
        ]

        func scan(_ bot: Bot) -> Date {
            messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
                ?? bot.createdAt.apiDate ?? .distantPast
        }
        let active = bots.filter { !$0.archived }.sorted { scan($0) > scan($1) }
        let sections = ChatSections.make(bots: bots, messages: messages, rooms: [])
        XCTAssertEqual(sections.pinned.map(\.id), ["news", "desk", "old"])
        XCTAssertEqual(sections.bots.map(\.id), ["forge", "scout", "quiet"])
        XCTAssertEqual(sections.pinned.map(\.id), active.filter(\.pinned).map(\.id))
        XCTAssertEqual(sections.bots.map(\.id), active.filter { !$0.pinned }.map(\.id))
        XCTAssertTrue(sections.people.isEmpty, "A Mac without a team has no People section.")
    }
}
