import XCTest

final class WorkFlowTests: XCTestCase {
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
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Work shows active and merged pull requests"; proof.lifetime = .keepAlways; add(proof)
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
