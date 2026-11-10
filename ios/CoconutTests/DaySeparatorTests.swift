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

    /// Each day with messages has exactly one row that starts it. The chat shows the separator above that row.
    func testOneRowStartsEachDay() {
        let entries = TranscriptEntry.grouped([
            message("a", at: "2026-11-07T14:00:00Z"), message("b", at: "2026-11-07T15:00:00Z"),
            message("c", at: "2026-11-08T14:00:00Z"), message("d", at: "2026-11-08T15:00:00Z"),
        ], calendar: calendar)
        XCTAssertEqual(entries.map(\.startsDay), [true, false, true, false])
    }

    /// A message without a date starts no day, and the message after it does not start the same day again.
    func testRowWithoutDateStartsNoDay() {
        let entries = TranscriptEntry.grouped([
            message("a", at: "2026-11-08T14:00:00Z"), message("b", at: ""), message("c", at: "2026-11-08T15:00:00Z"),
        ], calendar: calendar)
        XCTAssertEqual(entries.map(\.startsDay), [true, false, false])
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

    /// A carousel that crosses midnight gives the new day a row of its own, and that row starts the day.
    func testRowAfterMidnightStartsTheNewDay() {
        let entries = TranscriptEntry.grouped([
            message("photo-1", at: "2026-11-09T04:59:00Z", photo: true),
            message("photo-2", at: "2026-11-09T05:01:00Z", photo: true),
            message("note", at: "2026-11-09T05:02:00Z"),
        ], calendar: calendar)
        XCTAssertEqual(entries.map(\.id), ["photo-1", "photo-2", "note"])
        XCTAssertEqual(entries.map(\.startsDay), [true, true, false])
    }

    /// The rows group by the days of the calendar that the chat passes, so a new time zone moves the start of a day.
    /// 04:30Z and 05:30Z are 23:30 and 00:30 in New York, and 20:30 and 21:30 in Los Angeles.
    func testTimeZoneMovesTheStartOfDay() {
        let photos = [message("photo-1", at: "2026-11-09T04:30:00Z", photo: true),
                      message("photo-2", at: "2026-11-09T05:30:00Z", photo: true)]
        var west = calendar
        west.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        XCTAssertEqual(TranscriptEntry.grouped(photos, calendar: calendar).map(\.startsDay), [true, true])
        XCTAssertEqual(TranscriptEntry.grouped(photos, calendar: west).map(\.startsDay), [true])
        // The chat groups its rows again when the calendar is not equal to the one it used.
        XCTAssertNotEqual(calendar, west)
    }
}
