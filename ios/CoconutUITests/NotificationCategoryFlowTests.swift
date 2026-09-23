import XCTest

final class NotificationCategoryFlowTests: XCTestCase {
    @MainActor
    func testSettingsAndNotificationDestinations() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_NOTIFICATION_TEST_URL"] else {
            throw XCTSkip("Run the notification fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "notification-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 15))
        app.buttons["app-settings"].tap()
        app.buttons["notification-settings"].tap()
        let decisions = app.switches["decision-notifications"]
        let messages = app.switches["message-notifications"]
        XCTAssertTrue(decisions.waitForExistence(timeout: 5))
        let original = messages.value as? String
        messages.tap()
        XCTAssertNotEqual(messages.value as? String, original)
        XCTAssertEqual(decisions.value as? String, "1")
        messages.tap()
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Separate notification settings"; proof.lifetime = .keepAlways; add(proof)
        if app.buttons["Enable notifications"].exists {
            app.buttons["Enable notifications"].tap()
            let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
            if springboard.buttons["Allow"].waitForExistence(timeout: 5) { springboard.buttons["Allow"].tap() }
        }
        for kind in ["decision", "message"] {
            XCUIDevice.shared.press(.home)
            if kind == "decision" { app.terminate() }
            var request = URLRequest(url: URL(string: address + "/test/push?kind=" + kind)!)
            request.httpMethod = "POST"
            let sent = expectation(description: "Queue notification")
            URLSession.shared.dataTask(with: request) { _, _, _ in sent.fulfill() }.resume()
            await fulfillment(of: [sent], timeout: 5)
            let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
            let notification = springboard.descendants(matching: .any)
                .matching(NSPredicate(format: "label CONTAINS %@", "Notification fixture " + kind)).firstMatch
            XCTAssertTrue(notification.waitForExistence(timeout: 20))
            notification.tap()
            XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
            if kind == "decision" {
                XCTAssertTrue(app.staticTexts["Review the deferred notification?"].waitForExistence(timeout: 15))
                XCTAssertTrue(app.tabBars.buttons["Inbox"].isSelected)
                XCTAssertFalse(app.buttons["bot-settings"].exists)
                XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
            } else {
                XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 10))
                XCTAssertTrue(app.staticTexts["The notification opens this exact reply."].exists)
                XCTAssertFalse(app.tabBars.buttons["Inbox"].isHittable)
            }
            let proof = XCTAttachment(screenshot: app.screenshot())
            proof.name = "Notification opens " + kind; proof.lifetime = .keepAlways; add(proof)
        }
    }
}
