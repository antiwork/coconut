import XCTest

final class DecisionFollowupFlowTests: XCTestCase {
    @MainActor
    func testFollowupShowsTheLimitAndLinksToTheAnswer() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_FOLLOWUP_URL"], let code = env["COCONUT_FOLLOWUP_CODE"],
              let id = env["COCONUT_FOLLOWUP_ID"], let previousId = env["COCONUT_PREVIOUS_ID"] else {
            throw XCTSkip("Run scripts/test_decision_followups.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        let previous = app.buttons["previous-decision-\(id)"]
        XCTAssertTrue(previous.waitForExistence(timeout: 10))
        XCTAssertTrue(previous.label.contains("Check past decisions"))
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "The original ruling remains unavailable.")).firstMatch.exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Follow-up with earlier choice and evidence limit"
        proof.lifetime = .keepAlways
        add(proof)
        previous.tap()
        let receipt = app.staticTexts["decision-selection-\(previousId)"]
        XCTAssertTrue(receipt.waitForExistence(timeout: 5))
        XCTAssertEqual(receipt.label, "Check past decisions")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["inbox-context-\(id)"].tap()
        XCTAssertTrue(previous.waitForExistence(timeout: 5))
        previous.tap()
        XCTAssertTrue(receipt.isHittable)
    }
}
