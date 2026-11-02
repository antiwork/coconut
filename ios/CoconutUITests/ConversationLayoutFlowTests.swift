import XCTest

final class ConversationLayoutFlowTests: XCTestCase {
    @MainActor
    func testTypingResizingAndSendingKeepTranscriptVisible() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run with scripts/test_conversation_layout.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        var latest = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Latest response stays visible.")).firstMatch
        assertVisible(latest, app: app, stage: "Open conversation")
        app.scrollViews.firstMatch.swipeDown()
        app.scrollViews.firstMatch.swipeDown()
        input.tap()
        print("COCONUT_LAYOUT_STAGE=stream")
        for letter in "ok so what next. what things do you need" {
            let label = letter == " " ? "space" : String(letter)
            let key = app.keys.matching(NSPredicate(format: "label ==[c] %@ OR identifier == %@", label, label)).firstMatch
            if key.exists { key.tap() } else { input.typeText(String(letter)) }
        }
        assertTranscriptVisible(app, stage: "Software keyboard")
        for part in ["\nFirst detail", "\nSecond detail", "\nThird detail", "\nFourth detail"] {
            input.typeText(part)
            assertTranscriptVisible(app, stage: "Resize draft while reading history")
        }
        app.buttons["send-message"].tap()
        latest = app.staticTexts["Reply after message 1."]
        assertVisible(latest, app: app, stage: "First reply")
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
            .press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.8)))
        input.tap()
        input.typeText("One more message.")
        assertTranscriptVisible(app, stage: "Second draft")
        app.buttons["send-message"].tap()
        assertVisible(app.staticTexts["Reply after message 2."], app: app, stage: "Second reply")
    }

    @MainActor
    func testKeyboardDismissalAndComposerStayAvailable() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run with scripts/test_conversation_layout.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        let transcript = app.scrollViews["conversation-transcript"]
        for draft in ["Keep this draft.", "\nFirst detail\nSecond detail\nThird detail\nFourth detail"] {
            input.tap()
            input.typeText(draft)
            assertComposerAboveKeyboard(input, app: app)
            app.coordinate(withNormalizedOffset: .zero).withOffset(
                CGVector(dx: transcript.frame.maxX - 12, dy: input.frame.minY - 40)
            ).tap()
            XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3),
                          "Tapping the conversation must dismiss the keyboard.")
        }
        let savedDraft = input.value as? String
        input.tap()
        let latest = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Latest response stays visible.")).firstMatch
        XCTAssertTrue(latest.isHittable)
        latest.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3),
                      "Tapping message text must dismiss the keyboard.")
        input.tap()
        app.buttons["bot-settings"].tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3),
                      "Leaving the conversation must release its text field.")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        XCTAssertEqual(input.value as? String, savedDraft)
        input.tap()
        assertComposerAboveKeyboard(input, app: app)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Composer remains above the keyboard after returning"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    @MainActor
    func testLatestButtonReturnsToLastMessage() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py --latest-button.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        // Lazy rows below the screen stay in the tree, so the lowest text ends above the composer only at the end.
        let texts = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-text-"))
        let endVisible = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            (texts.allElementsBoundByIndex.map(\.frame.maxY).max() ?? .infinity) <= input.frame.minY
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [endVisible], timeout: 5), .completed, "The chat must open at its end.")
        let jump = app.buttons["conversation-latest"]
        XCTAssertTrue(jump.waitForNonExistence(timeout: 3), "A chat at its end needs no jump.")
        let transcript = app.scrollViews["conversation-transcript"]
        transcript.swipeDown()
        transcript.swipeDown()
        XCTAssertTrue(jump.waitForExistence(timeout: 5), "Newer messages below the screen must show the jump.")
        jump.tap()
        XCTAssertTrue(jump.waitForNonExistence(timeout: 5))
        let returned = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            (texts.allElementsBoundByIndex.map(\.frame.maxY).max() ?? .infinity) <= input.frame.minY
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [returned], timeout: 5), .completed, "The jump must show the end of the chat.")
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Jump to latest"; proof.lifetime = .keepAlways; add(proof)
    }

    @MainActor
    func testKeyboardKeepsBottomPinnedAndPreservesHistory() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run with scripts/test_conversation_layout.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        let latest = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Latest response stays visible.")).firstMatch
        assertVisible(latest, app: app, stage: "Bottom before keyboard")
        input.tap()
        assertComposerAboveKeyboard(input, app: app)
        XCTAssertLessThanOrEqual(latest.frame.maxY, input.frame.minY,
                                 "Opening the keyboard at the bottom must keep the end of the latest message visible.")
        let pinned = XCTAttachment(screenshot: app.screenshot())
        pinned.name = "Keyboard keeps the latest message above the composer"
        pinned.lifetime = .keepAlways
        add(pinned)

        let transcript = app.scrollViews["conversation-transcript"]
        app.coordinate(withNormalizedOffset: .zero).withOffset(
            CGVector(dx: transcript.frame.maxX - 8, dy: input.frame.minY - 40)
        ).tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3))
        transcript.swipeDown()
        transcript.swipeDown()
        let visibleMessages = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-text-"))
            .allElementsBoundByIndex.filter { $0.isHittable }
        let history = visibleMessages.first { $0.frame.minY > 140 && $0.frame.minY < 350 } ?? visibleMessages.first
        let anchorID = try XCTUnwrap(history).identifier
        let anchor = app.staticTexts[anchorID]
        let previousY = anchor.frame.minY
        input.tap()
        assertComposerAboveKeyboard(input, app: app)
        XCTAssertEqual(anchor.frame.minY, previousY, accuracy: 3,
                       "Opening the keyboard while reading history must preserve the reading position.")
        let preserved = XCTAttachment(screenshot: app.screenshot())
        preserved.name = "Keyboard preserves the history position"
        preserved.lifetime = .keepAlways
        add(preserved)
    }

    @MainActor
    private func assertComposerAboveKeyboard(_ input: XCUIElement, app: XCUIApplication) {
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 5))
        XCTAssertTrue(input.isHittable, "The focused composer must remain visible.")
        XCTAssertGreaterThan(input.frame.height, 20)
        XCTAssertLessThanOrEqual(input.frame.maxY, keyboard.frame.minY + 1,
                                 "The keyboard must not cover the composer.")
    }

    @MainActor
    private func assertTranscriptVisible(_ app: XCUIApplication, stage: String) {
        let visible = NSPredicate { _, _ in
            app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-text-"))
                .allElementsBoundByIndex.contains { $0.isHittable }
        }
        let result = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: visible, object: nil)], timeout: 5)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = stage
        screenshot.lifetime = .keepAlways
        add(screenshot)
        XCTAssertEqual(result, .completed, "Transcript disappeared at: \(stage)")
    }

    @MainActor
    private func assertVisible(_ element: XCUIElement, app: XCUIApplication, stage: String) {
        let visible = NSPredicate { _, _ in element.exists && element.isHittable }
        let result = XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: visible, object: nil)], timeout: 5)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = stage
        screenshot.lifetime = .keepAlways
        add(screenshot)
        XCTAssertEqual(result, .completed, "Transcript disappeared at: \(stage)")
    }
}
