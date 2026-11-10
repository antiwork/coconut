import XCTest
@testable import Coconut

final class TranscriptWindowTests: XCTestCase {
    private func ids(_ count: Int, from first: Int = 0) -> [String] { (first..<(first + count)).map { "m\($0)" } }

    func testOpenAtTheEndBuildsTheRowsThatFillTheViewThenTheSettledWindow() {
        var window = TranscriptWindow()
        let rows = ids(1_270)
        XCTAssertEqual(window.range(of: rows), (1_270 - TranscriptWindow.openRows)..<1_270)
        XCTAssertTrue(window.hasRowsAbove(rows))
        window.settle()
        XCTAssertEqual(window.range(of: rows), (1_270 - TranscriptWindow.settledRows)..<1_270)
        // A second settle changes nothing.
        window.settle()
        XCTAssertEqual(window.range(of: rows).count, TranscriptWindow.settledRows)
    }

    func testOpenAtAnOlderMessageHoldsItAndTheRowsBeforeIt() {
        var window = TranscriptWindow()
        let rows = ids(1_270)
        XCTAssertEqual(window.range(of: rows, focusId: "m100"), (100 - TranscriptWindow.contextRows)..<1_270)
        XCTAssertEqual(window.range(of: rows, focusId: "m2"), 0..<1_270)
        // A focus inside the window changes nothing, and an unknown focus is ignored.
        XCTAssertEqual(window.range(of: rows, focusId: "m1269"), window.range(of: rows))
        XCTAssertEqual(window.range(of: rows, focusId: "gone"), window.range(of: rows))
        // A reply quote that jumps to an older message widens the window to it.
        XCTAssertTrue(window.include("m500", in: rows))
        XCTAssertEqual(window.range(of: rows).lowerBound, 500 - TranscriptWindow.contextRows)
        XCTAssertFalse(window.include("m600", in: rows))
        XCTAssertFalse(window.include("gone", in: rows))
    }

    func testGrowAtTheTopAddsABatchUntilTheFirstRow() {
        var window = TranscriptWindow()
        window.settle()
        let rows = ids(130)
        XCTAssertTrue(window.growTop(in: rows))
        XCTAssertEqual(window.range(of: rows), (130 - TranscriptWindow.settledRows - TranscriptWindow.batchRows)..<130)
        // Each further grow adds one batch, and the last one stops at the first row.
        var grows = 1
        while window.growTop(in: rows) { grows += 1 }
        let above = 130 - TranscriptWindow.settledRows
        XCTAssertEqual(grows, (above + TranscriptWindow.batchRows - 1) / TranscriptWindow.batchRows)
        XCTAssertEqual(window.range(of: rows), 0..<130)
        XCTAssertFalse(window.hasRowsAbove(rows))
        XCTAssertFalse(window.growTop(in: rows))
    }

    func testAChatShorterThanAScreenBuildsAllRows() {
        var window = TranscriptWindow()
        XCTAssertEqual(window.range(of: ids(5)), 0..<5)
        XCTAssertEqual(window.range(of: []), 0..<0)
        XCTAssertFalse(window.hasRowsAbove(ids(5)))
        XCTAssertFalse(window.growTop(in: ids(5)))
        window.settle()
        XCTAssertEqual(window.range(of: ids(30)), 0..<30)
    }

    func testANewMessageWhileTheReaderReadsOlderRowsKeepsThoseRows() {
        var window = TranscriptWindow()
        window.settle()
        var rows = ids(300)
        window.growTop(in: rows)
        let first = window.range(of: rows).lowerBound
        rows.append("new")
        // The window holds the same oldest row and the new row at the bottom.
        XCTAssertEqual(window.range(of: rows), first..<301)
        // A page of older history from the Mac also leaves the rows of the window where they are.
        rows = ids(50, from: 1_000) + rows
        XCTAssertEqual(rows[window.range(of: rows).lowerBound], "m\(first)")
    }

    func testAPinnedWindowKeepsAPageOfHistoryAboveIt() {
        var window = TranscriptWindow()
        window.settle()
        var rows = ids(10)
        window.pinStart(in: rows)
        rows = ids(50, from: 1_000) + rows
        // The page from the Mac stays above the window until the window grows.
        XCTAssertEqual(window.range(of: rows), 50..<60)
        XCTAssertTrue(window.growTop(in: rows))
        XCTAssertEqual(window.range(of: rows), (50 - TranscriptWindow.batchRows)..<60)
    }

    func testARemovedFirstRowCountsTheWindowFromTheEnd() {
        var window = TranscriptWindow()
        window.settle()
        var rows = ids(300)
        window.growTop(in: rows)
        let first = rows[window.range(of: rows).lowerBound]
        rows.removeAll { $0 == first }
        XCTAssertEqual(window.range(of: rows), (299 - TranscriptWindow.settledRows)..<299)
    }
}
