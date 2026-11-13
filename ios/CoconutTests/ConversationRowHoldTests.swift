import XCTest
import SwiftUI
@testable import Coconut

final class ConversationRowHoldTests: XCTestCase {
    func testAnOffsetChangeWithoutTheReaderShowsTheHeldRowAgain() {
        var hold = ConversationRowHold()
        XCTAssertNil(hold.rowToRestore(layoutChange: -868, userScrolling: false, followsBottom: false))
        hold.hold("card")
        XCTAssertEqual(hold.rowToRestore(layoutChange: -868, userScrolling: false, followsBottom: false), "card")
        XCTAssertEqual(hold.rowToRestore(layoutChange: 19, userScrolling: false, followsBottom: false), "card")
    }

    func testTheReaderAndTheBottomKeepTheirScroll() {
        var hold = ConversationRowHold()
        hold.hold("card")
        XCTAssertNil(hold.rowToRestore(layoutChange: -868, userScrolling: true, followsBottom: false))
        XCTAssertNil(hold.rowToRestore(layoutChange: 400, userScrolling: false, followsBottom: true))
        XCTAssertNil(hold.rowToRestore(layoutChange: 0.5, userScrolling: false, followsBottom: false))
        hold.release()
        XCTAssertNil(hold.rowToRestore(layoutChange: -868, userScrolling: false, followsBottom: false))
    }

    func testOnlyAScrollByTheReaderEndsTheHold() {
        var hold = ConversationRowHold()
        hold.hold("card")
        for phase in [ScrollPhase.idle, .tracking, .animating, .decelerating] {
            hold.phaseChanged(phase)
            XCTAssertEqual(hold.row, "card")
        }
        hold.phaseChanged(.interacting)
        XCTAssertNil(hold.row)
    }

    func testAMoveOfTheOffsetAloneEndsTheHoldOnlyAfterTheRowIsPlaced() {
        var hold = ConversationRowHold()
        let placed = Date(timeIntervalSinceReferenceDate: 1_000)
        hold.hold("card", now: placed)
        // The scroll of the chat to the row is a move of the offset too.
        hold.offsetMoved(now: placed.addingTimeInterval(0.1))
        XCTAssertEqual(hold.row, "card")
        // A mouse wheel later.
        hold.offsetMoved(now: placed.addingTimeInterval(1))
        XCTAssertNil(hold.row)
    }

    func testASentMessageMovesTheChatAfterTheLayoutOfItsRow() {
        var scroll = SentMessageScroll()
        let sent = Date(timeIntervalSinceReferenceDate: 1_000)
        // Without a send, a layout moves nothing.
        XCTAssertFalse(scroll.laidOut(inset: 58, now: sent))
        XCTAssertFalse(scroll.waits)
        // The person sends. The scroll waits for the layout of the row.
        scroll.sent(now: sent)
        XCTAssertTrue(scroll.waits)
        // The layout of the row, with the composer of one line: the chat moves now, one time.
        XCTAssertTrue(scroll.laidOut(inset: 58, now: sent.addingTimeInterval(0.02)))
        XCTAssertFalse(scroll.waits)
        XCTAssertFalse(scroll.laidOut(inset: 58, now: sent.addingTimeInterval(0.03)))
    }

    func testASentMessageWaitsWhileTheComposerIsTall() {
        var scroll = SentMessageScroll()
        let sent = Date(timeIntervalSinceReferenceDate: 1_000)
        // The last layout before the send had the composer of one line.
        _ = scroll.laidOut(inset: 56, now: sent)
        scroll.sent(now: sent)
        // The layout of the row: the composer still has the five lines of the draft.
        XCTAssertFalse(scroll.laidOut(inset: 116, now: sent.addingTimeInterval(0.01)))
        XCTAssertTrue(scroll.waits)
        XCTAssertFalse(scroll.laidOut(inset: 116, now: sent.addingTimeInterval(0.02)))
        // The composer is short again: the chat moves now.
        XCTAssertTrue(scroll.laidOut(inset: 56, now: sent.addingTimeInterval(0.04)))
        XCTAssertFalse(scroll.waits)
    }

