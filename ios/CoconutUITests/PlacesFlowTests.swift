import XCTest

/// The five places of the app on an iPhone: Home with "Needs attention", Chats, Agents with the page of an agent,
/// People and Profile. Each step keeps a screenshot.
final class PlacesFlowTests: XCTestCase {
    @MainActor
    func testTheFivePlacesAndTheAttentionCard() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "--attention-preview"]
        app.launch()

        let tabs = ["Home", "Chats", "Agents", "People", "Profile"].map { app.tabBars.buttons[$0] }
        XCTAssertTrue(tabs[0].waitForExistence(timeout: 15))
        XCTAssertTrue(tabs[0].isSelected, "Home must be the launch screen.")
        for (left, right) in zip(tabs, tabs.dropFirst()) {
            XCTAssertTrue(right.exists, "The tab \(right.label) is missing.")
            XCTAssertLessThan(left.frame.minX, right.frame.minX, "The tabs keep the order Home, Chats, Agents, People, Profile.")
        }
        XCTAssertEqual(app.tabBars.buttons.count, 5)

        // Home: the red card, the unread chats with the cards, and the work in progress.
        XCTAssertTrue(app.buttons["attention-waiting"].waitForExistence(timeout: 5), "A card that waits past 4 hours must breach.")
        XCTAssertTrue(app.buttons["attention-agents"].exists, "An agent with a provider limit must breach.")
        XCTAssertTrue(app.buttons["attention-work"].exists, "A blocked To-do must breach.")
        XCTAssertTrue(app.buttons["home-review-cards"].exists)
        capture(app, "1 Home")

        // A row of the red card leads to the place to fix it: the page of the agent.
        app.buttons["attention-agents"].tap()
        XCTAssertTrue(app.buttons["agent-open-chat"].waitForExistence(timeout: 5))
        capture(app, "2 Agent from Needs attention")
        app.navigationBars.buttons.firstMatch.tap()

        // The cards open from Home, in the views of the Inbox.
        XCTAssertTrue(app.buttons["home-review-cards"].waitForExistence(timeout: 5))
        app.buttons["home-review-cards"].tap()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch
            .waitForExistence(timeout: 5))
        capture(app, "3 Cards from Home")

        tabs[1].tap()
        XCTAssertTrue(app.buttons["create-bot"].waitForExistence(timeout: 5))
        capture(app, "4 Chats")

        // Agents: each agent is a project. Its page has the chat, the To-dos, the pages, the routines and the settings.
        tabs[2].tap()
        let agent = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "agent-0")).firstMatch
        XCTAssertTrue(agent.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["agents-all-pages"].exists, "Every page stays reachable from Agents.")
        capture(app, "5 Agents")
        agent.tap()
        XCTAssertTrue(app.buttons["agent-open-chat"].waitForExistence(timeout: 5))
        capture(app, "6 Agent page")
        let settings = app.buttons["agent-settings"]
        app.swipeUp()
        for _ in 0..<3 where !settings.isHittable { app.swipeUp() }
        XCTAssertTrue(settings.isHittable, "The page of an agent must reach its settings.")
        XCTAssertTrue(app.buttons["agent-add-routine"].exists)
        XCTAssertTrue(app.buttons["agent-archive"].exists)
        capture(app, "7 Agent page, lower part")
        // A pushed page hides the tab bar, so Back comes first.
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(tabs[3].waitForExistence(timeout: 5))

        tabs[3].tap()
        XCTAssertTrue(app.buttons["team-create"].waitForExistence(timeout: 5), "People without a team offers to make one.")
        capture(app, "8 People")

        // Profile has the things of this device and the defaults. Coconut has no Settings screen.
        tabs[4].tap()
        XCTAssertTrue(app.buttons["mac-settings"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["notification-settings"].exists)
        XCTAssertTrue(app.buttons["mcp-settings"].exists)
        capture(app, "9 Profile")
        let parallel = app.buttons["parallel-work-settings"]
        for _ in 0..<3 where !parallel.isHittable { app.swipeUp() }
        parallel.tap()
        XCTAssertTrue(app.navigationBars.buttons.firstMatch.waitForExistence(timeout: 5))
        capture(app, "10 Parallel work from Profile")
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
