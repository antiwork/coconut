import XCTest

/// Measures the app against a local Mac. Run scripts/test_app_performance.py.
final class AppPerformanceTests: XCTestCase {
    private let environment = ProcessInfo.processInfo.environment
    private var conversationId: String {
        environment["COCONUT_PERFORMANCE_CONVERSATION"] ?? "bot-conversation-00000000-0000-4000-8000-000000000002"
    }

    @MainActor private func pairedApp() throws -> XCUIApplication {
        guard let url = environment["COCONUT_PERFORMANCE_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_app_performance.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "performance-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(app.buttons[conversationId].waitForExistence(timeout: 30))
        // Later launches use the saved pairing and the saved history, as on a phone.
        app.launchEnvironment = [:]
        return app
    }

    @MainActor private func options(_ count: Int = 5) -> XCTMeasureOptions {
        let options = XCTMeasureOptions()
        options.iterationCount = count
        return options
    }

    @MainActor private func openConversation(_ app: XCUIApplication) {
        let bot = app.buttons[conversationId]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let text = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-text-")).firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 10))
    }

    @MainActor private func closeConversation(_ app: XCUIApplication) {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons[conversationId].waitForExistence(timeout: 10))
    }

    @MainActor func testLaunchWithSavedHistory() throws {
        let app = try pairedApp()
        app.terminate()
        measure(metrics: [XCTApplicationLaunchMetric(waitUntilResponsive: true)], options: options()) {
            app.launch()
            XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
            app.terminate()
        }
    }

    @MainActor func testOpenLongConversation() throws {
        let app = try pairedApp()
        openConversation(app)
        closeConversation(app)
        measure(metrics: [XCTClockMetric(), XCTCPUMetric(application: app)], options: options()) {
            openConversation(app)
            closeConversation(app)
        }
    }

    @MainActor func testConversationAndTabSwitching() throws {
        let app = try pairedApp()
        measure(metrics: [XCTClockMetric(), XCTCPUMetric(application: app)], options: options()) {
            openConversation(app)
            closeConversation(app)
            app.tabBars.buttons["Inbox"].tap()
            XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 5))
            app.tabBars.buttons["Bots"].tap()
            XCTAssertTrue(app.buttons[conversationId].waitForExistence(timeout: 5))
        }
    }

    @MainActor func testTypingInLongConversation() throws {
        let app = try pairedApp()
        openConversation(app)
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
        let app = try pairedApp()
        openConversation(app)
        let transcript = app.scrollViews["conversation-transcript"]
        measure(metrics: [XCTCPUMetric(application: app), XCTOSSignpostMetric.scrollingAndDecelerationMetric], options: options()) {
            transcript.swipeDown()
            transcript.swipeDown()
            transcript.swipeUp()
            transcript.swipeUp()
        }
    }

    @MainActor func testLoadEarlierMessages() throws {
        let app = try pairedApp()
        openConversation(app)
        let transcript = app.scrollViews["conversation-transcript"]
        let earlier = app.buttons["load-earlier-messages"]
        let measureOptions = options(3)
        measureOptions.invocationOptions = [.manuallyStart, .manuallyStop]
        measure(metrics: [XCTClockMetric(), XCTCPUMetric(application: app)], options: measureOptions) {
            // A page of long messages can need many swipes. Only the load itself is measured.
            for _ in 0..<150 where !(earlier.exists && earlier.isHittable) { transcript.swipeDown(velocity: .fast) }
            XCTAssertTrue(earlier.isHittable, "The conversation must have older messages to load.")
            startMeasuring()
            earlier.tap()
            let loaded = expectation(for: NSPredicate { _, _ in !(earlier.exists && !earlier.isEnabled) }, evaluatedWith: nil)
            wait(for: [loaded], timeout: 15)
            stopMeasuring()
            // A failed request also ends the loading state. A loaded page moves the button far above the screen.
            XCTAssertFalse(app.descendants(matching: .any)["action-error"].exists, "The older page must load.")
            XCTAssertFalse(earlier.exists && earlier.isHittable, "The older page must appear above the conversation.")
        }
    }

    /// The Mac sends small updates while a person reads. Each one must stay cheap.
    @MainActor func testReadingWhileUpdatesArrive() throws {
        let app = try pairedApp()
        openConversation(app)
        measure(metrics: [XCTCPUMetric(application: app)], options: options(3)) {
            Thread.sleep(forTimeInterval: 20)
        }
    }
}
