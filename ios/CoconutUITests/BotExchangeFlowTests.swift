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
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let scout = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(scout.waitForExistence(timeout: 15))
        XCTAssertFalse(scout.label.contains("[SILENT]"))
        scout.tap()
        XCTAssertTrue(app.staticTexts["My own work stays here."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["[SILENT]"].exists)
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
        let releases = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(releases.waitForExistence(timeout: 5))
        releases.tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["My own work stays here."].exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "The check passed")).firstMatch.exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "let answer = 42")).firstMatch.exists)
        var request = URLRequest(url: URL(string: url + "/test/exchange")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        let updated = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@ AND label CONTAINS %@", "bot-exchange-", "3 messages with")).firstMatch
        XCTAssertTrue(updated.waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Releases"))
        XCTAssertFalse(app.navigationBars["Task"].exists)
        XCTAssertFalse(app.staticTexts["A new peer reply."].exists)
        updated.tap()
        assertExchange(app)
        XCTAssertTrue(app.staticTexts["A new peer reply."].exists)
        capture(app, "Recipient exchange after a live reply")
    }

    @MainActor
    func testLongExchangeOpensAtLatestAndDoesNotPullOlderReading() async throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py with --exchange.")
        }
        continueAfterFailure = false
        var request = URLRequest(url: URL(string: url + "/test/exchange-history")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let scout = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(scout.waitForExistence(timeout: 15)); scout.tap()
        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-exchange-")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 5)); row.tap()
        let latest = app.staticTexts["A new peer reply."]
        let latestExists = latest.waitForExistence(timeout: 2)
        let lastResult = latestExists ? latest : app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "let answer = 42")).firstMatch
        XCTAssertTrue(lastResult.waitForExistence(timeout: 5) && lastResult.isHittable)
        XCTAssertFalse(app.buttons["Latest messages"].exists)
        capture(app, "Long exchange opens at the latest message")
        let scroll = app.scrollViews.firstMatch
        scroll.swipeDown(); scroll.swipeDown()
        let anchor = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Older exchange")).allElementsBoundByIndex.first { $0.isHittable }
        let reading = try XCTUnwrap(anchor)
        let before = reading.frame.minY
        let previousReplies = app.staticTexts.matching(identifier: "A new peer reply.").count
        request.url = URL(string: url + "/test/exchange")!
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.staticTexts.matching(identifier: "A new peer reply.").element(boundBy: previousReplies).waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Latest messages"].waitForExistence(timeout: 5))
        XCTAssertEqual(reading.frame.minY, before, accuracy: 2)
        capture(app, "New reply preserves older reading position")
        app.buttons["Latest messages"].tap()
        let newReply = app.staticTexts["A new peer reply."].firstMatch
        XCTAssertTrue(newReply.waitForExistence(timeout: 5) && newReply.isHittable)
        XCTAssertFalse(app.descendants(matching: .any).matching(identifier: "message-input").firstMatch.exists)
    }

    @MainActor
    private func assertExchange(_ app: XCUIApplication) {
        XCTAssertFalse(app.staticTexts["[SILENT]"].exists)
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
