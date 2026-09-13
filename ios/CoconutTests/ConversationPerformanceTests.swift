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
}
