import XCTest

/// Swipe left on an agent in Chats archives it. The row "Archived" lists it, and a swipe there brings it back.
final class ArchiveAgentFlowTests: XCTestCase {
    @MainActor
    func testToDosOpenTheirWorkAndUnreadKeepsOnlyUnreadChats() throws {
        let app = try launchChats()
        let working = app.buttons["chats-todo-working"]
        XCTAssertTrue(working.waitForExistence(timeout: 10))
        let blocked = app.buttons["chats-todo-blocked"]
        XCTAssertTrue(blocked.exists)
        XCTAssertTrue(app.buttons["chats-todo-coconut-work"].exists)
        XCTAssertLessThan(working.frame.minY, blocked.frame.minY)
        XCTAssertFalse(app.buttons["chats-todo-completed"].exists)
        XCTAssertFalse(app.buttons["chats-todo-archived"].exists)
        capture("To-dos above the Chats list")
        working.tap()
        // SwiftUI applies the HStack identifier to its label and button in the native accessibility tree.
        XCTAssertTrue(app.staticTexts["work-context-preview"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts["work-context-preview"].label, "Prepare the next release")
        XCTAssertTrue(app.buttons["Remove work context"].exists)
        capture("The To-do opens its agent with work attached")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["chats-filter-people"].tap()
        XCTAssertFalse(working.exists)
        app.buttons["chats-filter-bots"].tap()
        XCTAssertTrue(working.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["chats-todo-coconut-work"].exists)
        app.buttons["chats-filter-unread"].tap()
        XCTAssertTrue(app.buttons["chats-filter-unread"].isSelected)
        XCTAssertFalse(working.exists)
        let room = String(repeating: "a", count: 64)
        XCTAssertTrue(app.buttons["team-chat-\(room)"].exists)
        XCTAssertTrue(app.buttons["team-chat-\(room)-reports"].exists)
        XCTAssertFalse(app.buttons["team-chat-\(room)-notices"].exists)
        XCTAssertFalse(app.buttons["team-chat-\(room)-design"].exists)
        XCTAssertFalse(app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"].exists)
        let starred = app.buttons["pinned-bot-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(starred.exists)
        capture("Unread includes starred chats and excludes muted chats")
        starred.tap()
        XCTAssertTrue(app.navigationBars.buttons.element(boundBy: 0).waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(starred.waitForNonExistence(timeout: 5))
        capture("Reading the starred agent removes it from Unread")
    }

    @MainActor
    func testToDosAndFiltersRemainReachableAtLargeText() throws {
        let app = try launchChats(largeText: true)
        let working = app.buttons["chats-todo-working"]
        XCTAssertTrue(working.waitForExistence(timeout: 10))
        XCTAssertTrue(working.isHittable)
        capture("To-dos with large text in dark mode")
        app.buttons["chats-filter-unread"].tap()
        XCTAssertTrue(app.buttons["chats-filter-unread"].isSelected)
        capture("Unread with large text in dark mode")
    }

    @MainActor
    private func launchChats(largeText: Bool = false) throws -> XCUIApplication {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_CHATS_TEST_URL"] else {
            throw XCTSkip("Run scripts/chats_milestone.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "chats-test"]
        if largeText {
            app.launchArguments = ["-AppleInterfaceStyle", "Dark", "-UIPreferredContentSizeCategoryName",
                                   "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        return app
    }

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
