import XCTest

/// Swipe left on an agent in Chats archives it. The row "Archived" lists it, and a swipe there brings it back.
final class ArchiveAgentFlowTests: XCTestCase {
    @MainActor
    func testSwipeLeftArchivesAnAgentAndArchivedBringsItBack() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let id = "00000000-0000-4000-8000-000000000002"
        let row = app.buttons["bot-conversation-\(id)"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        capture("Chats before the swipe")
        // The cell of the list has the swipe. A swipe on the button of the row would open the chat.
        app.cells.containing(.button, identifier: "bot-conversation-\(id)").firstMatch.swipeLeft()
        let archive = app.buttons["Archive"]
        XCTAssertTrue(archive.waitForExistence(timeout: 5))
        capture("The Archive action of the swipe")
        archive.tap()
        XCTAssertTrue(row.waitForNonExistence(timeout: 5))
        let archived = app.buttons["chats-archived"]
        XCTAssertTrue(archived.waitForExistence(timeout: 5))
        capture("Chats with the row Archived")
        archived.tap()
        let archivedRow = app.buttons["archived-bot-\(id)"]
        XCTAssertTrue(archivedRow.waitForExistence(timeout: 5))
        capture("The archived agents")
        app.cells.containing(.button, identifier: "archived-bot-\(id)").firstMatch.swipeLeft()
        app.buttons["Unarchive"].tap()
        XCTAssertTrue(archivedRow.waitForNonExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["chats-archived"].exists)
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
