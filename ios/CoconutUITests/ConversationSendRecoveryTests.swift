import XCTest

final class ConversationSendRecoveryTests: XCTestCase {
    @MainActor
    func testKeyboardDragKeepsKeysAndReplyVisibleAfterSending() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run scripts/test_conversation_layout.py --keyboard-recovery.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        input.tap()
        input.typeText("First message.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Reply after message 1."].waitForExistence(timeout: 8))
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
            .press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.8)))
        input.tap()
        input.typeText("Second message.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Reply after message 2."].waitForExistence(timeout: 8))
        let visible = NSPredicate { _, _ in
            app.keys["space"].exists && app.keys["space"].isHittable
                && app.keyboards.firstMatch.frame.maxY <= app.frame.maxY + 1
                && app.staticTexts["Reply after message 2."].isHittable
        }
        let result = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: visible, object: nil)], timeout: 4)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Keyboard after interrupted dismissal and send"
        proof.lifetime = .keepAlways
        add(proof)
        print("KEYBOARD_FRAME=\(app.keyboards.firstMatch.frame) WINDOW_FRAME=\(app.frame)")
        XCTAssertEqual(result, .completed, "The full keyboard must return after tapping the composer.")
    }

}
