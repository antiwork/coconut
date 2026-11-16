import XCTest

final class ConversationTabBarTests: XCTestCase {
    @MainActor
    func testConversationRestoresExpandedTabsAfterKeyboardAndSettings() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Home"].waitForExistence(timeout: 10))
        app.openCards()
        let context = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch
        XCTAssertTrue(context.waitForExistence(timeout: 5))
        context.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        let transcript = app.collectionViews["conversation-transcript"]
        let input = app.textViews["message-input"].exists ? app.textViews["message-input"] : app.textFields["message-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Draft")
        transcript.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 5))
        transcript.swipeDown()
        transcript.swipeUp()
        XCTAssertTrue(app.tabBars.buttons["Home"].isHittable)
        XCTAssertTrue(app.tabBars.buttons["Chats"].isHittable)
        app.buttons["bot-settings"].tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["Home"].isHittable)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Conversation with expanded bottom tabs"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.tabBars.buttons["Home"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["Home"].isHittable)
    }
}
