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
