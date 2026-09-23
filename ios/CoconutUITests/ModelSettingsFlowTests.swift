import XCTest

final class ModelSettingsFlowTests: XCTestCase {
    private let forgeId = "00000000-0000-4000-8000-000000000001"

    @MainActor
    func testBotModelOverridePersistsAndCanReturnToHostDefault() {
        continueAfterFailure = false
        let app = launchPreview()
        openBotModelSettings(in: app)
        assertInheritedModel("Preview Standard", effort: "Medium", in: app)

        setUseDefault(false, in: app)
        select("Preview Focused", in: "model-picker", app: app)
        assertRow("model-picker", contains: "Preview Focused", in: app)
        select("Low", in: "model-effort-picker", app: app)
        assertRow("model-picker", contains: "Preview Focused", in: app)
        assertRow("model-effort-picker", contains: "Low", in: app)
        save(in: app)
        capture("Saved custom model and effort")

        reopenBotModelSettings(in: app)
        XCTAssertEqual(app.switches["use-default-model"].value as? String, "0")
        assertRow("model-picker", contains: "Preview Focused", in: app)
        assertRow("model-effort-picker", contains: "Low", in: app)

        setUseDefault(true, in: app)
        save(in: app)
        reopenBotModelSettings(in: app)
        assertInheritedModel("Preview Standard", effort: "Medium", in: app)
        XCTAssertFalse(app.buttons["model-picker"].exists)
        capture("Bot inherits the host model again")
    }

    @MainActor
    func testHostModelChangeUpdatesInheritedModelAndDefaultEffort() {
        continueAfterFailure = false
        let app = launchPreview()
        app.buttons["app-settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        app.staticTexts["Model"].firstMatch.tap()
        XCTAssertTrue(app.buttons["model-picker"].waitForExistence(timeout: 5))

        select("Preview Focused", in: "model-picker", app: app)
        assertRow("model-picker", contains: "Preview Focused", in: app)
        assertRow("model-effort-picker", contains: "Model default", in: app)
        assertRow("model-effort-picker", contains: "High", in: app)
        save(in: app)

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        app.buttons["close-settings"].tap()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 5))
        openBotModelSettings(in: app)
        assertInheritedModel("Preview Focused", effort: "High", in: app)
        capture("Bot inherits the changed host model and effort")
    }

    @MainActor
    private func launchPreview() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 10))
        return app
    }

    @MainActor
    private func openBotModelSettings(in app: XCUIApplication) {
        app.buttons["app-settings"].tap()
        let bot = app.buttons["settings-bot-\(forgeId)"]
        for _ in 0..<5 where !bot.isHittable { app.swipeUp() }
        XCTAssertTrue(bot.waitForExistence(timeout: 5))
        bot.tap()
        openModelFromBotSettings(in: app)
    }

    @MainActor
    private func openModelFromBotSettings(in app: XCUIApplication) {
        app.buttons["Model"].tap()
        XCTAssertTrue(app.switches["use-default-model"].waitForExistence(timeout: 5))
    }

    @MainActor
    private func reopenBotModelSettings(in app: XCUIApplication) {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        openModelFromBotSettings(in: app)
    }

    @MainActor
    private func setUseDefault(_ enabled: Bool, in app: XCUIApplication) {
        let toggle = app.switches["use-default-model"]
        XCTAssertEqual(toggle.value as? String, enabled ? "0" : "1")
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(toggle.value as? String, enabled ? "1" : "0")
    }

    @MainActor
    private func select(_ label: String, in identifier: String, app: XCUIApplication) {
        let picker = app.buttons[identifier]
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        picker.tap()
        capture("Choices for \(identifier)")
        let option = identifier == "model-effort-picker" ? app.buttons[label].firstMatch : app.staticTexts[label].firstMatch
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        option.tap()
        let save = app.buttons["save-model-settings"]
        if !save.isHittable { app.navigationBars.buttons.element(boundBy: 0).tap() }
        XCTAssertTrue(save.waitForExistence(timeout: 5))
    }

    @MainActor
    private func save(in app: XCUIApplication) {
        let save = app.buttons["save-model-settings"]
        XCTAssertTrue(save.isEnabled, "The edited model settings must enable Save.")
        save.tap()
        let saved = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label CONTAINS %@ AND enabled == false", "Preview updated"), object: save
        )
        XCTAssertEqual(XCTWaiter.wait(for: [saved], timeout: 5), .completed,
                       "Save must show its completed state before the view closes.")
    }

    @MainActor
    private func assertInheritedModel(_ model: String, effort: String, in app: XCUIApplication) {
        XCTAssertEqual(app.switches["use-default-model"].value as? String, "1")
        assertRow("inherited-model", contains: model, in: app)
        assertRow("inherited-effort", contains: effort, in: app)
    }

    @MainActor
    private func assertRow(_ identifier: String, contains text: String, in app: XCUIApplication) {
        let row = app.descendants(matching: .any).matching(identifier: identifier).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        let labels = [row.label, row.value as? String ?? ""]
            + row.staticTexts.allElementsBoundByIndex.map(\.label)
        XCTAssertTrue(labels.joined(separator: " ").contains(text), "\(identifier) must show \(text).")
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
