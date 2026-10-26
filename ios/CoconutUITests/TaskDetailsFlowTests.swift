import XCTest

final class TaskDetailsFlowTests: XCTestCase {
    @MainActor
    func testNativeTaskDetailsReplaceWorkAndKeepCancellation() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_URL"],
              let code = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_CODE"],
              let itemId = ProcessInfo.processInfo.environment["COCONUT_WORK_CHAT_ID"],
              let botId = ProcessInfo.processInfo.environment["COCONUT_TASK_BOT_ID"] else {
            throw XCTSkip("Run scripts/test_task_details.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.tabBars.buttons["Work"].exists)
        XCTAssertTrue(app.tabBars.buttons["Inbox"].exists)
        let chat = app.buttons["bot-conversation-" + botId]
        XCTAssertTrue(chat.waitForExistence(timeout: 5))
        chat.tap()
        app.buttons["chat-task-details"].tap()
        XCTAssertTrue(app.navigationBars["Task details"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Export account data"].exists)
        XCTAssertFalse(app.staticTexts["Obsolete progress label"].exists)
        // Stop lives in the row's swipe actions and context menu, and it always asks first.
        let row = app.descendants(matching: .any)["task-detail-" + itemId]
        let stop = app.buttons["task-stop-" + itemId]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.swipeLeft()
        XCTAssertTrue(stop.waitForExistence(timeout: 3))
        stop.tap()
        XCTAssertTrue(app.buttons["confirm-stop-task"].waitForExistence(timeout: 3))
        app.buttons["Cancel"].tap()
        XCTAssertFalse(app.staticTexts["Stopped"].exists)
        row.press(forDuration: 1)
        XCTAssertTrue(stop.waitForExistence(timeout: 3))
        stop.tap()
        app.alerts.firstMatch.buttons.matching(identifier: "confirm-stop-task").firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Stopped"].waitForExistence(timeout: 10))
        row.swipeLeft()
        XCTAssertFalse(stop.exists)
        app.buttons["Done"].tap()
        XCTAssertTrue(app.buttons["chat-task-details"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.tabBars.buttons["Inbox"].tap()
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(chat.waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Task details"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Bots and Inbox navigation"; proof.lifetime = .keepAlways; add(proof)
    }
}