    func testASentMessageWaitsWhenTheChatKnewTheTallComposer() {
        var scroll = SentMessageScroll()
        let sent = Date(timeIntervalSinceReferenceDate: 1_000)
        // A first layout without the composer does not count. Then the composer with one line, then with five.
        _ = scroll.laidOut(inset: 0, now: sent)
        _ = scroll.laidOut(inset: 56, now: sent)
        _ = scroll.laidOut(inset: 116, now: sent)
        XCTAssertEqual(scroll.restingInset, 56)
        scroll.sent(now: sent)
        XCTAssertFalse(scroll.laidOut(inset: 116, now: sent.addingTimeInterval(0.01)))
        XCTAssertTrue(scroll.laidOut(inset: 56, now: sent.addingTimeInterval(0.03)))
    }

    func testTheWaitOfASentMessageEnds() {
        var scroll = SentMessageScroll()
        let sent = Date(timeIntervalSinceReferenceDate: 1_000)
        _ = scroll.laidOut(inset: 56, now: sent)
        scroll.sent(now: sent)
        XCTAssertFalse(scroll.laidOut(inset: 116, now: sent.addingTimeInterval(0.01)))
        // A later layout that still has the tall inset, after the limit: the chat moves.
        XCTAssertTrue(scroll.laidOut(inset: 116, now: sent.addingTimeInterval(SentMessageScroll.limit + 0.01)))
        // No layout at all: the timer of the chat ends the wait, one time.
        scroll.sent(now: sent)
        XCTAssertTrue(scroll.timedOut())
        XCTAssertFalse(scroll.timedOut())
        XCTAssertFalse(scroll.waits)
    }
}

@MainActor
final class ConversationRowHoldViewTests: XCTestCase {
    func testAnOffsetChangeWithoutTheReaderShowsTheOpenedCardAgain() async throws {
        let store = AppStore(demo: true)
        let forge = try XCTUnwrap(store.bots.first { $0.name == "Forge" })
        let first = try XCTUnwrap(store.decisions.first { $0.botId == forge.id && $0.isPending })
        let paragraph = "The plan keeps your instructions and prior decisions together. Each update continues the same conversation. "
        // Cards of different heights make the lazy stack's estimates for the rows above the opened card wrong.
        let cards = [4, 2, 1].enumerated().map { index, optionCount in
            var card = first
            card.id = "00000000-0000-4000-8000-00000000004\(index)"
            card.title = "Card \(index)"
            card.explanation = String(repeating: paragraph, count: 3 - index)
            card.options = (0..<optionCount).map { number in
                var option = first.options[0]
                option.id = "option-\(index)-\(number)"
                option.label = "Choice \(number)"
                option.explanation = String(repeating: "Keep the draft private and leave the page unchanged. ", count: number + 1)
                return option
            }
            return card
        }
        store.decisions = cards
        // The demo moves its times to now, so the later messages take their times from the cards.
        let cardTime = try XCTUnwrap(first.createdAt.apiDate)
        store.messages += [paragraph + paragraph + paragraph, String(repeating: paragraph, count: 12), "Latest note after the cards."].enumerated().map { index, text in
            Message(id: "later-\(index)", botId: forge.id, taskId: first.taskId, role: "assistant", kind: "text", text: text,
                    decisionId: nil, artifactId: nil, createdAt: cardTime.addingTimeInterval(Double(index + 1) * 60).formatted(.iso8601), streaming: false)
        }
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: NavigationStack {
            ConversationView(botId: forge.id, focusDecisionId: cards[2].id)
        }.environment(store))
        window.makeKeyAndVisible()
        defer { window.isHidden = true }

        func transcript() -> UIScrollView? {
            func scrollViews(in view: UIView) -> [UIScrollView] {
                ((view as? UIScrollView).map { [$0] } ?? []) + view.subviews.flatMap { scrollViews(in: $0) }
            }
            return scrollViews(in: window).max { $0.contentSize.height < $1.contentSize.height }
        }
        func settle() async { try? await Task.sleep(for: .milliseconds(400)) }
        for _ in 0..<20 where (transcript()?.contentOffset.y ?? 0) <= 0 { await settle() }
        await settle()
        let opened = try XCTUnwrap(transcript()?.contentOffset.y)
        XCTAssertGreaterThan(opened, 0, "The chat must open at the third card, below the top.")

        // While assistive technology reads every row, lazy rows above the card can take a new height, and the
        // scroll view then moved to the top of the chat. Make that move without the reader.
        let scrollView = try XCTUnwrap(transcript())
        scrollView.setContentOffset(CGPoint(x: 0, y: -scrollView.adjustedContentInset.top), animated: false)
        await settle()
        XCTAssertEqual(scrollView.contentOffset.y, opened, accuracy: 4, "The chat must show the opened card again.")

    }
}
