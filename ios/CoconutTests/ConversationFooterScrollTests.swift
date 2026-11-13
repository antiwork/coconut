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

    func testHiddenBelowMeasuresTheTranscriptEndBelowTheScreen() {
        var geometry = ScrollGeometry(contentOffset: CGPoint(x: 0, y: 900),
            contentSize: CGSize(width: 390, height: 2_000), contentInsets: EdgeInsets(top: 100, leading: 0, bottom: 140, trailing: 0),
            containerSize: CGSize(width: 390, height: 600))
        XCTAssertEqual(ConversationFooterScroll.hiddenBelow(in: geometry), 400)
        geometry.contentOffset.y = 1_300
        XCTAssertEqual(ConversationFooterScroll.hiddenBelow(in: geometry), 0)
        geometry.contentOffset.y = 1_400
        XCTAssertEqual(ConversationFooterScroll.hiddenBelow(in: geometry), 0)
        geometry.contentSize.height = 300
        geometry.contentOffset.y = -100
        XCTAssertEqual(ConversationFooterScroll.hiddenBelow(in: geometry), 0)
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

    // The largest height of a notice is its padding and a whole number of lines. Here a line is 20 points, with no
    // space between two lines, and the padding is 12 points above and below, so one line needs 44 points.
    private func limit(_ area: CGFloat, line: CGFloat = 20, spacing: CGFloat = 0) -> CGFloat {
        NoticeHeight.limit(area: area, line: line, spacing: spacing, padding: 12)
    }

    // A tall area gives 5 lines.
    func testNoticeHeightGivesFiveLinesInATallArea() {
        XCTAssertEqual(limit(1_000), 124)
    }

    // A middle area gives the whole lines that fit in 40 percent of it. 40 percent of 230 is 92, which holds 3.4
    // lines after the padding, so the notice gets 3 lines.
    func testNoticeHeightRoundsAMiddleAreaDownToWholeLines() {
        XCTAssertEqual(limit(230), 84)
        XCTAssertEqual(limit(200), 64)
    }

    // An area of exactly one line, and an area where 40 percent is less than one line, give one line.
    func testNoticeHeightGivesOneLineWhenTheAreaHoldsOneLine() {
        XCTAssertEqual(limit(44), 44)
        XCTAssertEqual(limit(80), 44)
    }

    // An area lower than one line gives the whole area, and no area gives no height.
    func testNoticeHeightGivesTheWholeAreaWhenItIsLowerThanOneLine() {
        XCTAssertEqual(limit(30), 30)
        XCTAssertEqual(limit(0), 0)
        XCTAssertEqual(limit(-10), 0)
    }

    // A line height that is not a whole number gives whole lines of that height, not of a rounded height.
    func testNoticeHeightUsesALineHeightThatIsNotAWholeNumber() {
        XCTAssertEqual(limit(1_000, line: 20.5), 5 * 20.5 + 24, accuracy: 0.0001)
        // 40 percent of 200 is 80, which holds 2.7 lines of 20.5 points after the padding.
        XCTAssertEqual(limit(200, line: 20.5), 2 * 20.5 + 24, accuracy: 0.0001)
    }

    // The body text of an iPhone at the Large text size: one line is 20.33 points high, and each next line adds 22
    // points. The space between two lines counts, but not after the last line.
    func testNoticeHeightCountsTheSpaceBetweenLines() {
        let line: CGFloat = 61 / 3, spacing: CGFloat = 5 / 3
        XCTAssertEqual(limit(1_000, line: line, spacing: spacing), 5 * line + 4 * spacing + 24, accuracy: 0.0001)
        // 40 percent of 200 is 80. Three lines need 88.33 points, so the notice gets two lines.
        XCTAssertEqual(limit(200, line: line, spacing: spacing), 2 * line + spacing + 24, accuracy: 0.0001)
        XCTAssertEqual(limit(30, line: line, spacing: spacing), 30)
    }

    // A chat on the screen holds the notice of the Mac window. A chat off the screen does not.
    func testChatHoldsNoticeWhenAnyChatOnTheScreenHoldsIt() {
        var holds = ChatHoldsNoticePreference.defaultValue
        XCTAssertFalse(holds)
        ChatHoldsNoticePreference.reduce(value: &holds) { false }
        XCTAssertFalse(holds)
        ChatHoldsNoticePreference.reduce(value: &holds) { true }
        ChatHoldsNoticePreference.reduce(value: &holds) { false }
        XCTAssertTrue(holds)
    }
}
