import XCTest

final class WorkTabReturnTests: XCTestCase {
    @MainActor
    func testWorkReturnsToItsListAfterNotificationOpensAnotherTab() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_NOTIFICATION_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_notification_categories.py --work-navigation.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "notification-test"]
        app.launch()
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
        app.tabBars.buttons["Work"].tap()
        let item = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "work-item-")).firstMatch
        XCTAssertTrue(item.waitForExistence(timeout: 5))
        item.tap()
        XCTAssertTrue(app.buttons["Remove work context"].waitForExistence(timeout: 5))
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
        XCTAssertTrue(app.tabBars.buttons["Work"].waitForExistence(timeout: 5))
        app.tabBars.buttons["Work"].tap()
        XCTAssertTrue(item.waitForExistence(timeout: 5) && item.isHittable)
        XCTAssertFalse(app.buttons["bot-settings"].exists)
        XCTAssertTrue(app.navigationBars["Work"].exists)
    }
}
