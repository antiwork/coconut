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
