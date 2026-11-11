import XCTest

final class GoogleConnectionFlowTests: XCTestCase {
    @MainActor
    func testGoogleSetupLivesInAppSettings() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 10))
        app.buttons["app-settings"].tap()
        let connections = app.buttons["mcp-settings"]
        for _ in 0..<3 where !connections.isHittable { app.swipeUp() }
        connections.tap()
        XCTAssertTrue(app.buttons["google-connect"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["All bots can read Gmail and Calendar. This connection cannot send email or change events."].exists)
        XCTAssertFalse(app.buttons["google-bot"].exists)
        XCTAssertFalse(app.textFields["Client ID"].exists)
        XCTAssertFalse(app.staticTexts["Terminal"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Google connection in Coconut settings"; proof.lifetime = .keepAlways; add(proof)
    }
    @MainActor
    func testSystemSignInReturnsToCoconut() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the Google connection fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 15))
        app.buttons["app-settings"].tap()
        app.buttons["mcp-settings"].tap()
        XCTAssertTrue(app.buttons["google-connect"].waitForExistence(timeout: 5))
        app.buttons["google-connect"].tap()
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        if springboard.buttons["Continue"].waitForExistence(timeout: 3) { springboard.buttons["Continue"].tap() }
        if app.buttons["Continue"].exists { app.buttons["Continue"].tap() }
        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 15))
        XCTAssertTrue(app.webViews.staticTexts["Sign in"].firstMatch.waitForExistence(timeout: 30))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "System Google sign-in"; proof.lifetime = .keepAlways; add(proof)
        print("COCONUT_GOOGLE_STAGE=callback")
        if springboard.buttons["Open"].waitForExistence(timeout: 5) { springboard.buttons["Open"].tap() }
        XCTAssertTrue(app.staticTexts["google-account"].waitForExistence(timeout: 15))
        XCTAssertEqual(app.staticTexts["google-account"].label, "Account, fixture@example.com")
        XCTAssertTrue(app.buttons["google-disconnect"].exists)
        app.buttons["google-disconnect"].tap()
        app.buttons["Disconnect"].tap()
        XCTAssertTrue(app.buttons["google-connect"].waitForExistence(timeout: 5))
    }

}
