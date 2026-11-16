import XCTest

final class DecisionPreviewFlowTests: XCTestCase {
    @MainActor
    func testImagesOpenInInboxAndChatWithoutSwipingTheCard() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_PREVIEW_URL"], let code = env["COCONUT_PREVIEW_CODE"],
              let id = env["COCONUT_PREVIEW_DECISION"], let firstId = env["COCONUT_PREVIEW_FIRST"],
              let secondId = env["COCONUT_PREVIEW_SECOND"] else {
            throw XCTSkip("Run scripts/test_decision_previews.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launchArguments = ["-AppleInterfaceStyle", "Dark"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Home"].waitForExistence(timeout: 15))
        app.openCards()
        let first = app.buttons["decision-preview-\(firstId)"]
        XCTAssertTrue(first.waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["decision-option-approve"].waitForExistence(timeout: 5))
        waitUntilEnabled(first)
        capture(app, "Images inside the decision")
        first.swipeLeft()
        let second = app.buttons["decision-preview-\(secondId)"]
        capture(app, "After swiping the images")
        XCTAssertTrue(second.isHittable)
        XCTAssertTrue(app.buttons["inbox-context-\(id)"].isHittable)
        second.tap()
        let done = app.buttons["Done"]
        XCTAssertTrue(done.waitForExistence(timeout: 5))
        let image = app.images["QLImageItemViewControllerImageViewAccessibilityIdentifier"]
        XCTAssertTrue(image.waitForExistence(timeout: 10))
        let originalWidth = image.frame.width
        image.pinch(withScale: 2, velocity: 1)
        XCTAssertGreaterThan(image.frame.width, originalWidth)
        XCTAssertFalse(app.buttons["Markup"].exists)
        capture(app, "Full-screen preview")
        done.tap()
        app.buttons["inbox-context-\(id)"].tap()
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        waitUntilEnabled(first)
        if !first.isHittable { app.swipeDown() }
        first.tap()
        XCTAssertTrue(done.waitForExistence(timeout: 5))
        done.tap()
    }

    @MainActor
    private func waitUntilEnabled(_ element: XCUIElement) {
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 15), .completed)
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = name
        proof.lifetime = .keepAlways
        add(proof)
    }
}
