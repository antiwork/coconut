import XCTest

final class QuietConversationTests: XCTestCase {
    @MainActor
    func testConversationHasNoToolActivityRows() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons.matching(NSPredicate(format: "identifier == %@ OR identifier == %@",
            "bot-conversation-00000000-0000-4000-8000-000000000001", "pinned-bot-00000000-0000-4000-8000-000000000001")).firstMatch
        XCTAssertTrue(bot.waitForExistence(timeout: 10))
        bot.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        let transcript = app.scrollViews["conversation-transcript"]
        transcript.swipeDown()
        XCTAssertFalse(app.descendants(matching: .any)["activity-row"].exists)
        XCTAssertFalse(app.staticTexts["Read 3 files · Compared the retry paths"].exists)
        XCTAssertFalse(app.staticTexts["Ran 6 focused checks · All passed"].exists)
        XCTAssertTrue(app.staticTexts["Save the retry fix?"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Conversation without tool logs"; proof.lifetime = .keepAlways; add(proof)
    }
}
