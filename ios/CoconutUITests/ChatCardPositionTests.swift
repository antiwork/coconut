import XCTest

final class ChatCardPositionTests: XCTestCase {
    @MainActor
    func testPendingCardKeepsItsPlaceWhenHistoryLoads() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_DISMISS_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_chat_card_dismiss.py --position.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "position-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let card = app.staticTexts["Keep this report?"]
        let before = app.staticTexts["Before the card."]
        let after = app.staticTexts["After the card."]
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        XCTAssertFalse(before.exists)
        XCTAssertLessThan(card.frame.maxY, after.frame.minY)
        app.buttons["load-earlier-messages"].tap()
        XCTAssertTrue(app.buttons["load-earlier-messages"].waitForNonExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts.matching(identifier: "Keep this report?").count, 1)
        XCTAssertLessThan(before.frame.maxY, card.frame.minY)
        XCTAssertLessThan(card.frame.maxY, after.frame.minY)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Pending card keeps its chronological position"
        proof.lifetime = .keepAlways
        add(proof)
    }
}
