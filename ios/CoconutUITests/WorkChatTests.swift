import XCTest

final class WorkChatTests: XCTestCase {
    @MainActor
    func testAttachRemoveAskAndGetUpdate() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_URL"],
              let code = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_CODE"],
              let itemId = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_ID"] else {
            throw XCTSkip("Run scripts/test_work_chat.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Work"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Work"].tap()
        let item = app.buttons["work-item-\(itemId)"]
        XCTAssertTrue(item.waitForExistence(timeout: 10))
        item.tap()
        XCTAssertTrue(app.buttons["Remove work context"].waitForExistence(timeout: 5))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Work attached to the composer"; proof.lifetime = .keepAlways; add(proof)
        app.buttons["Remove work context"].tap()
        XCTAssertFalse(app.buttons["Remove work context"].exists)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        item.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        input.tap(); input.typeText("What remains?")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["The export needs its final check."].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["Remove work context"].exists)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        item.press(forDuration: 1)
        app.buttons["Get update"].tap()
        XCTAssertTrue(app.staticTexts["The export is prepared. Its final check is next."].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["Remove work context"].exists)
    }
}
