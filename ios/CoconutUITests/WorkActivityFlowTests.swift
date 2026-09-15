import XCTest

final class WorkActivityFlowTests: XCTestCase {
    @MainActor
    func testActivityUpdatesKeepRowsInPlace() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_ACTIVITY_URL"] else { throw XCTSkip("Run the Work activity fixture.") }
        var reset = URLRequest(url: URL(string: address + "/test/activity")!)
        reset.httpMethod = "POST"
        reset.httpBody = try JSONSerialization.data(withJSONObject: [
            "title": "Review the refund change", "activity": "Reviewing with Fable", "activity_state": "working"])
        _ = try await URLSession.shared.data(for: reset)
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        let row = app.buttons["work-pr-42"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        let original = row.frame
        let spinner = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH %@", "work-progress-")).firstMatch
        XCTAssertTrue(spinner.exists)
        var request = URLRequest(url: URL(string: address + "/test/activity")!)
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "title": "Generate product file archives after the editor save commits",
            "activity": "Waiting for the final recovery test before checking the saved archive",
            "activity_state": "waiting"])
        _ = try await URLSession.shared.data(for: request)
        let changed = app.staticTexts["Waiting for the final recovery test before checking the saved archive"]
        XCTAssertTrue(changed.waitForExistence(timeout: 25))
        XCTAssertEqual(row.frame.height, original.height, accuracy: 1)
        XCTAssertFalse(spinner.exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Work with separate checks and stable activity"; proof.lifetime = .keepAlways; add(proof)
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "title": "Review the refund change", "activity": "Reviewing with Fable", "activity_state": "working"])
        _ = try await URLSession.shared.data(for: request)
    }

    @MainActor
    func testQueuedUndoSurvivesBackgroundAndReconnect() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_ACTIVITY_URL"] else { throw XCTSkip("Run the Work activity fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        let completed = app.staticTexts["Update the help article"]
        app.buttons["Recently completed"].tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        var request = URLRequest(url: URL(string: address + "/test/delay-next")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        completed.swipeLeft(); app.buttons["Dismiss"].tap()
        let undo = app.buttons["work-undo"]
        XCTAssertTrue(undo.waitForExistence(timeout: 2)); undo.tap()
        XCUIDevice.shared.press(.home)
        try await Task.sleep(for: .seconds(9))
        app.activate()
        XCTAssertTrue(completed.waitForExistence(timeout: 10))
        let (data, _) = try await URLSession.shared.data(from: URL(string: address + "/v1/state")!)
        let state = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let artifacts = try XCTUnwrap(state["artifacts"] as? [[String: Any]])
        let merged = try XCTUnwrap(artifacts.first { $0["pr_state"] as? String == "merged" })
        XCTAssertTrue(merged["work_dismissal_id"] == nil || merged["work_dismissal_id"] is NSNull)
    }

    @MainActor
    func testActivityDismissUndoFailureAndChatTimer() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_ACTIVITY_URL"] else { throw XCTSkip("Run the Work activity fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15))
        work.tap()
        XCTAssertTrue(app.staticTexts["Reviewing with Fable"].waitForExistence(timeout: 10))
        let active = app.staticTexts["Review the refund change"]
        let initialY = active.frame.minY
        let completed = app.staticTexts["Update the help article"]
        app.buttons["Recently completed"].tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        completed.swipeLeft()
        app.buttons["Dismiss"].tap()
        let undo = app.buttons["work-undo"]
        XCTAssertTrue(undo.waitForExistence(timeout: 2))
        XCTAssertEqual(active.frame.minY, initialY, accuracy: 2)
        undo.tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        try await Task.sleep(for: .seconds(3))
        XCTAssertTrue(completed.exists)
        completed.swipeLeft()
        app.buttons["Dismiss"].tap()
        try await Task.sleep(for: .seconds(3))
        XCTAssertFalse(completed.exists)
        undo.tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 5))
        var request = URLRequest(url: URL(string: address + "/test/fail-next")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        try await Task.sleep(for: .seconds(2))
        completed.swipeLeft()
        app.buttons["Dismiss"].tap()
        XCTAssertTrue(completed.waitForExistence(timeout: 6))
        let dismissError = app.buttons["Dismiss error"]
        XCTAssertTrue(dismissError.waitForExistence(timeout: 3))
        dismissError.tap()
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Compact activity and restored completed work"; proof.lifetime = .keepAlways; add(proof)
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "work-pr-")).firstMatch.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Open pull request"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Checked"].exists)
    }
}
