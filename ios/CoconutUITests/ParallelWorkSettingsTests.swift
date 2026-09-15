import XCTest

final class ParallelWorkSettingsTests: XCTestCase {
    @MainActor
    func testCustomLimitPersistsAndReturnsToAutomatic() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        app.buttons["app-settings"].tap()
        app.buttons["parallel-work-settings"].tap()
        let automatic = app.switches["automatic-workers"]
        XCTAssertTrue(automatic.waitForExistence(timeout: 5))
        XCTAssertEqual(automatic.value as? String, "1")
        automatic.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        let stepper = app.steppers["worker-limit"]
        XCTAssertTrue(stepper.waitForExistence(timeout: 5), app.debugDescription)
        stepper.buttons["worker-limit-Increment"].tap()
        app.buttons["Save limit"].tap()
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["parallel-work-settings"].tap()
        XCTAssertEqual(automatic.value as? String, "0")
        XCTAssertEqual(app.steppers["worker-limit"].buttons["worker-limit-Increment"].value as? String, "5")
        automatic.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(automatic.value as? String, "1")
        XCTAssertFalse(stepper.exists)
    }
}
