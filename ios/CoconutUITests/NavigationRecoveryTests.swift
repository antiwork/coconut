import XCTest

final class NavigationRecoveryTests: XCTestCase {
    @MainActor
    func testSearchConversationAndBackgroundRestoreNavigationAndKeyboard() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 10))
        for attempt in 0..<3 {
            let search = app.buttons["search-conversations"]
            XCTAssertTrue(search.waitForExistence(timeout: 3) && search.isHittable)
            XCTAssertTrue(app.buttons["app-settings"].isHittable)
            search.tap()
            let searchInput = app.textFields["home-search-input"]
            XCTAssertTrue(searchInput.waitForExistence(timeout: 3))
            searchInput.tap()
            XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 3))
            bot.tap()
            let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
            XCTAssertTrue(input.waitForExistence(timeout: 3))
            XCTAssertFalse(app.buttons["app-settings"].exists)
            XCTAssertFalse(search.exists)
            input.tap()
            input.typeText("Draft \(attempt)")
            XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 3))
            XCUIDevice.shared.press(.home)
            app.activate()
            XCTAssertTrue(input.waitForExistence(timeout: 3))
            input.tap()
            XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 3))
            input.typeText(" retained")
            app.buttons["bot-settings"].tap()
            XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3))
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(input.waitForExistence(timeout: 3))
            XCTAssertTrue((input.value as? String)?.contains("retained") == true)
            XCTAssertGreaterThan(input.frame.maxY, app.frame.maxY - 130,
                                 "A dismissed keyboard must not leave an empty inset.")
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(bot.waitForExistence(timeout: 3))
            XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 3) && app.buttons["app-settings"].isHittable)
            XCTAssertTrue(search.isHittable)
            if app.buttons["close-search"].exists { app.buttons["close-search"].tap() }
            app.tabBars.buttons["Inbox"].tap()
            app.tabBars.buttons["Bots"].tap()
            XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 3))
            if attempt == 2 {
                let proof = XCTAttachment(screenshot: app.screenshot())
                proof.name = "Bots header after navigation and app switching"
                proof.lifetime = .keepAlways
                add(proof)
            }
        }
    }
}
