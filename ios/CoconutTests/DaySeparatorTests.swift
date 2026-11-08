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

    private func message(_ id: String, at createdAt: String, photo: Bool = false, exchange: [String]? = nil) -> Message {
        Message(id: id, botId: "bot", role: "assistant", kind: exchange == nil ? "text" : "bot_exchange",
                text: photo ? "Room\nhttps://example.com/\(id).jpg" : "Body \(id)", createdAt: createdAt,
                streaming: false, senderBotId: "bot", recipientBotIds: exchange?.filter { $0 != "bot" } ?? [],
                exchangeRecipientBotIds: exchange)
    }

    func testLabels() {
        let now = date("2026-11-08T17:00:00Z")
        func label(_ s: String) -> String { DaySeparator.label(for: date(s), now: now, calendar: calendar, locale: locale) }
        XCTAssertEqual(label("2026-11-08T06:00:00Z"), "Today")
        XCTAssertEqual(label("2026-11-07T20:00:00Z"), "Yesterday")
        XCTAssertEqual(label("2026-11-04T20:00:00Z"), "Wednesday")
        XCTAssertEqual(label("2026-10-01T20:00:00Z"), "Oct 1")
        XCTAssertEqual(label("2025-11-05T20:00:00Z"), "Nov 5, 2025")
        // A late evening in the calendar's zone stays on that day, whatever zone the Mac runs in.
        XCTAssertEqual(label("2026-11-07T02:00:00Z"), "Friday")
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

    /// 23:59 and 00:01 in New York are 04:59Z and 05:01Z on these dates.
    func testPhotosAndExchangesSplitAtMidnight() {
        let acrossMidnight = TranscriptEntry.grouped([
            message("photo-1", at: "2026-11-09T04:59:00Z", photo: true),
            message("photo-2", at: "2026-11-09T05:01:00Z", photo: true),
        ], calendar: calendar)
        XCTAssertEqual(acrossMidnight.map(\.id), ["photo-1", "photo-2"])

        let sameDay = TranscriptEntry.grouped([
            message("photo-3", at: "2026-11-09T05:01:00Z", photo: true),
            message("photo-4", at: "2026-11-09T05:02:00Z", photo: true),
        ], calendar: calendar)
        XCTAssertEqual(sameDay.map(\.id), ["photo-3"])
        XCTAssertEqual(sameDay[0].photos.count, 2)

        let exchange = TranscriptEntry.grouped([
            message("ask", at: "2026-11-09T04:59:00Z", exchange: ["bot", "peer"]),
            message("reply", at: "2026-11-09T05:01:00Z", exchange: ["bot", "peer"]),
        ], calendar: calendar)
        XCTAssertEqual(exchange.map(\.id), ["ask-exchange", "reply-exchange"])
    }

    /// Each day with messages has exactly one separator, even when a carousel crosses midnight.
    func testOneSeparatorPerDayThroughGroupedRows() {
        let entries = TranscriptEntry.grouped([
            message("photo-1", at: "2026-11-09T04:59:00Z", photo: true),
            message("photo-2", at: "2026-11-09T05:01:00Z", photo: true),
            message("note", at: "2026-11-09T05:02:00Z"),
        ], calendar: calendar)
        var previous: Date?
        var separatorDays: [Date] = []
        for entry in entries {
            let day = entry.message.createdAt.apiDate
            if let day, DaySeparator.starts(day, after: previous, calendar: calendar) {
                separatorDays.append(calendar.startOfDay(for: day))
            }
            previous = day
        }
        let messageDays = Set(entries.compactMap(\.message.createdAt.apiDate).map { calendar.startOfDay(for: $0) })
        XCTAssertEqual(messageDays.count, 2)
        XCTAssertEqual(separatorDays, messageDays.sorted())
    }
}
