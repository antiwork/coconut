import XCTest

final class QuietConversationTests: XCTestCase {
    @MainActor
    func testConversationHasNoToolActivityRows() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let bot = app.buttons.matching(NSPredicate(format: "identifier == %@ OR identifier == %@",
            "bot-conversation-00000000-0000-4000-8000-000000000001", "pinned-bot-00000000-0000-4000-8000-000000000001")).firstMatch
        XCTAssertTrue(bot.waitForExistence(timeout: 10))
        bot.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        let transcript = app.collectionViews["conversation-transcript"]
        transcript.swipeDown()
        XCTAssertFalse(app.descendants(matching: .any)["activity-row"].exists)
        XCTAssertFalse(app.staticTexts["Read 4 files · Compared the payout retry paths"].exists)
        XCTAssertFalse(app.staticTexts["Ran 212 payout specs · All passed"].exists)
        XCTAssertTrue(app.staticTexts["Save the payout retry fix?"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Conversation without tool logs"; proof.lifetime = .keepAlways; add(proof)
    }
}
