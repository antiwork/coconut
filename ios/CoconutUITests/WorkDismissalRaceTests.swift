import XCTest

final class WorkDismissalRaceTests: XCTestCase {
    @MainActor
    func testRapidDismissalsSurviveOlderSnapshotsAndMessageReplay() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_RACE_URL"] else {
            throw XCTSkip("Run the Work dismissal race fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "work-test"]
        app.launch()
        let work = app.tabBars.buttons["Work"]
        XCTAssertTrue(work.waitForExistence(timeout: 15)); work.tap()
        for title in ["First completed change", "Second completed change"] {
            let row = app.staticTexts[title]
            XCTAssertTrue(row.waitForExistence(timeout: 5))
            row.swipeLeft()
            app.buttons["Dismiss"].tap()
        }
        try await Task.sleep(for: .seconds(5))
        let (data, _) = try await URLSession.shared.data(from: URL(string: address + "/test/proof")!)
        let proof = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(proof["commits"] as? Int, 2)
        XCTAssertEqual(proof["stale_reads"] as? Int, 2)
        XCTAssertEqual(proof["replayed"] as? Bool, true)
        XCTAssertFalse(app.staticTexts["First completed change"].exists)
        XCTAssertFalse(app.staticTexts["Second completed change"].exists)
        XCTAssertTrue(app.staticTexts["No tracked work"].exists)
    }
}
