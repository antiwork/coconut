import XCTest
@testable import Coconut

/// Each message shows its time by default, with the day when it is not today.
final class MessageTimesTests: XCTestCase {
    func testTheLabelNamesTheDayWhenItIsNotToday() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let now = calendar.date(from: DateComponents(year: 2026, month: 11, day: 16, hour: 17))!
        let time = { (date: Date) in date.formatted(date: .omitted, time: .shortened) }
        let today = now.addingTimeInterval(-3600)
        XCTAssertEqual(MessageTimes.label(today, now: now, calendar: calendar), time(today))
        let yesterday = now.addingTimeInterval(-86_400)
        XCTAssertEqual(MessageTimes.label(yesterday, now: now, calendar: calendar), "Yesterday " + time(yesterday))
        let older = now.addingTimeInterval(-20 * 86_400)
        XCTAssertTrue(MessageTimes.label(older, now: now, calendar: calendar).hasSuffix(", " + time(older)))
        XCTAssertTrue(MessageTimes.exact(today).contains(":"))
    }
}
