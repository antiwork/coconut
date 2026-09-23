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
        app.buttons["Bot options"].tap()
        app.buttons["chat-task-details"].tap()
        XCTAssertTrue(app.navigationBars["Task details"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Export account data"].exists)
        XCTAssertFalse(app.staticTexts["Obsolete progress label"].exists)
        let stop = app.buttons["task-stop-" + itemId]
        XCTAssertTrue(stop.waitForExistence(timeout: 5))
        stop.tap()
        XCTAssertTrue(app.buttons["confirm-stop-task"].waitForExistence(timeout: 3))
        app.buttons["Cancel"].tap()
        XCTAssertTrue(stop.exists)
        XCTAssertFalse(app.staticTexts["Stopped"].exists)
        stop.tap()
        app.alerts["Stop this task?"].buttons.matching(identifier: "confirm-stop-task").firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Stopped"].waitForExistence(timeout: 10))
        XCTAssertFalse(stop.exists)
        app.buttons["Done"].tap()
        XCTAssertTrue(app.buttons["Bot options"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.tabBars.buttons["Inbox"].tap()
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(chat.waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Task details"].exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Bots and Inbox navigation"; proof.lifetime = .keepAlways; add(proof)
    }
}
