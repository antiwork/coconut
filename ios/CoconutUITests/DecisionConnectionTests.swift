import XCTest

final class DecisionConnectionTests: XCTestCase {
    @MainActor
    func testCardsSendWhileSyncFailsAndRetryLostReceipts() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_CARD_CONNECTION_URL"] else {
            throw XCTSkip("Run scripts/test_card_connection.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "connection-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        let inbox = app.tabBars.buttons["Inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 15))
        inbox.tap()
        let option = app.buttons["decision-option-confirm"]
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        XCTAssertTrue(option.waitForExistence(timeout: 5) && option.isEnabled)
        XCTAssertFalse(app.otherElements["bot-working-00000000-0000-4000-8000-000000000001"].exists)
        XCTAssertTrue(app.otherElements["bot-working-00000000-0000-4000-8000-000000000002"].exists)
        var request = URLRequest(url: URL(string: address + "/test/offline")!)
        request.httpMethod = "POST"
        let offline = expectation(description: "Stop background sync")
        URLSession.shared.dataTask(with: request) { _, _, _ in offline.fulfill() }.resume()
        wait(for: [offline], timeout: 5)
        sleep(2)
        XCTAssertTrue(option.isEnabled)
        option.tap()
        XCTAssertTrue(app.staticTexts["Choice received"].waitForExistence(timeout: 10))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Choice recorded while background sync is unavailable"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let stale = app.buttons["decision-option-stale"]
        XCTAssertTrue(stale.waitForExistence(timeout: 5) && stale.isEnabled)
        stale.tap()
        XCTAssertTrue(app.buttons["Dismiss error"].waitForExistence(timeout: 8))
        app.buttons["Dismiss error"].tap()
        XCTAssertTrue(stale.waitForExistence(timeout: 5))
    }
}
