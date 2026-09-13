import XCTest

final class DecisionInboxTests: XCTestCase {
    @MainActor
    func testChoicesAdvanceBeforeConfirmationAndRecoverFailure() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else { throw XCTSkip("Run the choice fixture.") }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        app.buttons["decision-option-option-0-0"].tap()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 1))
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
        app.buttons["decision-option-option-1-0"].tap()
        XCTAssertTrue(app.staticTexts["Review the onboarding copy?"].waitForExistence(timeout: 1))
        app.buttons["decision-option-option-2-0"].tap()
        let empty = app.staticTexts["All caught up"]
        XCTAssertTrue(empty.waitForExistence(timeout: 1))
        let before = empty.frame
        releaseFixture(address)
        XCTAssertTrue(app.buttons["Dismiss error"].waitForExistence(timeout: 5))
        app.buttons["Dismiss error"].tap()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 5))
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(empty.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["inbox-review-later"].exists)
        XCTAssertEqual(empty.frame.minY, before.minY, accuracy: 1)
        XCTAssertEqual(empty.frame.height, before.height, accuracy: 1)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Stable caught-up position with pending items"; proof.lifetime = .keepAlways; add(proof)
    }

    @MainActor
    func testSwipeStackKeepsDecisionsAndSupportsUndo() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the Inbox stack fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch.waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        let first = app.staticTexts["Publish the release notes?"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        for index in 0..<4 {
            let option = app.buttons["decision-option-option-0-\(index)"]
            XCTAssertTrue(option.exists)
        }
        XCTAssertTrue(app.staticTexts["Publish these notes on the public release page."].exists)
        XCTAssertFalse(app.staticTexts["The draft matches the shipped changes."].exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-menu-")).firstMatch.exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Preview ")).firstMatch.exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Four choices with effects"; proof.lifetime = .keepAlways; add(proof)
        let card = app.scrollViews["inbox-card-scroll"]
        let start = card.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45))
        start.press(forDuration: 0.1, thenDragTo: card.coordinate(withNormalizedOffset: CGVector(dx: 0.62, dy: 0.45)), withVelocity: .slow, thenHoldForDuration: 0.2)
        XCTAssertTrue(first.isHittable)
        app.scrollViews["inbox-card-scroll"].swipeUp()
        XCTAssertTrue(first.exists)
        app.scrollViews["inbox-card-scroll"].swipeDown()
        XCTAssertFalse(app.buttons["inbox-later"].exists)
        XCTAssertFalse(app.buttons["decision-details"].exists)
        card.swipeLeft()
        let second = app.staticTexts["Keep the retry fix?"]
        XCTAssertTrue(second.waitForExistence(timeout: 5))
        XCTAssertFalse(first.isHittable)
        releaseFixture(address)
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.buttons["undo-inbox-dismiss"].tap()
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        card.swipeRight()
        XCTAssertTrue(app.buttons["Dismiss error"].waitForExistence(timeout: 5))
        XCTAssertTrue(first.isHittable)
        app.buttons["Dismiss error"].tap()
        card.swipeRight()
        XCTAssertTrue(second.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.buttons["undo-inbox-dismiss"].tap()
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        card.swipeLeft()
        XCTAssertTrue(second.waitForExistence(timeout: 5))
        card.swipeLeft()
        let proposal = app.staticTexts["Review the onboarding copy?"]
        XCTAssertTrue(proposal.waitForExistence(timeout: 5))
        card.swipeRight()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 5))
        XCTAssertFalse(first.isHittable)
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.buttons["undo-inbox-dismiss"].tap()
        XCTAssertTrue(proposal.waitForExistence(timeout: 5))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Inbox card stack"; shot.lifetime = .keepAlways; add(shot)
        let done = expectation(description: "No swipe approves a decision")
        URLSession.shared.dataTask(with: URL(string: address + "/test/inbox-status")!) { data, _, _ in
            let result = try? JSONSerialization.jsonObject(with: data ?? Data()) as? [String: Any]
            XCTAssertEqual((result?["answers"] as? [Any])?.count, 0)
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: 5)
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        let original = app.staticTexts["Review the onboarding copy?"]
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        XCTAssertTrue(original.isHittable)
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
    }

    @MainActor
    func testOpenChatShowsSelectedCard() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the Inbox link fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        app.buttons["3 decisions left. Show all"].tap()
        app.buttons.containing(.staticText, identifier: "Review the onboarding copy?").firstMatch.tap()
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        let target = app.staticTexts["Review the onboarding copy?"]
        XCTAssertTrue(target.waitForExistence(timeout: 5))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Selected card in chat"; proof.lifetime = .keepAlways; add(proof)
        XCTAssertTrue(target.isHittable)
        let composer = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(composer.exists)
        XCTAssertTrue(composer.isHittable)
    }

    @MainActor
    func testUndoDuringPendingSwipeKeepsCardAndHeaderStable() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the delayed Undo fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        let card = app.scrollViews["inbox-card-scroll"]
        let originalFrame = card.frame
        let original = app.staticTexts["Publish the release notes?"]
        XCTAssertTrue(original.isHittable)
        let count = app.buttons["3 decisions left. Show all"]
        let countFrame = count.frame
        card.swipeLeft()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 3))
        let undo = app.buttons["undo-inbox-dismiss"]
        XCTAssertTrue(undo.waitForExistence(timeout: 1), "Undo must appear before the server confirms the swipe.")
        XCTAssertEqual(card.frame.minY, originalFrame.minY, accuracy: 1)
        XCTAssertEqual(card.frame.height, originalFrame.height, accuracy: 1)
        XCTAssertEqual(app.buttons["2 decisions left. Show all"].frame.midX, countFrame.midX, accuracy: 1)
        undo.tap()
        XCTAssertTrue(original.waitForExistence(timeout: 1), "Undo must restore the card before the network returns.")
        XCTAssertEqual(card.frame.minY, originalFrame.minY, accuracy: 1)
        releaseFixture(address)
        let option = app.buttons["decision-option-option-0-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 5), .completed)
        XCTAssertFalse(undo.exists)
        XCTAssertEqual(card.frame.height, originalFrame.height, accuracy: 1)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Undo restores the stack without changing layout"; proof.lifetime = .keepAlways; add(proof)
    }

    @MainActor
    func testUndoTargetsLatestSwipeWhenEarlierWriteFinishesLast() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the delayed swipe order fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        let card = app.scrollViews["inbox-card-scroll"]
        card.swipeLeft()
        let second = app.staticTexts["Keep the retry fix?"]
        XCTAssertTrue(second.waitForExistence(timeout: 3))
        card.swipeLeft()
        XCTAssertTrue(app.staticTexts["Review the onboarding copy?"].waitForExistence(timeout: 3))
        app.buttons["undo-inbox-dismiss"].tap()
        XCTAssertTrue(second.waitForExistence(timeout: 1))
        releaseFixture(address)
        let settled = expectation(description: "Both writes and the restore finish")
        URLSession.shared.dataTask(with: URL(string: address + "/test/inbox-status")!) { data, _, _ in
            let result = try? JSONSerialization.jsonObject(with: data ?? Data()) as? [String: Any]
            XCTAssertEqual((result?["answers"] as? [Any])?.count, 0)
            settled.fulfill()
        }.resume()
        wait(for: [settled], timeout: 5)
        let option = app.buttons["decision-option-option-1-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 5), .completed)
        XCTAssertTrue(second.isHittable)
        XCTAssertFalse(app.staticTexts["Publish the release notes?"].isHittable)
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
    }

    @MainActor
    func testUndoSendsRestoreWhileSnapshotRefreshIsBlocked() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the blocked snapshot fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertFalse(app.buttons["decision-option-option-0-0"].label.contains("Recommended"))
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["decision-option-option-1-0"].label.contains("Recommended"))
        XCTAssertFalse(app.buttons["decision-option-option-1-1"].label.contains("Recommended"))
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Recommendation follows the bot choice"; proof.lifetime = .keepAlways; add(proof)
        app.buttons["undo-inbox-dismiss"].tap()
        let restored = expectation(description: "Restore arrives before snapshot refresh returns")
        URLSession.shared.dataTask(with: URL(string: address + "/test/inbox-status")!) { data, _, _ in
            let result = try? JSONSerialization.jsonObject(with: data ?? Data()) as? [String: Any]
            XCTAssertEqual(result?["actions"] as? [String], ["later", "restore"])
            restored.fulfill()
        }.resume()
        wait(for: [restored], timeout: 3)
        let option = app.buttons["decision-option-option-0-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 3), .completed)
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
    }

    @MainActor
    func testUndoRecoversAnUncertainSwipeWithTheSameKey() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the uncertain swipe fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        app.scrollViews["inbox-card-scroll"].swipeRight()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 3))
        app.buttons["undo-inbox-dismiss"].tap()
        releaseFixture(address)
        let option = app.buttons["decision-option-option-0-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 5), .completed)
        let restored = expectation(description: "Undo replays one swipe and restores its receipt")
        URLSession.shared.dataTask(with: URL(string: address + "/test/inbox-status")!) { data, _, _ in
            let result = try? JSONSerialization.jsonObject(with: data ?? Data()) as? [String: Any]
            XCTAssertEqual(result?["actions"] as? [String], ["dismiss", "dismiss", "restore"])
            let keys = result?["keys"] as? [String] ?? []
            XCTAssertEqual(keys.count, 3)
            if keys.count == 3 {
                XCTAssertEqual(keys[0], keys[1])
                XCTAssertNotEqual(keys[1], keys[2])
            }
            XCTAssertEqual((result?["answers"] as? [Any])?.count, 0)
            restored.fulfill()
        }.resume()
        wait(for: [restored], timeout: 3)
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
        XCTAssertTrue(app.staticTexts["Publish the release notes?"].isHittable)
    }

    @MainActor
    func testOldSnapshotCannotRemoveConfirmedUndo() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the stale snapshot fixture.")
        }
        continueAfterFailure = false
        func call(_ path: String, method: String = "POST") {
            var request = URLRequest(url: URL(string: address + path)!)
            request.httpMethod = method
            let done = expectation(description: path)
            URLSession.shared.dataTask(with: request) { data, _, _ in
                if path == "/test/stale-ready" {
                    let result = try? JSONSerialization.jsonObject(with: data ?? Data()) as? [String: Any]
                    XCTAssertEqual(result?["ready"] as? Bool, true)
                }
                done.fulfill()
            }.resume()
            wait(for: [done], timeout: 5)
        }
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        call("/test/start-stale")
        call("/test/stale-ready", method: "GET")
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 3))
        call("/test/release-stale")
        let applied = expectation(description: "Deliver the pre-swipe snapshot to the app")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { applied.fulfill() }
        wait(for: [applied], timeout: 2)
        let undo = app.buttons["undo-inbox-dismiss"]
        XCTAssertTrue(undo.exists, "A pre-swipe snapshot must not clear confirmed Undo.")
        undo.tap()
        let option = app.buttons["decision-option-option-0-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 5), .completed)
        XCTAssertFalse(undo.exists)
    }

    @MainActor
    func testReopeningFromPendingListEndsUndoBeforeDelayedReceipt() throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the pending-list revisit fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Inbox"].tap()
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.staticTexts["Keep the retry fix?"].waitForExistence(timeout: 3))
        let undo = app.buttons["undo-inbox-dismiss"]
        XCTAssertTrue(undo.exists)
        app.buttons["2 decisions left. Show all"].tap()
        app.buttons.containing(.staticText, identifier: "Publish the release notes?").firstMatch.tap()
        let original = app.staticTexts["Publish the release notes?"]
        XCTAssertTrue(original.waitForExistence(timeout: 3))
        XCTAssertFalse(undo.exists)
        releaseFixture(address)
        let option = app.buttons["decision-option-option-0-0"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: option)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 5), .completed)
        XCTAssertFalse(undo.exists, "The earlier swipe receipt must not recreate Undo in the new review.")
        XCTAssertTrue(original.isHittable)
    }

    private func releaseFixture(_ address: String) {
        var request = URLRequest(url: URL(string: address + "/test/release-send")!)
        request.httpMethod = "POST"
        let done = expectation(description: "Release delayed write")
        URLSession.shared.dataTask(with: request) { _, _, _ in done.fulfill() }.resume()
        wait(for: [done], timeout: 5)
    }


    @MainActor
    func testNativeNavigation() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run the navigation fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch.waitForExistence(timeout: 15))
        let bar = app.tabBars.firstMatch
        XCTAssertTrue(bar.exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Native tabs expanded"; shot.lifetime = .keepAlways; add(shot)
        app.scrollViews.firstMatch.swipeUp()
        app.scrollViews.firstMatch.swipeUp()
        let collapsed = XCTAttachment(screenshot: app.screenshot())
        collapsed.name = "Native tabs collapsed"; collapsed.lifetime = .keepAlways; add(collapsed)
        XCTAssertEqual(app.tabBars.buttons["Bots"].value as? String, "Collapsed")
        app.tabBars.buttons["Bots"].tap()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 5))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 5))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch
        bot.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.tabBars.buttons["Inbox"].isHittable)
        app.buttons["Back"].tap()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 5))
    }

    @MainActor
    func testInlineReplyAndContextUseOneDecision() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let inbox = app.tabBars.buttons["Inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        inbox.tap()
        let context = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch
        XCTAssertTrue(context.waitForExistence(timeout: 5))
        let id = String(context.identifier.dropFirst("inbox-context-".count))
        let reply = app.textViews["decision-reply-" + id]
        let field = app.textFields["decision-reply-" + id]
        let input = reply.exists ? reply : field
        for _ in 0..<4 where !input.isHittable { app.swipeUp() }
        XCTAssertTrue(input.isHittable)
        input.tap()
        XCTAssertFalse(app.tabBars.buttons["Inbox"].isHittable)
        input.typeText("What did you verify?")
        app.buttons["send-decision-reply-" + id].tap()
        let answer = app.staticTexts["What did you verify?"]
        for _ in 0..<4 where !answer.isHittable { app.swipeUp() }
        XCTAssertTrue(answer.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-option-")).firstMatch.exists)
        input.tap()
        input.typeText("Which files changed?")
        app.buttons["send-decision-reply-" + id].tap()
        let earlier = app.buttons["decision-earlier-replies"]
        for _ in 0..<4 where !earlier.isHittable { app.swipeUp() }
        XCTAssertTrue(earlier.waitForExistence(timeout: 5))
        earlier.tap()
        XCTAssertTrue(answer.exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Inbox with inline reply"; shot.lifetime = .keepAlways; add(shot)
        for _ in 0..<6 where !context.isHittable { app.swipeDown() }
        context.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.tabBars.buttons["Inbox"].isHittable)
        XCTAssertTrue(app.textViews["message-input"].exists || app.textFields["message-input"].exists)
        XCTAssertFalse(app.buttons["Reply target"].exists)
    }

    @MainActor
    func testChatAlwaysUsesOneNormalComposer() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        let forge = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Forge")).firstMatch
        XCTAssertTrue(forge.waitForExistence(timeout: 10))
        forge.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Reply target"].exists)
        XCTAssertFalse(app.textFields.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-reply-")).firstMatch.exists)
        let input = app.textFields["message-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap(); input.typeText("Keep this draft")
        XCTAssertEqual(input.value as? String, "Keep this draft")
    }

    @MainActor
    func testCardHasNoMenuOrDetailsAndAnswerClearsSwipeUndo() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-menu-")).firstMatch.exists)
        XCTAssertFalse(app.buttons["Preview retry-fix-notes.md"].exists)
        XCTAssertFalse(app.buttons["decision-details"].exists)
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.buttons["inbox-review-later"].tap()
        let option = app.buttons["decision-option-00000000-0000-4000-8000-000000000061"]
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        option.tap()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
    }

    @MainActor
    func testReviewingPendingEndsPreviousUndoAndAllowsANewSwipeUndo() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        let card = app.scrollViews["inbox-card-scroll"]
        let originalFrame = card.frame
        let question = app.staticTexts["Save the retry fix?"]
        card.swipeLeft()
        let undo = app.buttons["undo-inbox-dismiss"]
        XCTAssertTrue(undo.waitForExistence(timeout: 3))
        app.buttons["inbox-review-later"].tap()
        XCTAssertTrue(question.waitForExistence(timeout: 3))
        XCTAssertFalse(undo.exists, "Starting a new review must end the previous swipe's Undo.")
        XCTAssertEqual(card.frame.minY, originalFrame.minY, accuracy: 1)
        XCTAssertEqual(card.frame.height, originalFrame.height, accuracy: 1)
        card.swipeLeft()
        XCTAssertTrue(undo.waitForExistence(timeout: 3), "A new Later swipe must still support Undo.")
        undo.tap()
        XCTAssertTrue(question.waitForExistence(timeout: 3))
        XCTAssertFalse(undo.exists)
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = "Reopened pending card has no stale Undo"; proof.lifetime = .keepAlways; add(proof)
    }

    @MainActor
    func testSwipeUndoExpiresAfterItsWindow() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        app.scrollViews["inbox-card-scroll"].swipeRight()
        let undo = app.buttons["undo-inbox-dismiss"]
        XCTAssertTrue(undo.waitForExistence(timeout: 5))
        let expired = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: undo)
        XCTAssertEqual(XCTWaiter.wait(for: [expired], timeout: 12), .completed)
    }

    @MainActor
    func testReplyClearsSwipeUndo() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.buttons["inbox-review-later"].tap()
        let field = app.textFields.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-reply-")).firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap(); field.typeText("What changes?")
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "send-decision-reply-")).firstMatch.tap()
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-option-")).firstMatch.exists)
    }

    @MainActor
    func testBodyTapStaysInInboxAndLaterDoesNotResolveDecision() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Inbox"].tap()
        let question = app.staticTexts["Save the retry fix?"]
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        question.tap()
        XCTAssertFalse(app.buttons["bot-settings"].exists)
        app.scrollViews["inbox-card-scroll"].swipeLeft()
        XCTAssertTrue(app.buttons["inbox-review-later"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["undo-inbox-dismiss"].waitForExistence(timeout: 5))
        app.tabBars.buttons["Bots"].tap()
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertFalse(app.buttons["undo-inbox-dismiss"].exists)
        app.buttons["inbox-review-later"].tap()
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "decision-option-")).firstMatch.exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Focused Inbox"; shot.lifetime = .keepAlways; add(shot)
    }
}
