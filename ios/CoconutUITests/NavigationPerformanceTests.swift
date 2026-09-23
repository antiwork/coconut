import XCTest

final class NavigationPerformanceTests: XCTestCase {
    @MainActor
    func testLongConversationAndTabsOpenPromptly() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_NAVIGATION_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_navigation_performance.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "navigation-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        for attempt in 1...2 {
            let start = ContinuousClock.now
            bot.tap()
            let latest = app.staticTexts["Latest response."]
            XCTAssertTrue(latest.waitForExistence(timeout: 3) && latest.isHittable)
            let elapsed = start.duration(to: .now)
            print("COCONUT_OPEN_\(attempt)=\(elapsed)")
            XCTAssertLessThan(elapsed, .seconds(3), "Opening a conversation must not lay out its entire history.")
            app.navigationBars.buttons.element(boundBy: 0).tap()
            XCTAssertTrue(bot.waitForExistence(timeout: 3))
            let tabsStart = ContinuousClock.now
            XCTAssertFalse(app.tabBars.buttons["Work"].exists)
            app.tabBars.buttons["Inbox"].tap()
            XCTAssertTrue(app.staticTexts["Review the old report?"].waitForExistence(timeout: 3))
            print("COCONUT_TABS_\(attempt)=\(tabsStart.duration(to: .now))")
            if attempt == 1 {
                app.tabBars.buttons["Bots"].tap()
            }
        }
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        let oldCard = app.staticTexts["Review the old report?"]
        XCTAssertTrue(oldCard.waitForExistence(timeout: 3) && oldCard.isHittable,
                      "An Inbox link must still position an old card within the full conversation.")
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Old decision in long conversation"
        proof.lifetime = .keepAlways
        add(proof)
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        input.tap()
        input.typeText("Continue from this old card.")
        app.buttons["send-message"].tap()
        let reply = app.staticTexts["Reply from the old card received."]
        XCTAssertTrue(reply.waitForExistence(timeout: 5) && reply.isHittable,
                      "Sending from an old card must return to the new reply.")
    }
}
