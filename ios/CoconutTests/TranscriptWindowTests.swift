import XCTest
@testable import Coconut

final class TranscriptWindowTests: XCTestCase {
    private func ids(_ count: Int, from first: Int = 0) -> [String] { (first..<(first + count)).map { "m\($0)" } }

    func testALargeChatOpensWithTheNewestRowsInOneStep() {
        var window = TranscriptWindow(isUsed: true)
        let rows = ids(1_270)
        XCTAssertEqual(window.range(of: rows), (1_270 - TranscriptWindow.openRows)..<1_270)
        // Keeping the window changes no row.
        window.keep(rows)
        XCTAssertEqual(window.range(of: rows), (1_270 - TranscriptWindow.openRows)..<1_270)
    }

    func testAChatWithFewRowsBuildsAllOfThem() {
        var window = TranscriptWindow(isUsed: true)
        XCTAssertEqual(window.range(of: ids(5)), 0..<5)
        XCTAssertEqual(window.range(of: ids(TranscriptWindow.openRows)), 0..<TranscriptWindow.openRows)
        XCTAssertEqual(window.range(of: []), 0..<0)
        window.keep([])
        XCTAssertEqual(window.range(of: ids(5)), 0..<5)
    }

    func testAPressAddsOneBatchAboveTheWindow() {
        var window = TranscriptWindow(isUsed: true)
        let rows = ids(1_270)
        window.keep(rows)
        let before = window.range(of: rows).lowerBound
        XCTAssertTrue(window.growTop(in: rows))
        XCTAssertEqual(window.range(of: rows), (before - TranscriptWindow.pressRows)..<1_270)
        // The last press stops at the first row, and then a press adds nothing.
        var short = TranscriptWindow(isUsed: true)
        let few = ids(TranscriptWindow.openRows + 10)
        short.keep(few)
        XCTAssertTrue(short.growTop(in: few))
        XCTAssertEqual(short.range(of: few), 0..<few.count)
        XCTAssertFalse(short.growTop(in: few))
    }

    func testAPageFromTheMacStaysAboveTheWindowUntilAPress() {
        var window = TranscriptWindow(isUsed: true)
        var rows = ids(10)
        window.keep(rows)
        rows = ids(100, from: 1_000) + rows
        XCTAssertEqual(window.range(of: rows), 100..<110)
        XCTAssertTrue(window.growTop(in: rows))
        XCTAssertEqual(window.range(of: rows), (100 - TranscriptWindow.pressRows)..<110)
    }

    func testANewRowAtTheBottomKeepsTheFirstRow() {
        var window = TranscriptWindow(isUsed: true)
        var rows = ids(300)
        window.keep(rows)
        let first = window.range(of: rows).lowerBound
        rows.append("new")
        XCTAssertEqual(window.range(of: rows), first..<301)
    }

    func testAnOpenAtAnOlderMessageBuildsFromAFewRowsAboveIt() {
        var window = TranscriptWindow(isUsed: true)
        let rows = ids(1_270)
        XCTAssertEqual(window.range(of: rows, focusId: "m100"), (100 - TranscriptWindow.contextRows)..<1_270)
        XCTAssertEqual(window.range(of: rows, focusId: "m2"), 0..<1_270)
        // A focus inside the window changes nothing, and an unknown focus is ignored.
        XCTAssertEqual(window.range(of: rows, focusId: "m1269"), window.range(of: rows))
        XCTAssertEqual(window.range(of: rows, focusId: "gone"), window.range(of: rows))
        // The window keeps the rows of the focus.
        window.keep(rows, focusId: "m100")
        XCTAssertEqual(window.range(of: rows), (100 - TranscriptWindow.contextRows)..<1_270)
    }

    func testAJumpToAQuotedMessageWidensTheWindowToIt() {
        var window = TranscriptWindow(isUsed: true)
        let rows = ids(1_270)
        window.keep(rows)
        XCTAssertTrue(window.include("m500", in: rows))
        XCTAssertEqual(window.range(of: rows), (500 - TranscriptWindow.contextRows)..<1_270)
        XCTAssertFalse(window.include("m600", in: rows))
        XCTAssertFalse(window.include("gone", in: rows))
    }

    func testAPressGrowsTheWindowOnlyWhileTheAppHasOlderRows() {
        var window = TranscriptWindow(isUsed: true)
        let rows = ids(120)
        window.keep(rows)
        XCTAssertEqual(window.press(rows), .growWindow)
        window.growTop(in: rows)
        XCTAssertEqual(window.press(rows), .loadPage)
    }

    /// An iPhone list builds every row, so a press always asks the Mac for a page, also with more rows than a Mac window.
    func testOnTheIPhoneAPressAlwaysLoadsAPageFromTheMac() {
        var window = TranscriptWindow(isUsed: false)
        let rows = ids(120)
        window.keep(rows)
        XCTAssertEqual(window.range(of: rows), 0..<120)
        XCTAssertEqual(window.press(rows), .loadPage)
        XCTAssertFalse(window.growTop(in: rows))
        XCTAssertFalse(window.include("m0", in: rows))
        XCTAssertEqual(window.range(of: rows, focusId: "m100"), 0..<120)
    }

    /// The window of the platform where the tests run: the iPhone builds every row, the Mac builds a window.
    func testThePlatformDecidesWhetherTheWindowIsUsed() {
        var window = TranscriptWindow()
        let rows = ids(120)
        window.keep(rows)
        #if os(macOS)
        XCTAssertEqual(window.press(rows), .growWindow)
        #else
        XCTAssertEqual(window.range(of: rows), 0..<120)
        XCTAssertEqual(window.press(rows), .loadPage)
        #endif
    }

    func testARemovedFirstRowCountsTheWindowFromTheEnd() {
        var window = TranscriptWindow(isUsed: true)
        var rows = ids(300)
        window.keep(rows)
        window.growTop(in: rows)
        let first = rows[window.range(of: rows).lowerBound]
        rows.removeAll { $0 == first }
        XCTAssertEqual(window.range(of: rows), (299 - TranscriptWindow.openRows)..<299)
    }
}
