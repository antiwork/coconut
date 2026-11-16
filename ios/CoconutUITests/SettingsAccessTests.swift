import XCTest

final class SettingsAccessTests: XCTestCase {
    /// Coconut has no Settings screen. Home is the launch tab, and the things of this device open from it in one tap
    /// on the tab bar.
    @MainActor
    func testHomeIsTheLaunchTabAndChatsStaysOneTapAway() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let home = app.tabBars.buttons["Home"]
        XCTAssertTrue(home.waitForExistence(timeout: 15))
        XCTAssertTrue(home.isSelected, "Home must be the launch screen.")
        XCTAssertFalse(app.tabBars.buttons["Inbox"].exists, "The Inbox is a part of Home.")
        XCTAssertTrue(app.buttons["home-review-cards"].waitForExistence(timeout: 5), "Home must lead to the cards.")
        app.tabBars.buttons["Chats"].tap()
        XCTAssertTrue(app.buttons["coconut-chat"].waitForExistence(timeout: 5) || app.buttons["create-bot"].waitForExistence(timeout: 5))
    }
}
