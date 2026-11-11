import XCTest

final class NotificationOpenPerformanceTests: XCTestCase {
    @MainActor
    func testColdNotificationShowsCachedCardWhileStateRequestWaits() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_NOTIFICATION_TEST_URL"] else {
            throw XCTSkip("Run the notification fixture with --slow-open.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "notification-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 15))
        app.buttons["app-settings"].tap()
        app.buttons["notification-settings"].tap()
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        if app.buttons["Enable notifications"].exists {
            app.buttons["Enable notifications"].tap()
            if springboard.buttons["Allow"].waitForExistence(timeout: 5) { springboard.buttons["Allow"].tap() }
        }
        XCUIDevice.shared.press(.home)
        app.terminate()
        var request = URLRequest(url: URL(string: address + "/test/push?kind=decision")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        let notification = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "Notification fixture decision")).firstMatch
        XCTAssertTrue(notification.waitForExistence(timeout: 15))
        notification.tap()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 5))
        XCTAssertTrue(app.staticTexts["Review the deferred notification?"].waitForExistence(timeout: 1))
        XCTAssertTrue(app.tabBars.buttons["Inbox"].isSelected)
        let choice = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-option-")).firstMatch
        XCTAssertTrue(choice.exists)
        XCTAssertFalse(choice.isEnabled, "A cached notification card must wait for current state before accepting a choice.")
        request.url = URL(string: address + "/test/release-state")!
        _ = try await URLSession.shared.data(for: request)
        let enabled = NSPredicate { _, _ in choice.isEnabled }
        await fulfillment(of: [XCTNSPredicateExpectation(predicate: enabled, object: nil)], timeout: 5)
    }
}
