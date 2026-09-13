import XCTest

final class WorkFlowTests: XCTestCase {
    @MainActor
    func testPersistentWorkAndInboxBadge() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_TEST_URL"] else { throw XCTSkip("Run the Work fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let inbox = app.tabBars.buttons["Inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 15))
        app.tabBars.buttons["Work"].tap()
        let item = app.buttons["work-item-export-active"]
        XCTAssertTrue(item.waitForExistence(timeout: 15))
        for (status, activity) in [("waiting", "Waiting for another bot"), ("working", "Preparing the export")] {
            var request = URLRequest(url: URL(string: address + "/test/work-state")!)
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: ["status": status, "activity": activity])
            _ = try await URLSession.shared.data(for: request)
            XCTAssertTrue(app.staticTexts[activity].waitForExistence(timeout: 25))
            XCTAssertTrue(item.exists)
        }
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Persistent Work and pending Inbox badge"; proof.lifetime = .keepAlways; add(proof)
        var request = URLRequest(url: URL(string: address + "/test/work-state")!)
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: ["status": "working", "activity": "Checking the completed export", "resolved": true])
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.staticTexts["Checking the completed export"].waitForExistence(timeout: 25))
        let resolvedProof = XCTAttachment(screenshot: app.screenshot())
        resolvedProof.name = "Inbox badge clears after the decision is resolved"
        resolvedProof.lifetime = .keepAlways; add(resolvedProof)
        request.httpBody = try JSONSerialization.data(withJSONObject: ["status": "working", "activity": "Preparing the export"])
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.staticTexts["Preparing the export"].waitForExistence(timeout: 25))
    }

    @MainActor
    func testWorkListAndConversationNavigation() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_TEST_URL"] else { throw XCTSkip("Run the Work fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15))
        work.tap()
        XCTAssertTrue(app.staticTexts["Fix the refund email"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts.matching(identifier: "Fix the refund email").count, 1)
        XCTAssertTrue(app.staticTexts["Checks running"].exists)
        XCTAssertTrue(app.staticTexts["Checks failed"].exists)
        XCTAssertTrue(app.staticTexts["Status out of date"].exists)
        XCTAssertTrue(app.staticTexts["Already merged"].exists)
        XCTAssertTrue(app.staticTexts["Merged"].exists)
        XCTAssertTrue(app.staticTexts["Export account data"].exists)
        XCTAssertTrue(app.staticTexts["Preparing the export"].exists)
        let completed = app.buttons["work-item-report-complete"]
        if !completed.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        completed.swipeLeft()
        app.buttons["Dismiss"].tap()
        XCTAssertTrue(completed.waitForNonExistence(timeout: 5))
        app.buttons["work-undo"].tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        app.collectionViews.firstMatch.swipeDown()
        app.buttons["work-item-export-active"].tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(work.waitForExistence(timeout: 5))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Work shows tasks and pull requests"; proof.lifetime = .keepAlways; add(proof)
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "work-bot-")).firstMatch.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.tabBars.buttons["Work"].isHittable)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(work.waitForExistence(timeout: 5))
        XCTAssertTrue(work.isSelected)
        var request = URLRequest(url: URL(string: address + "/test/complete")!)
        request.httpMethod = "POST"
        let (_, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertTrue(app.staticTexts["No tracked work"].waitForExistence(timeout: 25))
        XCTAssertTrue(work.exists)
        let emptyProof = XCTAttachment(screenshot: app.screenshot())
        emptyProof.name = "Work uses a uniform empty background"
        emptyProof.lifetime = .keepAlways
        add(emptyProof)
    }
}
