import XCTest
@testable import Coconut

final class DaySeparatorTests: XCTestCase {
    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/New_York")!
        return c
    }
    private let locale = Locale(identifier: "en_US")

    private func date(_ s: String) -> Date { s.apiDate! }

    func testLabels() {
        let now = date("2026-11-08T17:00:00Z")
        func label(_ s: String) -> String { DaySeparator.label(for: date(s), now: now, calendar: calendar, locale: locale) }
        XCTAssertEqual(label("2026-11-08T06:00:00Z"), "Today")
        XCTAssertEqual(label("2026-11-07T20:00:00Z"), "Yesterday")
        XCTAssertEqual(label("2026-11-04T20:00:00Z"), "Wednesday")
        XCTAssertEqual(label("2026-10-01T20:00:00Z"), "Oct 1")
        XCTAssertEqual(label("2025-11-05T20:00:00Z"), "Nov 5, 2025")
    }

    func testLabelChangesAfterMidnight() {
        let sent = date("2026-11-08T17:00:00Z")
        XCTAssertEqual(DaySeparator.label(for: sent, now: sent, calendar: calendar, locale: locale), "Today")
        XCTAssertEqual(DaySeparator.label(for: sent, now: date("2026-11-09T06:00:00Z"), calendar: calendar, locale: locale), "Yesterday")
    }

    func testOneSeparatorPerDay() {
        let times = ["2026-11-07T14:00:00Z", "2026-11-07T15:00:00Z", "2026-11-08T14:00:00Z", "2026-11-08T15:00:00Z"].map(date)
        var previous: Date?
        var starts = 0
        for t in times {
            if DaySeparator.starts(t, after: previous, calendar: calendar) { starts += 1 }
            previous = t
        }
        XCTAssertEqual(starts, 2)
    }
}
