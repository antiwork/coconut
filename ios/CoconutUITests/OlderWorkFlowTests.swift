import XCTest

final class OlderWorkFlowTests: XCTestCase {
    @MainActor
    func testTaskDetailsLoadsOlderFinishedWorkOnRequest() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let address = environment["COCONUT_OLDER_WORK_URL"], let code = environment["COCONUT_OLDER_WORK_CODE"],
              let botId = environment["COCONUT_OLDER_WORK_BOT_ID"], let title = environment["COCONUT_OLDER_WORK_TITLE"] else {
            throw XCTSkip("Run scripts/test_older_work.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        // A pinned bot opens from the top of the list.
        let chat = app.buttons.matching(NSPredicate(format: "identifier IN %@",
                                                    ["bot-conversation-" + botId, "pinned-bot-" + botId])).firstMatch
        XCTAssertTrue(chat.waitForExistence(timeout: 15))
        chat.tap()
        app.buttons["chat-task-details"].tap()
        XCTAssertTrue(app.navigationBars["Task details"].waitForExistence(timeout: 5))
        let older = app.buttons["load-older-tasks"]
        XCTAssertTrue(older.waitForExistence(timeout: 5))
        for _ in 0..<60 where !older.isHittable { app.swipeUp() }
        XCTAssertTrue(older.isHittable)
        XCTAssertEqual(older.label, "Show older")
        XCTAssertFalse(app.staticTexts[title].exists, "The recent state leaves out Work that finished more than a week ago.")
        older.tap()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: 15))
        XCTAssertFalse(older.exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Older finished Work"; proof.lifetime = .keepAlways; add(proof)
    }
}
