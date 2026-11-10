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

    /// Three rows of a chat, 80 points high with 10 points between them. The third row starts the second day.
    /// `scrolled` moves them up, as a scroll of the chat does. The pinned pill is 50 points high, at 100 points.
    @MainActor
    private func pinnedDay(scrolled offset: CGFloat, thirdRowAt third: CGFloat = 180) -> PinnedDay {
        let pinned = PinnedDay()
        pinned.line = 100
        pinned.height = 50
        let first = date("2026-11-07T14:00:00Z"), second = date("2026-11-08T14:00:00Z")
        for (id, top, day, starts) in [("a", 0.0, first, true), ("b", 90.0, first, false), ("c", third, second, true)] {
            pinned.place(id, frame: CGRect(x: 0, y: top + 140 - offset, width: 300, height: 80), date: day, startsDay: starts)
        }
        return pinned
    }

    @MainActor
    func testPinnedDayIsTheDayOfTheTopRow() {
        // The first row is below the line: the chat is at its start, and no day is pinned.
        XCTAssertNil(pinnedDay(scrolled: 0).day)

        // The first row passes the line. Its day is pinned, and its own pill gives its place to the pinned pill.
        let start = pinnedDay(scrolled: 80)
        XCTAssertEqual(start.day, date("2026-11-07T14:00:00Z"))
        XCTAssertEqual(start.source, "a")
        XCTAssertEqual(start.push, 0)

        // The second row is at the top, and the first row of the next day is 20 points below the line. That row
        // pushes the pinned pill up by the 30 points that the pill does not have, and the pill fades.
        let pushed = pinnedDay(scrolled: 200)
        XCTAssertEqual(pushed.day, date("2026-11-07T14:00:00Z"))
        XCTAssertEqual(pushed.source, "a")
        XCTAssertEqual(pushed.push, -30)
        XCTAssertEqual(pushed.strength, 0.4, accuracy: 0.001)

        // The first row of the next day reaches the line. The next day is pinned.
        let next = pinnedDay(scrolled: 220)
        XCTAssertEqual(next.day, date("2026-11-08T14:00:00Z"))
        XCTAssertEqual(next.source, "c")
        XCTAssertEqual(next.push, 0)
    }

    /// A row that is not a message is at the line, between the second row and the third row. No day is pinned.
    @MainActor
    func testNoDayIsPinnedBetweenMessages() {
        XCTAssertNil(pinnedDay(scrolled: 240, thirdRowAt: 260).day)
    }

    @MainActor
    func testPinnedPillShowsDuringAScrollAndOneSecondAfterIt() async throws {
        let pinned = pinnedDay(scrolled: 80)
        XCTAssertFalse(pinned.standsIn(for: "a"))
        pinned.scrolls(true)
        XCTAssertTrue(pinned.standsIn(for: "a"))
        XCTAssertFalse(pinned.standsIn(for: "b"))
        pinned.scrolls(false)
        XCTAssertTrue(pinned.isVisible)
        try await Task.sleep(for: .milliseconds(1300))
        XCTAssertFalse(pinned.isVisible)
    }
}
