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
    func testMentionPickerRoutesReplyAndKeepsConversation() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
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
        XCTAssertFalse(app.buttons["message-mode"].exists, "A mention must show explicit recipients instead of a steering mode.")
        input.typeText("Check the plan.")
        capture("Selected mention in composer")
        app.buttons["send-message"].tap()
        let sender = app.buttons["message-sender-\(scoutId)"].firstMatch
        XCTAssertTrue(sender.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Forge"), "The response must remain in the original conversation.")
        XCTAssertFalse(app.buttons["remove-mention-\(scoutId)"].exists)
        capture("Attributed reply in original conversation")

        sender.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["bot-settings"].label.contains("Scout"))
        XCTAssertTrue(app.staticTexts["In Forge"].waitForExistence(timeout: 5), "The recipient must link to work in another conversation.")
        capture("Shared task in recipient conversation")
    }

    @MainActor
    func testInboxConversationMentionPreservesPendingDecision() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let inbox = app.buttons["home-inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        inbox.tap()
        app.staticTexts["inbox-subject-00000000-0000-4000-8000-000000000021"].tap()
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
        XCTAssertTrue(app.buttons["message-sender-\(scoutId)"].firstMatch.waitForExistence(timeout: 5))
        for _ in 0..<7 where !option.isHittable { app.swipeDown() }
        XCTAssertTrue(option.exists && option.isEnabled, "Mentioning another bot must preserve the exact pending choices.")
        capture("Mention reply preserves pending decision")
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
