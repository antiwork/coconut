import XCTest

final class ConcurrentWorkSettingsTests: XCTestCase {
    @MainActor
    func testWorkCapacityDoesNotChangeTheHelperLimit() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        app.buttons["app-settings"].tap()
        app.buttons["parallel-work-settings"].tap()
        let automatic = app.switches["automatic-work-items"]
        XCTAssertTrue(automatic.waitForExistence(timeout: 5))
        automatic.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        let stepper = app.steppers["work-item-limit"]
        XCTAssertTrue(stepper.waitForExistence(timeout: 5))
        stepper.buttons["work-item-limit-Increment"].tap()
        app.buttons["Save work limit"].tap()
        XCTAssertEqual(app.switches["automatic-workers"].value as? String, "1")
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Separate limits for work items and Hermes helpers"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["parallel-work-settings"].tap()
        XCTAssertEqual(automatic.value as? String, "0")
        XCTAssertEqual(app.steppers["work-item-limit"].buttons["work-item-limit-Increment"].value as? String, "5")
    }
}
