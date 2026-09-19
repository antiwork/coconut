import XCTest

final class AppPerformanceTests: XCTestCase {
    @MainActor private func app() throws -> XCUIApplication {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_PERFORMANCE_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_app_performance.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "performance-test"]
        return app
    }

    @MainActor private func options() -> XCTMeasureOptions {
        let options = XCTMeasureOptions()
        options.iterationCount = 3
        return options
    }

    @MainActor private func openChat(_ app: XCUIApplication) {
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        XCTAssertTrue(app.staticTexts["Latest response."].waitForExistence(timeout: 5))
    }

    @MainActor func testLaunchWithSavedHistory() throws {
        let app = try app()
        app.launch()
        openChat(app)
        app.terminate()
        measure(metrics: [XCTApplicationLaunchMetric(waitUntilResponsive: true)], options: options()) {
            app.launch()
            XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
            app.terminate()
        }
    }

    @MainActor func testConversationAndTabSwitching() throws {
        let app = try app()
        app.launch()
        measure(metrics: [XCTClockMetric(), XCTCPUMetric(application: app)], options: options()) {
            openChat(app)
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertFalse(app.tabBars.buttons["Work"].exists)
            app.tabBars.buttons["Inbox"].tap()
            XCTAssertTrue(app.staticTexts["Review the old report?"].waitForExistence(timeout: 5))
            app.tabBars.buttons["Bots"].tap()
        }
    }

    @MainActor func testTypingInLongConversation() throws {
        let app = try app()
        app.launch()
        openChat(app)
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        input.tap()
        let text = "Please check the current report. "
        let measureOptions = options()
        measureOptions.invocationOptions = .manuallyStop
        measure(metrics: [XCTCPUMetric(application: app)], options: measureOptions) {
            input.typeText(text)
            stopMeasuring()
            input.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: text.count))
        }
    }

    @MainActor func testLongConversationScrolling() throws {
        let app = try app()
        app.launch()
        openChat(app)
        let transcript = app.scrollViews["conversation-transcript"]
        measure(metrics: [XCTCPUMetric(application: app), XCTOSSignpostMetric.scrollingAndDecelerationMetric], options: options()) {
            transcript.swipeDown()
            transcript.swipeDown()
            transcript.swipeUp()
            transcript.swipeUp()
        }
    }
}
