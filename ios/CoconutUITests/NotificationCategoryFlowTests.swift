import XCTest

final class NotificationCategoryFlowTests: XCTestCase {
    @MainActor
    func testWorkHoursSettings() throws {
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

        func control(_ id: String) -> XCUIElement { app.descendants(matching: .any)[id] }
        func attach(_ name: String) {
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.name = name; shot.lifetime = .keepAlways; add(shot)
        }

        let summary = control("notification-summary")
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        XCTAssertTrue(summary.label.hasPrefix("From 9:00 to 18:00"), summary.label)
        XCTAssertTrue(control("notification-level").exists)
        attach("Notification settings")
        let form = app.collectionViews.firstMatch
        form.swipeUp()
        let nothing = app.buttons["Nothing"]
        XCTAssertTrue(nothing.waitForExistence(timeout: 5))
        nothing.tap()
        attach("Notification settings - Lower part")
        form.swipeDown()
        XCTAssertTrue(summary.waitForExistence(timeout: 5))
        XCTAssertTrue(summary.label.contains("Outside those hours, nothing notifies you."), summary.label)
        attach("Notification settings - Nothing outside work hours")
        form.swipeUp()
        app.buttons.matching(NSPredicate(format: "label == %@", "Only urgent")).element(boundBy: 1).tap()
        for id in ["message-notifications", "urgent-cards", "last-notification"] {
            XCTAssertTrue(control(id).waitForExistence(timeout: 5), id)
        }
        attach("Notification settings - Bot replies and urgent")
        app.buttons["urgent-cards"].tap()
        XCTAssertTrue(control("urgent-week").waitForExistence(timeout: 5))
        XCTAssertEqual(control("urgent-week").label, "Last week: 4 urgent of 358 cards")
        attach("What counts as urgent")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        form.swipeDown()
        app.buttons["work-hours"].tap()
        for id in ["work-hours-start", "work-hours-end", "work-hours-timezone", "work-days-disclosure"] {
            XCTAssertTrue(control(id).waitForExistence(timeout: 5), id)
        }
        attach("Work hours")

        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"] {
            XCTAssertTrue(app.switches["work-day-" + day].waitForExistence(timeout: 5), day)
        }
        let monday = app.switches["work-day-monday"]
        let original = monday.value as? String
        monday.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)).tap()
        expectation(for: NSPredicate(format: "value != %@", original ?? ""), evaluatedWith: monday)
        waitForExpectations(timeout: 2)
        monday.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)).tap()
        expectation(for: NSPredicate(format: "value == %@", original ?? ""), evaluatedWith: monday)
        waitForExpectations(timeout: 2)
        app.swipeUp()
        XCTAssertTrue(app.switches["work-day-sunday"].waitForExistence(timeout: 5), "sunday")
        attach("Work hours - Days")
    }

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
        let messages = app.switches["message-notifications"]
        XCTAssertTrue(messages.waitForExistence(timeout: 5))
        for level in ["every", "grouped", "urgent_only"] {
            XCTAssertTrue(app.descendants(matching: .any)["notification-level-" + level].exists, level)
        }
        let original = messages.value as? String
        messages.tap()
        XCTAssertNotEqual(messages.value as? String, original)
        messages.tap()
        XCTAssertTrue(app.descendants(matching: .any)["notification-summary"].exists)
        for choice in ["urgent", "nothing", "same"] {
            XCTAssertTrue(app.descendants(matching: .any)["outside-work-hours-" + choice].exists, choice)
        }
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
