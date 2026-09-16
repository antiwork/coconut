import XCTest

final class WorkFlowTests: XCTestCase {
    @MainActor
    func testCancellationShowsProgressFailureAndConfirmedStateBeforeRefresh() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_TEST_URL"] else { throw XCTSkip("Run the Work fixture.") }
        continueAfterFailure = false
        func configure(fail: Bool, delay: Int = 3, refreshDelay: Int = 12) async throws {
            var request = URLRequest(url: URL(string: address + "/test/cancellation")!)
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: ["delay": delay, "refresh_delay": refreshDelay, "fail": fail])
            _ = try await URLSession.shared.data(for: request)
        }
        try await configure(fail: true)
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        let item = app.buttons["work-item-long-queued"]
        let menu = app.buttons["work-menu-long-queued"]
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
        if !menu.isHittable { app.collectionViews.firstMatch.swipeUp() }
        menu.tap(); app.buttons["Cancel work"].tap()
        XCTAssertTrue(item.staticTexts["Cancelling…"].waitForExistence(timeout: 2))
        let alert = app.alerts["Cancellation not confirmed"]
        XCTAssertTrue(alert.waitForExistence(timeout: 5))
        XCTAssertTrue(item.exists)
        alert.buttons["OK"].tap()
        try await configure(fail: false)
        menu.tap(); app.buttons["Cancel work"].tap()
        XCTAssertTrue(item.staticTexts["Cancelling…"].waitForExistence(timeout: 2))
        XCTAssertTrue(item.waitForNonExistence(timeout: 5), "The receipt must update Work before the delayed refresh.")
        XCTAssertTrue(app.buttons["work-item-export-active"].exists)
        XCTAssertTrue(work.isSelected)
        try await configure(fail: false, delay: 0, refreshDelay: 0)
    }

    @MainActor
    func testActiveWorkMovesToTopWhenItsStateChanges() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_TEST_URL"] else { throw XCTSkip("Run the Work fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        let item = app.buttons["work-item-export-active"]
        let pr = app.buttons["work-pr-42"]
        XCTAssertTrue(item.waitForExistence(timeout: 5))
        var request = URLRequest(url: URL(string: address + "/test/order")!)
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: ["export_working": true])
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.staticTexts["Export moved into execution"].waitForExistence(timeout: 25))
        let top = NSPredicate { _, _ in item.frame.minY < pr.frame.minY }
        let moved = XCTNSPredicateExpectation(predicate: top, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [moved], timeout: 5), .completed)
        XCTAssertTrue(app.activityIndicators["work-progress-export-active"].exists)
        XCTAssertFalse(app.staticTexts["Needs you"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Running work moves above queued work"; proof.lifetime = .keepAlways; add(proof)
        request.httpBody = try JSONSerialization.data(withJSONObject: ["export_working": false])
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.staticTexts["Preparing the export"].waitForExistence(timeout: 25))
    }

    @MainActor
    func testWrappingRowsAndFullSwipeDismissal() throws {
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
        let long = app.buttons["work-item-long-queued"]
        if !long.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(long.waitForExistence(timeout: 5))
        let title = app.staticTexts["Detect failed UPI purchases with late payments and verify that recovery does not charge the buyer again"]
        XCTAssertGreaterThan(title.frame.height, 45)
        XCTAssertGreaterThan(long.frame.height, task.frame.height)
        XCTAssertFalse(app.activityIndicators["work-progress-long-queued"].exists)
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "3rd in")).firstMatch.exists)
        let completed = app.buttons["work-item-report-complete"]
        let disclosure = app.buttons["work-completed"]
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
        proof.name = "Work with full titles after swipe and undo"
        proof.lifetime = .keepAlways; add(proof)
        let menu = app.buttons["work-menu-long-queued"]
        if !menu.isHittable { app.collectionViews.firstMatch.swipeDown() }
        menu.tap()
        app.buttons["Cancel work"].tap()
        XCTAssertTrue(long.staticTexts["Stopped"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["work-item-export-active"].exists)
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
        let waiting = item.staticTexts["Save the retry fix?"]
        if !item.isHittable { app.collectionViews.firstMatch.swipeUp() }
        XCTAssertTrue(waiting.waitForExistence(timeout: 25))
        XCTAssertFalse(app.staticTexts["Needs you"].exists)
        if !item.isHittable { app.collectionViews.firstMatch.swipeUp() }
        item.tap()
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
    func testZWorkListAndConversationNavigation() async throws {
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
        let disclosure = app.buttons["work-completed"]
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
