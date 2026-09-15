import XCTest

final class WorkFlowTests: XCTestCase {
    @MainActor
    func testConsistentRowsAndFullSwipeDismissal() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_TEST_URL"] else { throw XCTSkip("Run the Work fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        let task = app.buttons["work-item-export-active"]
        let pr = app.buttons["work-pr-42"]
        XCTAssertTrue(task.waitForExistence(timeout: 5))
        XCTAssertTrue(pr.waitForExistence(timeout: 5))
        XCTAssertEqual(task.frame.height, pr.frame.height, accuracy: 1)
        XCTAssertLessThanOrEqual(task.frame.height, 80)
        let completed = app.buttons["work-item-report-complete"]
        let disclosure = app.buttons["Recently completed"]
        if !disclosure.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(disclosure.waitForExistence(timeout: 5)); disclosure.tap()
        if !completed.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(completed.isHittable)
        let start = completed.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5))
        let end = completed.coordinate(withNormalizedOffset: CGVector(dx: 0.02, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end)
        XCTAssertTrue(completed.waitForNonExistence(timeout: 3))
        XCTAssertTrue(work.isSelected)
        app.buttons["work-undo"].tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Consistent Work rows after swipe and undo"
        proof.lifetime = .keepAlways; add(proof)
    }

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
        var decisionRequest = URLRequest(url: URL(string: address + "/test/work-state")!)
        decisionRequest.httpMethod = "POST"
        decisionRequest.httpBody = try JSONSerialization.data(withJSONObject: ["status": "needs_decision", "activity": "Waiting for your decision"])
        _ = try await URLSession.shared.data(for: decisionRequest)
        let review = app.buttons["work-decision-export-active"]
        XCTAssertTrue(review.waitForExistence(timeout: 25)); review.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.tabBars.buttons["Work"].isSelected)
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
        XCTAssertFalse(app.staticTexts["Checks running"].exists)
        XCTAssertFalse(app.staticTexts["Already merged"].exists)
        XCTAssertTrue(app.staticTexts["Export account data"].exists)
        XCTAssertTrue(app.staticTexts["Preparing the export"].exists)
        let completed = app.buttons["work-item-report-complete"]
        let disclosure = app.buttons["Recently completed"]
        if !disclosure.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(disclosure.waitForExistence(timeout: 5)); disclosure.tap()
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
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "work-pr-")).firstMatch.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.tabBars.buttons["Work"].isHittable)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(work.waitForExistence(timeout: 5))
        XCTAssertTrue(work.isSelected)
        var request = URLRequest(url: URL(string: address + "/test/complete")!)
        request.httpMethod = "POST"
        let (_, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 25))
        XCTAssertTrue(work.exists)
        let emptyProof = XCTAttachment(screenshot: app.screenshot())
        emptyProof.name = "Work uses a uniform empty background"
        emptyProof.lifetime = .keepAlways
        add(emptyProof)
    }
}
