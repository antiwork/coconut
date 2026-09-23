import XCTest

final class MessageInteractionFlowTests: XCTestCase {
    @MainActor
    func testRepliesReactionsAndCopy() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the message fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let original = app.staticTexts["The blue folder has the complete report."]
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        let originalId = original.identifier
        let source = app.staticTexts[originalId]
        source.swipeRight()
        XCTAssertTrue(app.buttons["Cancel reply"].waitForExistence(timeout: 2))
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 2))
        input.typeText("Use this folder.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Use this folder."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Cancel reply"].exists)
        let quote = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-quote-")).firstMatch
        XCTAssertTrue(quote.waitForExistence(timeout: 5))
        quote.tap()
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Reply links to the original message"; proof.lifetime = .keepAlways; add(proof)
        source.press(forDuration: 1)
        XCTAssertTrue(app.buttons["Copy"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Reply"].exists)
        for name in ["Thumbs up", "Thumbs down"] {
            XCTAssertTrue(app.buttons[name].exists, name)
        }
        for name in ["Laugh", "Celebrate", "Confused", "Heart", "Rocket", "Eyes"] {
            XCTAssertFalse(app.buttons[name].exists, name)
        }
        let menu = XCTAttachment(screenshot: app.screenshot())
        menu.name = "Two reactions, Reply, and Copy"; menu.lifetime = .keepAlways; add(menu)
        app.buttons["Thumbs up"].tap()
        XCTAssertTrue(app.buttons["Remove 👍 reaction"].waitForExistence(timeout: 3))
        app.buttons["Remove 👍 reaction"].tap()
        XCTAssertTrue(app.buttons["Remove 👍 reaction"].waitForNonExistence(timeout: 3))
        source.press(forDuration: 1)
        app.buttons["Copy"].tap()
        input.tap()
        input.press(forDuration: 1)
        let paste = app.menuItems["Paste"].exists ? app.menuItems["Paste"] : app.buttons["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 3))
        paste.tap()
        XCTAssertEqual(input.value as? String, "The blue folder has the complete report.")
    }
}
