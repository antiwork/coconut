import XCTest

/// The working line of a chat opens the live view of its turn: the steps, the images and the files.
final class LiveActivityFlowTests: XCTestCase {
    @MainActor
    func testTheWorkingLineOpensTheLiveViewOfTheTurn() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let botId = "00000000-0000-4000-8000-000000000002"
        let row = app.buttons["bot-conversation-\(botId)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        let open = app.buttons["live-activity-open"]
        XCTAssertTrue(open.waitForExistence(timeout: 10))
        capture(app, "live-1-working-line")
        open.tap()
        let files = app.buttons["live-activity-files"]
        XCTAssertTrue(files.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Run"].firstMatch.waitForExistence(timeout: 5))
        capture(app, "live-2-sheet")
        // The sheet grows to the whole screen.
        app.swipeUp()
        capture(app, "live-3-sheet-large")
        files.tap()
        let file = app.buttons["live-activity-file"].firstMatch
        XCTAssertTrue(file.waitForExistence(timeout: 5))
        capture(app, "live-4-files")
        file.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'class Processor'")).firstMatch
            .waitForExistence(timeout: 5))
        capture(app, "live-5-file")
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
