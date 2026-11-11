import XCTest

final class SettingsAccessTests: XCTestCase {
    @MainActor
    func testSettingsOpenFromBothTabs() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        for tab in ["Inbox", "Chats"] {
            app.tabBars.buttons[tab].tap()
            let settings = app.buttons["app-settings"]
            XCTAssertTrue(settings.waitForExistence(timeout: 5), "Settings is missing in \(tab).")
            settings.tap()
            XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
            // Parallel work is the last row. The list makes a row only when the row scrolls into view.
            let parallelWork = app.buttons["parallel-work-settings"]
            for _ in 0..<3 where !parallelWork.isHittable { app.swipeUp() }
            XCTAssertTrue(parallelWork.waitForExistence(timeout: 5), "Settings from \(tab) must reach Parallel work.")
            app.buttons["close-settings"].tap()
            XCTAssertTrue(app.tabBars.buttons[tab].waitForExistence(timeout: 5))
        }
    }
}
