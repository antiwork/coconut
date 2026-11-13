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

    // The largest height of a notice: 5 lines in a tall area, 40 percent of a middle area, one line when 40 percent is
    // less, and the whole area when the area is lower than one line.
    func testNoticeHeightLimit() {
        let line: CGFloat = 20, padding: CGFloat = 12
        XCTAssertEqual(NoticeHeight.limit(area: 1_000, line: line, padding: padding), 124)
        XCTAssertEqual(NoticeHeight.limit(area: 200, line: line, padding: padding), 80)
        XCTAssertEqual(NoticeHeight.limit(area: 80, line: line, padding: padding), 44)
        XCTAssertEqual(NoticeHeight.limit(area: 30, line: line, padding: padding), 30)
        XCTAssertEqual(NoticeHeight.limit(area: 0, line: line, padding: padding), 0)
    }

    // With no chat on the screen, the detail column of the Mac window shows the notice.
    func testColumnShowsNoticeWhenNoChatHoldsIt() {
        var holders = NoticeHolders()
        XCTAssertTrue(holders.columnShowsNotice)
        XCTAssertNil(holders.holder)
        let chat = UUID()
        holders.add(chat)
        XCTAssertFalse(holders.columnShowsNotice)
        holders.remove(chat)
        XCTAssertTrue(holders.columnShowsNotice)
        XCTAssertNil(holders.holder)
    }

    // A page that a stack pushes over a chat covers the chat, so the chat leaves the screen. The column shows the
    // notice, and the covered chat does not hold it.
    func testCoveredChatDoesNotHoldNotice() {
        var holders = NoticeHolders()
        let chat = UUID()
        holders.add(chat)
        holders.remove(chat)
        XCTAssertTrue(holders.columnShowsNotice)
        // The page goes back: the chat comes on the screen again and holds the notice again.
        holders.add(chat)
        XCTAssertEqual(holders.holder, chat)
    }

    // Exactly one chat shows the notice: the chat that came on the screen last. A push of a chat over a chat can
    // show the new chat before the old chat leaves.
    func testNewestChatOnTheScreenHoldsNotice() {
        var holders = NoticeHolders()
        let first = UUID(), second = UUID()
        holders.add(first)
        holders.add(second)
        XCTAssertEqual(holders.holder, second)
        holders.remove(first)
        XCTAssertEqual(holders.holder, second)
        // The order is the other way: the old chat leaves first.
        holders = NoticeHolders()
        holders.add(first)
        holders.remove(first)
        holders.add(second)
        XCTAssertEqual(holders.holder, second)
        // A chat that comes on the screen again is not counted two times.
        holders.add(first)
        holders.add(second)
        XCTAssertEqual(holders.chats, [first, second])
        holders.remove(second)
        XCTAssertEqual(holders.holder, first)
        // A chat that is not in the list changes nothing.
        holders.remove(UUID())
        XCTAssertEqual(holders.chats, [first])
    }
}
