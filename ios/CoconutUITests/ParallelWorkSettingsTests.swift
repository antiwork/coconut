import XCTest

final class ParallelWorkSettingsTests: XCTestCase {
    @MainActor
    func testBotsWorkingAtOnceSavesWhenItChangesAndReturnsToAutomatic() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        app.buttons["app-settings"].tap()
        let parallelWork = app.buttons["parallel-work-settings"]
        for _ in 0..<3 where !parallelWork.isHittable { app.swipeUp() }
        parallelWork.tap()
        let limit = app.buttons["work-item-limit"]
        XCTAssertTrue(limit.waitForExistence(timeout: 5))
        XCTAssertTrue(choice(of: limit).contains("Automatic"))
        choose("5", in: limit, app: app)
        XCTAssertTrue(choice(of: limit).contains("5"))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "One control for bots working at once"
        proof.lifetime = .keepAlways
        add(proof)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        parallelWork.tap()
        XCTAssertTrue(limit.waitForExistence(timeout: 5))
        XCTAssertTrue(choice(of: limit).contains("5"))
        choose("Automatic", in: limit, app: app)
        XCTAssertTrue(choice(of: limit).contains("Automatic"))
    }

    // A navigation link picker gives its title as the label and the chosen option as the value.
    @MainActor
    private func choice(of picker: XCUIElement) -> String {
        picker.value as? String ?? ""
    }

    @MainActor
    private func choose(_ label: String, in picker: XCUIElement, app: XCUIApplication) {
        picker.tap()
        let option = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", label)).firstMatch
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        option.tap()
        if !picker.waitForExistence(timeout: 2) { app.navigationBars.buttons.element(boundBy: 0).tap() }
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
    }
}
