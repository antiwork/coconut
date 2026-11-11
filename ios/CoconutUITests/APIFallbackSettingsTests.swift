import XCTest

final class APIFallbackSettingsTests: XCTestCase {
    @MainActor
    func testFallbackSwitchKeepsThePrimaryModel() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        app.buttons["app-settings"].tap()
        app.staticTexts["Model"].firstMatch.tap()
        let toggle = app.switches["openai-api-fallback"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 5))
        XCTAssertEqual(toggle.value as? String, "0")
        let primary = app.buttons["model-picker"].label
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(toggle.value as? String, "1")
        XCTAssertEqual(app.buttons["model-picker"].label, primary)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "OpenAI API fallback in Model settings"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.staticTexts["Model"].firstMatch.tap()
        XCTAssertEqual(toggle.value as? String, "1")
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(toggle.value as? String, "0")
    }
}
