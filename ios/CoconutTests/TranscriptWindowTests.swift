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

    // The hold. Rows of a list, from the top: the header, the transcript, the status row, the bottom spacer.
    private let list: [(id: String, height: CGFloat)] = [("header", 10), ("m1", 120), ("m2", 80), ("m3", 200),
                                                          ("m4", 60), ("m5", 90), ("status", 40), ("spacer", 20)]
    /// m3 and m4 are on screen.
    private let onScreen = ["m3", "m4"]

    private func tops(_ rows: [(id: String, height: CGFloat)]) -> [String: CGFloat] {
        var top: CGFloat = 0
        var tops: [String: CGFloat] = [:]
        for row in rows { tops[row.id] = top; top += row.height }
        return tops
    }

    private func correction(after: [(id: String, height: CGFloat)]) -> CGFloat? {
        let before = tops(list)
        return TranscriptHoldRule.correction(anchors: onScreen.map { ($0, before[$0]!) }, topsAfter: tops(after))
    }

    private func older(_ heights: [CGFloat]) -> [(id: String, height: CGFloat)] {
        heights.enumerated().map { ("old\($0.offset)", $0.element) }
    }

    func testAGrowthAboveWithTheStatusRowRemovedBelowCorrectsForTheGrowth() {
        let after = [list[0]] + older([300, 45, 130, 70, 55]) + list[1...5] + [list[7]]
        XCTAssertEqual(correction(after: Array(after)), 600)
    }

    func testAGrowthAboveWithAReplyBelowCorrectsForTheGrowth() {
        let after = [list[0]] + older([300, 45]) + list[1...5] + [(id: "reply", height: CGFloat(150))] + list[6...7]
        XCTAssertEqual(correction(after: Array(after)), 345)
    }

    func testARowDeletedAboveTheAnchorMovesBackByItsHeight() {
        XCTAssertEqual(correction(after: list.filter { $0.id != "m2" }), -80)
    }

    func testARowDeletedBelowTheAnchorChangesNothing() {
        XCTAssertEqual(correction(after: list.filter { $0.id != "m5" }), 0)
    }

    func testADeletedAnchorUsesTheNextRowThatWasOnScreen() {
        // m3 is gone; m4 was at 410 and is at 210 now.
        XCTAssertEqual(correction(after: list.filter { $0.id != "m3" }), -200)
        XCTAssertNil(correction(after: list.filter { !onScreen.contains($0.id) }))
    }

    func testNoChangeGivesNoCorrection() {
        XCTAssertEqual(correction(after: list), 0)
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
