import XCTest

final class WorkTabReturnTests: XCTestCase {
    @MainActor
    func testTaskDetailsDoNotReplaceBotsAfterNotification() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_NOTIFICATION_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_notification_categories.py --work-navigation.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "notification-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 15))
        app.buttons["app-settings"].tap()
        let settings = app.buttons["notification-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        settings.tap()
        if app.buttons["Enable notifications"].exists {
            app.buttons["Enable notifications"].tap()
            let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
            if springboard.buttons["Allow"].waitForExistence(timeout: 5) { springboard.buttons["Allow"].tap() }
        }
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["close-settings"].tap()
        let chat = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch
        XCTAssertTrue(chat.waitForExistence(timeout: 5)); chat.tap()
        app.buttons["chat-task-details"].tap()
        XCTAssertTrue(app.navigationBars["Task details"].waitForExistence(timeout: 5))
        app.buttons["Done"].tap()
        XCUIDevice.shared.press(.home)
        var request = URLRequest(url: URL(string: address + "/test/push?kind=message")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let notification = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Notification fixture message")).firstMatch
        XCTAssertTrue(notification.waitForExistence(timeout: 20))
        notification.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 10))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.tabBars.buttons["Work"].exists)
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(chat.waitForExistence(timeout: 5) && chat.isHittable)
        XCTAssertFalse(app.navigationBars["Task details"].exists)
    }
}
