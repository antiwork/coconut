import XCTest

final class SettingsAccessTests: XCTestCase {
    @MainActor
    func testSettingsOpenFromBothTabs() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        for tab in ["Inbox", "Bots"] {
            app.tabBars.buttons[tab].tap()
            let settings = app.buttons["app-settings"]
            XCTAssertTrue(settings.waitForExistence(timeout: 5), "Settings is missing in \(tab).")
            settings.tap()
            XCTAssertTrue(app.buttons["parallel-work-settings"].waitForExistence(timeout: 5))
            app.buttons["close-settings"].tap()
            XCTAssertTrue(app.tabBars.buttons[tab].waitForExistence(timeout: 5))
        }
    }
}
