import XCTest

final class BotExchangeFlowTests: XCTestCase {
    @MainActor
    func testExchangeOpensFromEitherBotWithMarkdownAndNoComposer() async throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py with --exchange.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        let scout = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(scout.waitForExistence(timeout: 15))
        scout.tap()
        XCTAssertTrue(app.staticTexts["My own work stays here."].waitForExistence(timeout: 5))
        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-exchange-")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertTrue(row.label.contains("2 messages with"))
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Check the report.")).firstMatch.exists)
        capture(app, "Compact bot exchange")
        row.tap()
        assertExchange(app)
        capture(app, "Shared exchange with Markdown")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5) && row.isHittable)
        XCTAssertTrue(app.staticTexts["My own work stays here."].isHittable)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let forge = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(forge.waitForExistence(timeout: 5))
        forge.tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["My own work stays here."].exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "The check passed")).firstMatch.exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "let answer = 42")).firstMatch.exists)
        var request = URLRequest(url: URL(string: url + "/test/exchange")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        let updated = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@ AND label CONTAINS %@", "bot-exchange-", "3 messages with")).firstMatch
        XCTAssertTrue(updated.waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Forge"))
        XCTAssertFalse(app.navigationBars["Task"].exists)
        XCTAssertFalse(app.staticTexts["A new peer reply."].exists)
        updated.tap()
        assertExchange(app)
        XCTAssertTrue(app.staticTexts["A new peer reply."].exists)
        capture(app, "Recipient exchange after a live reply")
    }

    @MainActor
    private func assertExchange(_ app: XCUIApplication) {
        XCTAssertTrue(app.staticTexts["Read-only"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.descendants(matching: .any).matching(identifier: "message-input").firstMatch.exists)
        let request = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Check the report.")).firstMatch
        XCTAssertTrue(request.waitForExistence(timeout: 5))
        XCTAssertFalse(request.label.contains("**"))
        let result = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "let answer = 42")).firstMatch
        XCTAssertTrue(result.waitForExistence(timeout: 5))
        XCTAssertFalse(result.label.contains("```"))
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = name
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
