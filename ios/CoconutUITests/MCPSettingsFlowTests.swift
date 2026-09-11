import XCTest

final class MCPSettingsFlowTests: XCTestCase {
    @MainActor
    func testConnectionFormUsesSecureTokenInput() {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.buttons["app-settings"].waitForExistence(timeout: 10))
        app.buttons["app-settings"].tap()
        let connections = app.buttons["mcp-settings"]
        for _ in 0..<3 where !connections.isHittable { app.swipeUp() }
        XCTAssertTrue(connections.waitForExistence(timeout: 5))
        connections.tap()
        app.buttons["add-mcp"].tap()
        XCTAssertTrue(app.textFields["mcp-name"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.secureTextFields["mcp-token"].exists)
        XCTAssertFalse(app.buttons["save-mcp"].isEnabled)
        app.textFields["mcp-name"].tap()
        app.textFields["mcp-name"].typeText("gumclaw")
        app.textFields["mcp-url"].tap()
        app.textFields["mcp-url"].typeText("https://example.com/mcp")
        XCTAssertTrue(app.buttons["save-mcp"].isEnabled)
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "MCP connection form"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
