import XCTest

final class FooterScrollTests: XCTestCase {
    @MainActor
    func testOnlyLongConversationScrollMinimizesFooter() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py --footer-scroll.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        let inbox = app.tabBars.buttons["Home"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 15))
        XCTAssertTrue(inbox.isSelected, "Home must be the launch screen.")
        XCTAssertLessThan(inbox.frame.minX, app.tabBars.buttons["Chats"].frame.minX)
        app.tabBars.buttons["Chats"].tap()
        app.scrollViews.firstMatch.swipeUp()
        XCTAssertTrue(inbox.isHittable, "Scrolling the Bots list must keep both tabs available.")
        app.openCards()
        app.scrollViews.firstMatch.swipeUp()
        XCTAssertTrue(app.tabBars.buttons["Chats"].isHittable, "Reading a card must not minimize the footer.")
        app.tabBars.buttons["Chats"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        for _ in 0..<5 where !bot.isHittable { app.scrollViews.firstMatch.swipeDown(velocity: .fast) }
        XCTAssertTrue(bot.isHittable)
        bot.tap()
        let transcript = app.collectionViews["conversation-transcript"]
        XCTAssertTrue(transcript.waitForExistence(timeout: 5))
        XCTAssertTrue(inbox.isHittable, "Opening a conversation must keep the footer expanded.")
        let start = transcript.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 45)), withVelocity: .slow, thenHoldForDuration: 0.2)
        XCTAssertTrue(inbox.isHittable, "A short scroll must not minimize the footer.")
        transcript.swipeDown(velocity: .slow)
        transcript.swipeDown(velocity: .slow)
        XCTAssertTrue(inbox.waitForNonExistence(timeout: 5) || !inbox.isHittable,
                      "A long scroll toward earlier messages must minimize the footer.")
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Footer minimized while reading history"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(inbox.waitForExistence(timeout: 5) && inbox.isHittable,
                      "Leaving the conversation must restore the footer.")
    }
}
