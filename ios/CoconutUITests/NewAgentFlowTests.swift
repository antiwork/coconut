import XCTest

final class NewAgentFlowTests: XCTestCase {
    /// The plus makes an agent with a random letter and opens its chat. Its name in the header opens the settings.
    @MainActor
    func testThePlusOpensTheChatOfANewLetterAgent() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Chats"].tap()
        let plus = app.buttons["create-bot"]
        XCTAssertTrue(plus.waitForExistence(timeout: 5))
        plus.tap()

        let header = app.buttons["bot-settings"]
        XCTAssertTrue(header.waitForExistence(timeout: 5))
        let name = header.label.replacingOccurrences(of: " settings", with: "")
        XCTAssertEqual(name.count, 1)
        XCTAssertTrue(name.first!.isUppercase)
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let chat = XCTAttachment(screenshot: app.screenshot())
        chat.name = "New letter agent"
        chat.lifetime = .keepAlways
        add(chat)

        header.tap()
        let field = app.textFields.matching(NSPredicate(format: "value == %@", name)).firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        let settings = XCTAttachment(screenshot: app.screenshot())
        settings.name = "Settings with the name to change"
        settings.lifetime = .keepAlways
        add(settings)
    }
}
