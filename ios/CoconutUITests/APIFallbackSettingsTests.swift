import XCTest

final class APIFallbackSettingsTests: XCTestCase {
    @MainActor
    func testFallbackModelKeepsThePrimaryModel() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        app.buttons["app-settings"].tap()
        app.staticTexts["Model"].firstMatch.tap()
        let picker = app.buttons["fallback-model-picker"]
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        XCTAssertTrue(shown(picker).contains("None"), shown(picker))
        let primary = app.buttons["model-picker"].label
        picker.tap()
        // The default model is not a choice for its own fallback.
        let focused = app.buttons["Preview Focused, preview"]
        XCTAssertTrue(focused.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Preview Standard, preview"].exists)
        focused.tap()
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        XCTAssertTrue(shown(picker).contains("Preview Focused"), shown(picker))
        XCTAssertEqual(app.buttons["model-picker"].label, primary)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Fallback model in Model settings"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.staticTexts["Model"].firstMatch.tap()
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        XCTAssertTrue(shown(picker).contains("Preview Focused"), shown(picker))
        picker.tap()
        XCTAssertTrue(app.buttons["None"].waitForExistence(timeout: 5))
        app.buttons["None"].tap()
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        XCTAssertTrue(shown(picker).contains("None"), shown(picker))
    }

    // A picker row shows its choice in its label or in its value.
    private func shown(_ element: XCUIElement) -> String {
        element.label + " " + ((element.value as? String) ?? "")
    }
}
