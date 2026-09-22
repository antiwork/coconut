import XCTest

final class MessageInteractionFlowTests: XCTestCase {
    @MainActor
    func testSelectingMainChatDoesNotWaitForRefresh() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the selection fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        let choice = app.buttons["select-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(choice.waitForExistence(timeout: 15))
        choice.tap()
        XCTAssertTrue(app.buttons["manage-bots"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.descendants(matching: .any).matching(identifier: "message-input").firstMatch.exists)
        capture(app, "Committed selection opens Coconut while refresh fails")
    }

    @MainActor
    func testSingleChatQuotesWorkerAndKeepsDraft() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the single chat fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 15))
        XCTAssertTrue(app.tabBars.buttons["Coconut"].exists)
        XCTAssertFalse(app.tabBars.buttons["Bots"].exists)
        let checkOrder = ProcessInfo.processInfo.environment["COCONUT_ORDER_TEST"] == "1"
        if checkOrder {
            app.tabBars.buttons["Inbox"].tap()
            let option = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-option-")).firstMatch
            XCTAssertTrue(option.waitForExistence(timeout: 5))
            option.tap()
            app.tabBars.buttons["Coconut"].tap()
        }
        input.tap()
        input.typeText("Keep this draft")
        app.buttons["manage-bots"].tap()
        let worker = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(worker.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["create-bot"].exists)
        worker.tap()
        XCTAssertTrue(app.staticTexts["The form is ready for review."].waitForExistence(timeout: 3))
        XCTAssertFalse(input.exists)
        XCTAssertTrue(app.buttons["bot-settings"].exists)
        capture(app, "Read-only bot activity with reply action")
        app.buttons["reply-in-coconut-worker-answer"].tap()
        XCTAssertTrue(input.waitForExistence(timeout: 3))
        XCTAssertEqual(input.value as? String, "Keep this draft")
        XCTAssertTrue(app.buttons["Cancel reply"].exists)
        capture(app, "Coconut keeps the draft and quoted worker message")
        XCTAssertTrue(app.buttons["send-message"].isHittable)
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Keep this draft"].waitForExistence(timeout: 5))
        input.tap()
        input.typeText("An unrelated question")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Reply after message 1."].waitForExistence(timeout: 5), "A held worker request must not block the main chat.")
        if checkOrder {
            let (pendingData, _) = try await URLSession.shared.data(from: URL(string: address + "/test/send-status")!)
            let pending = try JSONSerialization.jsonObject(with: pendingData) as! [String: Any]
            XCTAssertEqual((pending["requests"] as? [[String: Any]])?.count, 1, "The worker reply must wait for its earlier decision answer.")
        }
        var release = URLRequest(url: URL(string: address + "/test/release-send")!)
        release.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: release)
        if checkOrder {
            XCTAssertTrue(app.staticTexts["Reply after message 2."].waitForExistence(timeout: 8))
        }
        let (data, _) = try await URLSession.shared.data(from: URL(string: address + "/test/send-status")!)
        let result = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let requests = result["requests"] as! [[String: Any]]
        let quoted = requests.first { ($0["body"] as? [String: Any])?["reply_to_message_id"] != nil }
        let body = quoted?["body"] as? [String: Any]
        XCTAssertEqual(body?["reply_to_message_id"] as? String, "worker-answer")
        XCTAssertEqual(quoted?["path"] as? String, "/v1/bots/00000000-0000-4000-8000-000000000002/messages")
        XCTAssertTrue(app.buttons["manage-bots"].exists)
        capture(app, "Worker quote appears in the single conversation")
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }

    @MainActor
    func testRepliesReactionsAndCopy() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the message fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.buttons["manage-bots"].waitForExistence(timeout: 15))
        let original = app.staticTexts["The blue folder has the complete report."]
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        let originalId = original.identifier
        let source = app.staticTexts[originalId]
        source.swipeRight()
        XCTAssertTrue(app.buttons["Cancel reply"].waitForExistence(timeout: 2))
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 2))
        input.typeText("Use this folder.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(app.staticTexts["Use this folder."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Cancel reply"].exists)
        let quote = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "message-quote-")).firstMatch
        XCTAssertTrue(quote.waitForExistence(timeout: 5))
        quote.tap()
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Reply links to the original message"; proof.lifetime = .keepAlways; add(proof)
        source.press(forDuration: 1)
        XCTAssertTrue(app.buttons["Copy"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Reply"].exists)
        for name in ["Thumbs up", "Thumbs down"] {
            XCTAssertTrue(app.buttons[name].exists, name)
        }
        for name in ["Laugh", "Celebrate", "Confused", "Heart", "Rocket", "Eyes"] {
            XCTAssertFalse(app.buttons[name].exists, name)
        }
        let menu = XCTAttachment(screenshot: app.screenshot())
        menu.name = "Two reactions, Reply, and Copy"; menu.lifetime = .keepAlways; add(menu)
        app.buttons["Thumbs up"].tap()
        XCTAssertTrue(app.buttons["Remove 👍 reaction"].waitForExistence(timeout: 3))
        app.buttons["Remove 👍 reaction"].tap()
        XCTAssertTrue(app.buttons["Remove 👍 reaction"].waitForNonExistence(timeout: 3))
        source.press(forDuration: 1)
        app.buttons["Copy"].tap()
        input.tap()
        input.press(forDuration: 1)
        let paste = app.menuItems["Paste"].exists ? app.menuItems["Paste"] : app.buttons["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 3))
        paste.tap()
        XCTAssertEqual(input.value as? String, "The blue folder has the complete report.")
    }
}
