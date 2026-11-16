import XCTest
@testable import Coconut

/// The line under the newest message shows the time of the turn to the second, and the model.
final class ActivityDetailTests: XCTestCase {
    func testTheTimeOfATurnCountsSeconds() {
        XCTAssertEqual(ConversationActivity.elapsed(0), "0s")
        XCTAssertEqual(ConversationActivity.elapsed(9.7), "9s")
        XCTAssertEqual(ConversationActivity.elapsed(245), "4m 05s")
        XCTAssertEqual(ConversationActivity.elapsed(3725), "1h 02m 05s")
    }

    @MainActor
    func testTheLineShowsTheTimeAndTheModel() {
        let start = Date(timeIntervalSince1970: 1_000)
        let activity = ConversationActivity(phase: .command, startedAt: start, reconnecting: false,
                                            detail: "pytest backend/tests", model: "Claude Opus 5.5")
        let row = BotStatusRow(bot: Bot(id: "b", sessionId: "s", name: "Scout", role: "", avatar: "sparkle",
                                          color: "777777", instructions: "", status: "working", lastMessage: "",
                                          unreadCount: 0, archived: false, createdAt: "2026-09-08T12:00:00Z"),
                               activity: activity)
        XCTAssertEqual(row.meta(at: start.addingTimeInterval(74)), "· 1m 14s · Claude Opus 5.5")
    }
}
