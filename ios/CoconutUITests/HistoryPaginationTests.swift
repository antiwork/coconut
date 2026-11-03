import XCTest

final class HistoryPaginationTests: XCTestCase {
    @MainActor
    func testLoadingEarlierMessagesKeepsTheConversationVisible() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_HISTORY_URL"], let code = env["COCONUT_HISTORY_CODE"],
              let bot = env["COCONUT_HISTORY_BOT"] else {
            throw XCTSkip("Run with the history pagination fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let conversation = app.buttons["bot-conversation-\(bot)"]
        XCTAssertTrue(conversation.waitForExistence(timeout: 15))
        conversation.tap()
        let transcript = app.collectionViews["conversation-transcript"]
        let earlier = app.buttons["load-earlier-messages"]
        for _ in 0..<18 {
            if earlier.exists && earlier.isHittable { break }
            transcript.swipeDown(velocity: .fast)
        }
        XCTAssertTrue(earlier.isHittable)
        earlier.tap()
        let anchor = app.staticTexts["Message 71. This is a conversation used to check history pagination."]
        XCTAssertTrue(anchor.waitForExistence(timeout: 5))
        XCTAssertTrue(anchor.isHittable)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Conversation after loading earlier messages"
        proof.lifetime = .keepAlways
        add(proof)
        for _ in 0..<18 {
            if earlier.exists && earlier.isHittable { break }
            transcript.swipeDown(velocity: .fast)
        }
        XCTAssertTrue(earlier.isHittable)
        earlier.tap()
        transcript.swipeDown(velocity: .fast)
        let first = app.staticTexts["Message 1. This is a conversation used to check history pagination."]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertTrue(first.isHittable)
        XCTAssertFalse(earlier.exists)
    }
}
