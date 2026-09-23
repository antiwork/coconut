import XCTest

final class SendFromHistoryTests: XCTestCase {
    @MainActor
    func testSendingFromHistoryShowsMessageBeforeServerAcceptance() async throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the conversation fixture with --send-from-history.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.buttons["manage-bots"].waitForExistence(timeout: 15))
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        let transcript = app.scrollViews["conversation-transcript"]
        for attempt in 0..<3 {
            for _ in 0..<(4 + attempt * 2) { transcript.swipeDown(velocity: .fast) }
            input.tap()
            let text = "Immediate message \(attempt)."
            input.typeText(text)
            app.buttons["send-message"].tap()
            let message = app.staticTexts[text]
            let visible = message.exists && message.isHittable
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = "Sent message before server acceptance \(attempt)"
            attachment.lifetime = .keepAlways
            add(attachment)
            XCTAssertTrue(visible, "The first snapshot after sending must show the new message.")
            XCTAssertLessThanOrEqual(message.frame.maxY, input.frame.minY)
            // Deferred scroll layout must not replace the visible transcript with empty space.
            try await Task.sleep(for: .seconds(1))
            let settled = message.exists && message.isHittable
            let settledImage = XCTAttachment(screenshot: app.screenshot())
            settledImage.name = "Transcript after deferred layout \(attempt)"
            settledImage.lifetime = .keepAlways
            add(settledImage)
            XCTAssertTrue(settled, "The transcript must stay visible while the server request waits.")
        }
    }
}
