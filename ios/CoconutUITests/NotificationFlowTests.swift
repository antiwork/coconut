import XCTest

final class NotificationFlowTests: XCTestCase {
    @MainActor
    func testNotificationOpensConversationFromConversation() throws {
        try checkNotification(terminate: false)
    }

    @MainActor
    func testNotificationOpensConversationAfterTermination() throws {
        try checkNotification(terminate: true)
    }

    @MainActor
    private func checkNotification(terminate: Bool) throws {
        guard ProcessInfo.processInfo.environment["COCONUT_PUSH_TEST"] == "1" else {
            throw XCTSkip("Run with a paired phone and an external test notification.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = []
        app.launch()
        let bot = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@ OR identifier BEGINSWITH %@", "bot-conversation-", "pinned-bot-")).firstMatch
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCUIDevice.shared.press(.home)
        if terminate { app.terminate() }
        print("COCONUT_PUSH_STAGE=backgrounded")

        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let environment = ProcessInfo.processInfo.environment
        let title = try XCTUnwrap(environment["COCONUT_PUSH_EXPECTED_TITLE"])
        let body = try XCTUnwrap(environment["COCONUT_PUSH_EXPECTED_BODY"])
        let notification = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", body)).firstMatch
        XCTAssertTrue(notification.waitForExistence(timeout: 45), "The notification must contain the decrypted preview while Coconut is closed.")
        XCTAssertTrue(springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch.exists,
            "The notification must show its bot name.")
        notification.tap()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["bot-settings"].label.contains(title))
        XCTAssertFalse(app.navigationBars["Task"].exists)
    }
}
