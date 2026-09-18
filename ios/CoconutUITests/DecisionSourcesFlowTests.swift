import XCTest

final class DecisionSourcesFlowTests: XCTestCase {
    @MainActor
    func testSourcesStayWithTheirCard() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--card-sources-preview", "-AppleInterfaceStyle", "Dark"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertTrue(app.links["Email · Weekly payout failures ↗"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.links["Email · Negative-balance reports ↗"].isHittable)
        XCTAssertTrue(app.buttons["decision-option-include"].isHittable)
        capture(app, "01-email-sources-dark")
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.links["PR · coconut #42 ↗"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.links["Issue · coconut #37 ↗"].isHittable)
        XCTAssertFalse(app.links["Email · Weekly payout failures ↗"].exists)
        capture(app, "02-github-sources-dark")
    }

    @MainActor
    func testCardOutlineInLightMode() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--card-sources-preview", "-AppleInterfaceStyle", "Light"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertTrue(app.links["Email · Weekly payout failures ↗"].waitForExistence(timeout: 5))
        capture(app, "03-email-sources-light")
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
