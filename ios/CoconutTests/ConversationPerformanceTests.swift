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

    #if os(macOS)
    /// The sidebar sorts its bots in one pass over the messages. The order must stay the order of a scan for each bot.
    @MainActor
    func testTheMacSidebarKeepsTheOrderOfAScanForEachBot() throws {
        let store = AppStore(demo: true)
        var bots = store.bots
        XCTAssertGreaterThanOrEqual(bots.count, 3)
        bots[0].pinned = true
        bots[bots.count - 1].pinned = true
        bots[1].archived = true
        var quiet = bots[2]
        quiet.id = "quiet"
        quiet.createdAt = "2000-01-01T00:00:00Z"
        bots.append(quiet)
        var pinnedQuiet = quiet
        pinnedQuiet.id = "pinned-quiet"
        pinnedQuiet.pinned = true
        bots.append(pinnedQuiet)
        var messages = store.messages
        let template = try XCTUnwrap(messages.first)
        var undated = template
        undated.id = "undated"
        undated.createdAt = "invalid"
        messages.append(undated)

        func scan(_ bot: Bot) -> Date {
            messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
                ?? bot.createdAt.apiDate ?? .distantPast
        }
        let active = bots.filter { !$0.archived }.sorted { scan($0) > scan($1) }
        let sections = MacSidebar.sections(bots: bots, messages: messages)
        XCTAssertEqual(sections.pinned.map(\.id), active.filter(\.pinned).map(\.id))
        XCTAssertEqual(sections.others.map(\.id), active.filter { !$0.pinned }.map(\.id))
        XCTAssertFalse(sections.pinned.isEmpty)
        XCTAssertFalse(sections.others.isEmpty)
    }
    #endif
}
