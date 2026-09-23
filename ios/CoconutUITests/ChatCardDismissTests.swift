import XCTest

final class ChatCardDismissTests: XCTestCase {
    @MainActor
    func testDismissFromChatAndRetryAfterFailure() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_DISMISS_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_chat_card_dismiss.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "dismiss-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        let dismiss = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "dismiss-card-")).firstMatch
        XCTAssertTrue(app.staticTexts["Delete the sample export?"].waitForExistence(timeout: 5))
        XCTAssertFalse(dismiss.exists, "Inbox dismissal stays in the swipe interaction.")
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        XCTAssertTrue(dismiss.waitForExistence(timeout: 5) && dismiss.isHittable)
        let pendingProof = XCTAttachment(screenshot: app.screenshot())
        pendingProof.name = "Chat dismiss control"
        pendingProof.lifetime = .keepAlways
        add(pendingProof)
        dismiss.tap()
        let receipt = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-selection-")).firstMatch
        XCTAssertTrue(receipt.waitForExistence(timeout: 1))
        XCTAssertEqual(receipt.label, "Dismissed")
        XCTAssertTrue(app.staticTexts["Dismissal not confirmed. Try again."].waitForExistence(timeout: 8))
        XCTAssertTrue(dismiss.exists && dismiss.isHittable)
        dismiss.tap()
        XCTAssertTrue(receipt.waitForExistence(timeout: 1))
        XCTAssertEqual(receipt.label, "Dismissed")
        XCTAssertFalse(app.staticTexts["Temporary failure"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Dismissed card in chat"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.staticTexts["Delete the sample export?"].exists)
    }
}
