import XCTest
import SwiftUI
@testable import Coconut

final class ConversationFooterScrollTests: XCTestCase {
    func testOnlyLongUserScrollTowardHistoryAllowsCollapse() {
        var scroll = ConversationFooterScroll()
        scroll.scroll(to: 2_000)
        XCTAssertFalse(scroll.allowsMinimization)
        scroll.phaseChanged(.interacting, offset: 2_000)
        scroll.scroll(to: 1_950)
        XCTAssertFalse(scroll.allowsMinimization)
        scroll.phaseChanged(.decelerating, offset: 1_950)
        scroll.scroll(to: 1_800)
        XCTAssertTrue(scroll.allowsMinimization)
        scroll.phaseChanged(.idle, offset: 1_800)
        XCTAssertTrue(scroll.allowsMinimization)
        scroll.phaseChanged(.interacting, offset: 1_800)
        scroll.scroll(to: 1_850)
        XCTAssertFalse(scroll.allowsMinimization)
    }

    func testBounceDoesNotCountAsTravel() {
        let short = ScrollGeometry(contentOffset: CGPoint(x: 0, y: -200),
            contentSize: CGSize(width: 390, height: 100), contentInsets: EdgeInsets(top: 40, leading: 0, bottom: 80, trailing: 0),
            containerSize: CGSize(width: 390, height: 600))
        XCTAssertEqual(ConversationFooterScroll.offset(in: short), 0)
        var long = short
        long.contentSize.height = 2_000
        long.contentOffset.y = 2_000
        XCTAssertEqual(ConversationFooterScroll.offset(in: long), 1_520)
        long.contentOffset.y = -200
        XCTAssertEqual(ConversationFooterScroll.offset(in: long), 0)
    }

    func testSeparateShortGesturesDoNotAccumulate() {
        var scroll = ConversationFooterScroll()
        for offset in stride(from: 2_000, to: 1_000, by: -50) {
            scroll.phaseChanged(.interacting, offset: CGFloat(offset))
            scroll.scroll(to: CGFloat(offset - 50))
            scroll.phaseChanged(.idle, offset: CGFloat(offset - 50))
            XCTAssertFalse(scroll.allowsMinimization)
        }
    }

    func testProgrammaticScrollAndNavigationResetCannotCollapse() {
        var scroll = ConversationFooterScroll()
        scroll.phaseChanged(.animating, offset: 2_000)
        scroll.scroll(to: 500)
        XCTAssertFalse(scroll.allowsMinimization)
        scroll.phaseChanged(.interacting, offset: 2_000)
        scroll.scroll(to: 1_700)
        XCTAssertTrue(scroll.allowsMinimization)
        scroll.reset()
        scroll.scroll(to: 500)
        XCTAssertFalse(scroll.allowsMinimization)
    }
}
