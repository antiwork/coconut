import XCTest

final class BotMentionsFlowTests: XCTestCase {
    private let forgeId = "00000000-0000-4000-8000-000000000001"
    private let scoutId = "00000000-0000-4000-8000-000000000002"

    @MainActor
    func testMentionPickerSupportsAccessibilityTextAndRemoval() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let forge = app.buttons["bot-conversation-\(forgeId)"]
        XCTAssertTrue(forge.waitForExistence(timeout: 10))
        forge.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("@sc")
        let option = app.buttons["mention-bot-\(scoutId)"]
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        XCTAssertTrue(option.isHittable)
        XCTAssertGreaterThanOrEqual(option.frame.height, 44)
        capture("Bot picker with accessibility text")
        option.tap()
        let remove = app.buttons["remove-mention-\(scoutId)"]
        XCTAssertTrue(remove.waitForExistence(timeout: 5))
        XCTAssertGreaterThanOrEqual(remove.frame.height, 44)
        remove.tap()
        XCTAssertFalse(remove.exists)
        XCTAssertFalse((input.value as? String ?? "").contains("@Scout"))
    }

    @MainActor
    func testMentionPickerIdentifiesBotAndKeepsConversation() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let forge = app.buttons["bot-conversation-\(forgeId)"]
        XCTAssertTrue(forge.waitForExistence(timeout: 10))
        forge.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("@sc")
        let scoutOption = app.buttons["mention-bot-\(scoutId)"]
        XCTAssertTrue(scoutOption.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["mention-bot-\(forgeId)"].exists)
        capture("Filtered bot mention picker")
        scoutOption.tap()
        XCTAssertTrue(app.buttons["remove-mention-\(scoutId)"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["message-mode"].exists, "The composer keeps automatic routing.")
        input.typeText("Check the plan.")
        capture("Selected mention in composer")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["@Scout Check the plan."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["message-sender-\(scoutId)"].firstMatch.exists)
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Forge"))
        XCTAssertFalse(app.staticTexts["To Scout"].exists)
        capture("Mention stays in the current conversation")
    }

    @MainActor
    func testInboxConversationMentionPreservesPendingDecision() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let inbox = app.tabBars.buttons["Home"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        app.openCards()
        let openChat = app.buttons["inbox-context-00000000-0000-4000-8000-000000000031"]
        XCTAssertTrue(openChat.waitForExistence(timeout: 5))
        openChat.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        let option = app.buttons["decision-option-00000000-0000-4000-8000-000000000061"]
        for _ in 0..<5 where !option.isHittable { app.swipeUp() }
        XCTAssertTrue(option.isHittable)
        input.tap()
        input.typeText("@Sc")
        let scout = app.buttons["mention-bot-\(scoutId)"]
        XCTAssertTrue(scout.waitForExistence(timeout: 5))
        scout.tap()
        input.typeText("Please check this decision.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["@Scout Please check this decision."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["message-sender-\(scoutId)"].firstMatch.exists)
        for _ in 0..<7 where !option.isHittable { app.swipeDown() }
        XCTAssertTrue(option.exists && option.isEnabled, "Mentioning another bot must preserve the exact pending choices.")
        capture("Mention reply preserves pending decision")
    }

    @MainActor
    func testMentionKeepsOwnerOnWire() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py with --bot-references.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let scout = app.buttons["bot-conversation-\(scoutId)"]
        XCTAssertTrue(scout.waitForExistence(timeout: 15)); scout.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5)); input.tap()
        input.typeText("Tell me about @Fo")
        let mention = app.buttons["mention-bot-\(forgeId)"]
        XCTAssertTrue(mention.waitForExistence(timeout: 5)); mention.tap()
        XCTAssertTrue(app.staticTexts["Mentioned"].exists)
        XCTAssertFalse(app.staticTexts["New task"].exists)
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Reply after message 1."].waitForExistence(timeout: 10))
        let (data, _) = try await URLSession.shared.data(from: URL(string: address + "/test/send-status")!)
        let status = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let requests = try XCTUnwrap(status["requests"] as? [[String: Any]])
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0]["path"] as? String, "/v1/bots/\(scoutId)/messages")
        let body = try XCTUnwrap(requests[0]["body"] as? [String: Any])
        XCTAssertEqual(body["mentioned_bot_ids"] as? [String], [forgeId])
        XCTAssertEqual(body["recipient_bot_ids"] as? [String], [])
        XCTAssertEqual(body["mode"] as? String, "auto")
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Scout"))
        XCTAssertFalse(app.staticTexts["To Forge"].exists)
        capture("Mention keeps the original bot as recipient")
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
