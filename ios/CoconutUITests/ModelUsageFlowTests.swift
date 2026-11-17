import XCTest

/// The model of an agent changes from a menu on its page and in its chat, and the usage of the agents has a card and a page.
final class ModelUsageFlowTests: XCTestCase {
    private let releasesId = "00000000-0000-4000-8000-000000000001"

    @MainActor
    func testTheModelChangesFromAMenuAndTheUsageShows() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Agents"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Agents"].tap()
        XCTAssertTrue(app.buttons["agents-usage"].waitForExistence(timeout: 10))
        capture("Agents with the Usage row")
        app.buttons["agent-" + releasesId].tap()
        let chat = app.buttons["agent-chat-button"]
        XCTAssertTrue(chat.waitForExistence(timeout: 10))
        capture("The page of an agent")

        // The model: one tap opens the menu, the second chooses.
        let menu = element("agent-model-menu", in: app)
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
        XCTAssertTrue(menu.label.contains("Preview Standard"), menu.label)
        menu.tap()
        let focused = item("Preview Focused", in: app)
        _ = focused.waitForExistence(timeout: 5)
        capture("The model menu of the agent")
        XCTAssertTrue(focused.exists, app.debugDescription)
        focused.tap()
        XCTAssertTrue(wait { self.element("agent-model-menu", in: app).label.contains("Preview Focused") })
        capture("The agent on the chosen model")

        // The usage card is under the routines.
        let card = app.buttons["agent-usage"]
        for _ in 0..<6 where !card.isHittable { app.swipeUp() }
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        capture("The usage card of the agent")
        card.tap()
        XCTAssertTrue(app.buttons["12 weeks"].waitForExistence(timeout: 5))
        capture("The usage of the agent, 30 days")
        app.buttons["12 weeks"].tap()
        app.buttons["Cost"].tap()
        capture("The cost of the agent by week")
        app.navigationBars.buttons.element(boundBy: 0).tap()

        // The chat has the same menu in its bar.
        for _ in 0..<6 where !chat.isHittable { app.swipeDown() }
        chat.tap()
        let chatMenu = element("chat-model-menu", in: app)
        XCTAssertTrue(chatMenu.waitForExistence(timeout: 10))
        XCTAssertTrue(chatMenu.label.contains("Preview Focused"), chatMenu.label)
        capture("The chat with the model in its bar")
        chatMenu.tap()
        let standard = item("Default · Preview Standard", in: app)
        XCTAssertTrue(standard.waitForExistence(timeout: 5))
        capture("The model menu of the chat")
        standard.tap()
        XCTAssertTrue(wait { self.element("chat-model-menu", in: app).label.contains("Preview Standard") })

        // Profile has the default model as a menu, and the usage of all agents.
        XCTAssertTrue(app.tabBars.buttons["Profile"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Profile"].tap()
        XCTAssertTrue(element("profile-model", in: app).waitForExistence(timeout: 10))
        capture("Profile")
        element("profile-model", in: app).tap()
        XCTAssertTrue(item("Effort and fallback…", in: app).waitForExistence(timeout: 5))
        capture("The default model menu")
        item("Preview Standard", in: app).tap()
        let usage = app.buttons["profile-usage"]
        for _ in 0..<4 where !usage.isHittable { app.swipeUp() }
        usage.tap()
        XCTAssertTrue(app.buttons["12 weeks"].waitForExistence(timeout: 5))
        capture("The usage of all agents")
        app.swipeUp()
        capture("The agents and the models of the usage")
    }

    /// A line of an open menu. A line with a check mark is a switch, the others are buttons.
    @MainActor
    private func item(_ label: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "label == %@ AND (elementType == %d OR elementType == %d OR elementType == %d)",
                                                             label, XCUIElement.ElementType.button.rawValue, XCUIElement.ElementType.switch.rawValue,
                                                             XCUIElement.ElementType.menuItem.rawValue)).firstMatch
    }

    @MainActor
    private func element(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    @MainActor
    private func wait(_ condition: @escaping @MainActor () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return condition()
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
