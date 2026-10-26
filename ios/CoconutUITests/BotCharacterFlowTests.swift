import XCTest

final class BotCharacterFlowTests: XCTestCase {
    @MainActor
    func testCustomizeCharacterAndKeepItWhenReopeningSettings() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let forge = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(forge.waitForExistence(timeout: 10))
        forge.tap()
        app.buttons["bot-settings"].tap()
        app.buttons["Change character"].tap()
        let fruit = app.buttons["bot-avatar-fruit.apple"]
        XCTAssertTrue(fruit.waitForExistence(timeout: 5))
        fruit.tap()
        app.buttons["Done"].tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["bot-settings"].tap()
        app.buttons["Change character"].tap()
        XCTAssertTrue(fruit.waitForExistence(timeout: 5))
        XCTAssertTrue(fruit.isSelected)
        capture("Saved coconut character")
    }

    @MainActor
    func testWorkingCharacterAppearsInRowsPinsAndConversation() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let id = "00000000-0000-4000-8000-000000000002"
        let row = app.buttons["bot-conversation-\(id)"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        capture("Working coconut in the bot list")
        row.press(forDuration: 1)
        app.buttons["Pin bot"].tap()
        let pin = app.buttons["pinned-bot-\(id)"]
        XCTAssertTrue(pin.waitForExistence(timeout: 5))
        capture("Working coconut pinned")
        pin.tap()
        let working = app.otherElements["bot-working-\(id)"]
        XCTAssertTrue(working.waitForExistence(timeout: 5))
        XCTAssertEqual(working.progressIndicators.count, 0)
        let composer = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(composer.exists)
        XCTAssertLessThanOrEqual(working.frame.maxY, composer.frame.minY)
        capture("Coconut replaces the conversation spinner")
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
