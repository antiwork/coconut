import XCTest
@testable import Coconut

final class ConversationRowHoldTests: XCTestCase {
    func testAnOffsetChangeWithoutTheReaderShowsTheHeldRowAgain() {
        var hold = ConversationRowHold()
        XCTAssertNil(hold.rowToRestore(offsetChange: -868, userScrolling: false, followsBottom: false))
        hold.hold("card")
        XCTAssertEqual(hold.rowToRestore(offsetChange: -868, userScrolling: false, followsBottom: false), "card")
        XCTAssertEqual(hold.rowToRestore(offsetChange: 19, userScrolling: false, followsBottom: false), "card")
    }

    func testTheReaderAndTheBottomKeepTheirScroll() {
        var hold = ConversationRowHold()
        hold.hold("card")
        XCTAssertNil(hold.rowToRestore(offsetChange: -868, userScrolling: true, followsBottom: false))
        XCTAssertNil(hold.rowToRestore(offsetChange: 400, userScrolling: false, followsBottom: true))
        XCTAssertNil(hold.rowToRestore(offsetChange: 0.5, userScrolling: false, followsBottom: false))
        hold.release()
        XCTAssertNil(hold.rowToRestore(offsetChange: -868, userScrolling: false, followsBottom: false))
    }
}
