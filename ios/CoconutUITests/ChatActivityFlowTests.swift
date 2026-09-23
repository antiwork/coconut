import XCTest

final class ChatActivityFlowTests: XCTestCase {
    @MainActor
    func testThinkingFollowsRunnableAndStreamingTransitions() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_CHAT_ACTIVITY_TEST_URL"] else {
            throw XCTSkip("Run with scripts/test_chat_activity.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "activity-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let botId = "00000000-0000-4000-8000-000000000002"
        let row = app.buttons["bot-conversation-\(botId)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        let thinking = app.otherElements["bot-working-\(botId)"]
        let dot = app.otherElements["bot-active-\(botId)"]
        XCTAssertFalse(thinking.exists, "Queued work must not look like active thinking.")
        XCTAssertFalse(dot.exists)
        capture(app, "Queued task is resting")

        stage("working")
        assertAppears(thinking, within: 3, stage: "working")
        XCTAssertTrue(dot.waitForExistence(timeout: 3))
        capture(app, "Working task shows thinking and activity")

        stage("waiting")
        assertDisappears(thinking, within: 3, stage: "waiting")
        XCTAssertTrue(dot.waitForNonExistence(timeout: 3))
        capture(app, "Waiting task returns to rest")

        stage("streaming")
        assertAppears(thinking, within: 3, stage: "streaming")
        XCTAssertFalse(dot.exists, "Streaming feedback does not invent backend worker liveness.")
        capture(app, "Streaming reply retains response feedback")

        for terminal in ["completed", "failed"] {
            stage(terminal)
            assertDisappears(thinking, within: 3, stage: terminal)
            XCTAssertFalse(dot.exists)
            capture(app, "\(terminal.capitalized) task is resting")
        }
    }

    private func stage(_ value: String) { print("COCONUT_CHAT_ACTIVITY_STAGE=\(value)") }

    @MainActor
    private func assertAppears(_ element: XCUIElement, within seconds: TimeInterval, stage: String) {
        let start = Date()
        XCTAssertTrue(element.waitForExistence(timeout: seconds), "Thinking did not appear for \(stage).")
        XCTAssertLessThan(Date().timeIntervalSince(start), seconds, "Thinking transition exceeded \(seconds)s for \(stage).")
    }

    @MainActor
    private func assertDisappears(_ element: XCUIElement, within seconds: TimeInterval, stage: String) {
        let start = Date()
        XCTAssertTrue(element.waitForNonExistence(timeout: seconds), "Thinking did not stop for \(stage).")
        XCTAssertLessThan(Date().timeIntervalSince(start), seconds, "Resting transition exceeded \(seconds)s for \(stage).")
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
