import XCTest

final class InstantSendFlowTests: XCTestCase {
    @MainActor
    func testSendAppearsBeforeServerAcceptsAndRetryDoesNotDuplicate() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py with --instant-send.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        let botId = "00000000-0000-4000-8000-000000000002"
        let bot = app.buttons["bot-conversation-\(botId)"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        capture(app, "Separated home controls")
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Immediate message.")
        app.buttons["send-message"].tap()
        // The fixture cannot accept this request until this test releases it.
        XCTAssertTrue(app.staticTexts["Immediate message."].waitForExistence(timeout: 1))
        XCTAssertNotEqual(input.value as? String, "Immediate message.")
        XCTAssertTrue(app.otherElements["bot-working-\(botId)"].waitForExistence(timeout: 1))
        capture(app, "Message and thinking before server acceptance")
        input.typeText("Retry this message.")
        _ = try await request(address + "/test/release-send", method: "POST")
        XCTAssertTrue(app.staticTexts["Reply after message 1."].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Immediate message.")).count, 1)
        XCTAssertEqual(input.value as? String, "Retry this message.")
        app.buttons["send-message"].tap()
        let retry = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "retry-message-")).firstMatch
        XCTAssertTrue(retry.waitForExistence(timeout: 10))
        capture(app, "Failed send stays in the conversation")
        app.terminate()
        app.launchEnvironment = [:]
        app.launch()
        XCTAssertTrue(bot.waitForExistence(timeout: 10))
        bot.tap()
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        retry.tap()
        XCTAssertTrue(app.staticTexts["Reply after message 2."].waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Retry this message.")).count, 1)
        let data = try await request(address + "/test/send-status")
        let status = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let keys = status["keys"] as! [String]
        XCTAssertEqual(keys.count, 3)
        XCTAssertEqual(keys[1], keys[2])
        XCTAssertNotEqual(keys[0], keys[1])
        XCTAssertEqual(status["sends"] as? Int, 2)
        input.tap()
        input.typeText("Rejected message.")
        app.buttons["send-message"].tap()
        let edit = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "edit-message-")).firstMatch
        XCTAssertTrue(edit.waitForExistence(timeout: 10))
        edit.tap()
        XCTAssertTrue(app.staticTexts["The selected task has finished."].waitForExistence(timeout: 5))
        let editor = app.textViews["edit-outgoing-input"]
        editor.tap()
        editor.typeText(" Corrected.")
        app.buttons["resend-edited-message"].tap()
        XCTAssertTrue(app.staticTexts["Reply after message 3."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Rejected message."].exists)
        let final = try JSONSerialization.jsonObject(with: await request(address + "/test/send-status")) as! [String: Any]
        let finalKeys = final["keys"] as! [String]
        XCTAssertEqual(finalKeys.count, 5)
        XCTAssertNotEqual(finalKeys[3], finalKeys[4])
        XCTAssertEqual(final["sends"] as? Int, 3)
    }

    @MainActor
    private func request(_ address: String, method: String = "GET") async throws -> Data {
        var request = URLRequest(url: URL(string: address)!)
        request.httpMethod = method
        return try await URLSession.shared.data(for: request).0
    }

    @MainActor
    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
