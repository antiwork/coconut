import XCTest

final class NativeDecisionFlowTests: XCTestCase {
    @MainActor
    func testBotInstructionsAndRoutineEditingPersist() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()

        let forge = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(forge.waitForExistence(timeout: 10))
        forge.tap()
        let settings = app.buttons["bot-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        settings.tap()
        let instructions = app.descendants(matching: .any).matching(identifier: "bot-instructions").firstMatch
        XCTAssertTrue(instructions.waitForExistence(timeout: 5))
        capture("Compact native bot settings")
        XCTAssertFalse(app.buttons["bot-settings-model"].exists)
        XCTAssertFalse(app.staticTexts["Workflow permissions"].exists)
        instructions.tap()
        instructions.typeText(" Keep progress updates concise.")
        app.swipeUp()
        let addRoutine = app.buttons["add-routine"]
        revealInList(addRoutine, in: app)
        addRoutine.tap()
        XCTAssertTrue(app.navigationBars["New routine"].waitForExistence(timeout: 5))
        app.textFields["routine-name"].tap()
        app.textFields["routine-name"].typeText("Morning check")
        let prompt = app.descendants(matching: .any).matching(identifier: "routine-prompt").firstMatch
        prompt.tap()
        prompt.typeText("Summarize the open work.")
        app.textFields["routine-schedule"].tap()
        app.textFields["routine-schedule"].typeText("every 30m")
        app.buttons["save-routine"].tap()
        let routine = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Morning check")).firstMatch
        revealInList(routine, in: app)
        XCTAssertTrue(routine.label.contains("every 30m"))
        routine.tap()
        let enabled = app.switches["routine-enabled"]
        XCTAssertTrue(enabled.waitForExistence(timeout: 5))
        enabled.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        app.buttons["save-routine"].tap()
        revealInList(routine, in: app)
        XCTAssertTrue(routine.label.contains("Paused"))
        capture("Native routine paused")
    }

    @MainActor
    func testSearchAndSettingsReturnToHome() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()

        let forge = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        let scout = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(scout.waitForExistence(timeout: 10))
        XCTAssertTrue(scout.label.contains("I am checking background limits and reconnect behavior."),
                      "Home must show the latest conversation text instead of a generic status.")
        XCTAssertTrue(forge.exists)

        app.buttons["app-settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        capture("Settings")
        app.staticTexts["Model"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Model"].waitForExistence(timeout: 5))
        capture("Model settings")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        app.buttons["close-settings"].tap()
        XCTAssertTrue(app.buttons["home-inbox"].waitForExistence(timeout: 5))
        XCTAssertTrue(forge.isHittable)
        XCTAssertTrue(scout.isHittable)
        capture("Home after search and settings")
    }

    @MainActor
    func testAccessibilityTextKeepsDecisionActionsReachable() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo", "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        app.launch()

        let inbox = app.buttons["home-inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        XCTAssertTrue(inbox.isHittable)
        capture("Home with accessibility text")
        inbox.tap()
        XCTAssertFalse(app.buttons["inbox-filter-needsYou"].exists)
        let subject = app.staticTexts["inbox-subject-00000000-0000-4000-8000-000000000021"]
        XCTAssertTrue(subject.waitForExistence(timeout: 5))
        subject.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Task"].exists)

        let menuId = "decision-menu-00000000-0000-4000-8000-000000000031"
        for identifier in [menuId, "decision-option-00000000-0000-4000-8000-000000000061", "decision-option-00000000-0000-4000-8000-000000000062"] {
            let button = app.buttons[identifier]
            reveal(button, in: app)
            XCTAssertTrue(button.exists && button.isHittable, "\(identifier) must remain reachable at the largest text size.")
            XCTAssertGreaterThanOrEqual(button.frame.height, 44)
            XCTAssertGreaterThanOrEqual(button.frame.width, 44)
        }
        capture("Decision actions with accessibility text")
        let menu = app.buttons[menuId]
        reveal(menu, in: app)
        menu.tap()
        let question = app.buttons["ask-decision-context"]
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        XCTAssertTrue(question.isHittable)
        question.tap()
        XCTAssertTrue(app.navigationBars["Ask for context"].waitForExistence(timeout: 5))
        let input = app.descendants(matching: .any).matching(identifier: "decision-context-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        XCTAssertTrue(input.isHittable, "The question field must be visible without resizing the sheet.")
        input.tap()
        input.typeText("What changes?")
        XCTAssertTrue(app.buttons["submit-decision-context"].isEnabled)
        capture("Question editor with accessibility text")
    }

    @MainActor
    func testContextPreservesDecisionAndApprovalRecordsOutcome() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["--demo"]
        app.launch()

        let homeInbox = app.buttons["home-inbox"]
        XCTAssertTrue(homeInbox.waitForExistence(timeout: 10))
        XCTAssertFalse(app.tabBars.firstMatch.exists, "Home uses one conversation list without tabs.")
        XCTAssertTrue(app.buttons["create-bot"].isHittable)
        let forge = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(forge.waitForExistence(timeout: 5))
        forge.press(forDuration: 1.1)
        let pinAction = app.buttons["Pin bot"]
        XCTAssertTrue(pinAction.waitForExistence(timeout: 5), "A long press must show bot actions before navigation.")
        pinAction.tap()
        let pinnedForge = app.buttons["pinned-bot-00000000-0000-4000-8000-000000000001"]
        XCTAssertTrue(pinnedForge.waitForExistence(timeout: 5), "Pinning must create a bot tile on Home.")
        capture("Pinned bot and Inbox on Home")
        pinnedForge.tap()
        XCTAssertTrue(app.descendants(matching: .any).matching(identifier: "message-input").firstMatch.waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(homeInbox.waitForExistence(timeout: 5))
        homeInbox.tap()
        XCTAssertTrue(app.navigationBars["Inbox"].waitForExistence(timeout: 5))

        XCTAssertFalse(app.buttons["inbox-filter-all"].exists)
        XCTAssertFalse(app.buttons["inbox-filter-done"].exists)
        XCTAssertFalse(app.buttons["inbox-filter-needsYou"].exists)
        XCTAssertFalse(app.staticTexts["inbox-subject-00000000-0000-4000-8000-000000000022"].exists,
                       "Completed work must stay out of Inbox.")

        let thread = app.staticTexts["inbox-subject-00000000-0000-4000-8000-000000000021"]
        XCTAssertTrue(thread.waitForExistence(timeout: 10))
        capture("Decision inbox")
        thread.tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Task"].exists)
        XCTAssertFalse(app.staticTexts["The comparison is ready. Local networking fits the first milestone."].exists,
                       "The conversation must exclude another bot's messages.")

        let menu = app.buttons["decision-menu-00000000-0000-4000-8000-000000000031"]
        XCTAssertTrue(menu.isHittable, "Inbox must anchor directly to the decision card.")
        reveal(menu, in: app)
        XCTAssertTrue(menu.exists && menu.isHittable)
        menu.tap()
        app.buttons["decision-details"].tap()
        XCTAssertTrue(app.navigationBars["Decision details"].waitForExistence(timeout: 5))
        let proposedFile = app.staticTexts["retry-fix-notes.md"]
        revealInList(proposedFile, in: app)
        XCTAssertTrue(proposedFile.exists, "Details must expose the exact file for the second option before it runs.")
        capture("Review exact option details")
        app.buttons["close-decision-details"].tap()

        reveal(menu, in: app)
        menu.tap()
        let contextButton = app.buttons["ask-decision-context"]
        XCTAssertTrue(contextButton.waitForExistence(timeout: 5))
        contextButton.tap()
        let question = app.descendants(matching: .any).matching(identifier: "decision-context-input").firstMatch
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        question.tap()
        question.typeText("Does this publish any changes?")
        app.buttons["submit-decision-context"].tap()
        XCTAssertTrue(app.buttons["bot-settings"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.navigationBars["Task"].exists)

        let option = app.buttons["decision-option-00000000-0000-4000-8000-000000000062"]
        XCTAssertTrue(option.waitForExistence(timeout: 5), "Asking a question must keep the exact pending options.")
        let contextLink = app.buttons.matching(identifier: "View decision").firstMatch
        reveal(contextLink, in: app)
        XCTAssertTrue(contextLink.exists && contextLink.isHittable, "Context must link back to the decision.")
        contextLink.tap()
        reveal(option, in: app)
        XCTAssertTrue(option.isEnabled)
        XCTAssertEqual(option.label, "B. Patch with notes", "The row must preserve the backend option label and order.")
        XCTAssertFalse(app.buttons["approve-decision"].exists, "Dynamic choices must not require a second confirmation control.")
        capture("Context requested with decision pending")
        option.tap()

        let selection = app.staticTexts["decision-selection-00000000-0000-4000-8000-000000000031"]
        reveal(selection, in: app)
        XCTAssertTrue(selection.waitForExistence(timeout: 5))
        XCTAssertEqual(selection.label, "Patch with notes")
        XCTAssertFalse(app.staticTexts["Preview result saved. No real action ran."].exists)
        XCTAssertFalse(menu.exists, "Answered cards must keep only the question and selected option.")
        selection.press(forDuration: 1.1)
        app.buttons["Details"].tap()
        let outcome = app.staticTexts["Preview result saved. No real action ran."]
        revealInList(outcome, in: app)
        XCTAssertTrue(outcome.exists, "The full record must remain available through Details.")
        app.buttons["close-decision-details"].tap()
        XCTAssertFalse(option.exists, "One tap must replace the pending choices with a backend receipt.")
        capture("Recorded decision outcome")
        let savedFile = app.staticTexts["retry-fix-notes.md"]
        reveal(savedFile, in: app)
        XCTAssertTrue(savedFile.exists, "The selected option must create its own file, not the recommended option's file.")

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.navigationBars["Inbox"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["inbox-subject-00000000-0000-4000-8000-000000000021"].exists,
                       "Resolving the last decision must remove its Inbox row.")
        XCTAssertTrue(app.staticTexts["No pending decisions"].waitForExistence(timeout: 5))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(homeInbox.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["No pending decisions"].exists,
                      "Home must not use chat or completed work as the Inbox preview.")
        pinnedForge.tap()
        let history = app.staticTexts["decision-selection-00000000-0000-4000-8000-000000000031"]
        reveal(history, in: app)
        XCTAssertTrue(history.exists, "The resolved decision must remain in the originating bot conversation.")
    }

    @MainActor
    func testChoiceReceiptStaysStableDuringServerConfirmation() async throws {
        guard let address = ProcessInfo.processInfo.environment["COCONUT_LAYOUT_TEST_URL"] else {
            throw XCTSkip("Run scripts/test_conversation_layout.py with --decision-receipt.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": address, "COCONUT_PAIR_CODE": "layout-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        bot.tap()
        let option = app.buttons["decision-option-00000000-0000-4000-8000-000000000062"]
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        capture("Aligned decision choices")
        option.tap()
        XCTAssertTrue(app.staticTexts["Temporary test failure."].waitForExistence(timeout: 10))
        XCTAssertTrue(option.isEnabled, "A failed send must restore the original choices.")
        option.tap()
        let selection = app.staticTexts["decision-selection-00000000-0000-4000-8000-000000000031"]
        XCTAssertTrue(selection.waitForExistence(timeout: 1), "Selection must appear before the server accepts it.")
        XCTAssertEqual(selection.label, "Patch with notes")
        XCTAssertFalse(option.exists)
        XCTAssertFalse(app.activityIndicators.firstMatch.exists)
        XCTAssertFalse(app.buttons["decision-menu-00000000-0000-4000-8000-000000000031"].exists)
        let pendingFrame = selection.frame
        capture("Selected choice before confirmation")
        var request = URLRequest(url: URL(string: address + "/test/release-send")!)
        request.httpMethod = "POST"
        _ = try await URLSession.shared.data(for: request)
        XCTAssertTrue(app.images["Choice recorded"].waitForExistence(timeout: 10))
        XCTAssertEqual(selection.frame.minX, pendingFrame.minX, accuracy: 1)
        XCTAssertEqual(selection.frame.minY, pendingFrame.minY, accuracy: 1)
        XCTAssertEqual(selection.frame.width, pendingFrame.width, accuracy: 1)
        XCTAssertEqual(selection.frame.height, pendingFrame.height, accuracy: 1)
        XCTAssertFalse(app.staticTexts["Answer recorded. Hermes will continue."].exists)
        XCTAssertFalse(app.activityIndicators.firstMatch.exists)
        capture("Confirmed choice without a layout shift")
    }

    @MainActor
    private func revealInList(_ element: XCUIElement, in app: XCUIApplication) {
        for _ in 0..<5 {
            if element.exists && element.isHittable { return }
            app.swipeUp()
        }
        XCTAssertTrue(element.exists && element.isHittable, "The settings control must remain reachable.")
    }

    @MainActor
    private func reveal(_ element: XCUIElement, in app: XCUIApplication) {
        if element.exists && element.isHittable { return }
        let scrollView = app.scrollViews.firstMatch
        for _ in 0..<4 {
            scrollView.swipeUp()
            if element.exists && element.isHittable { return }
        }
        for _ in 0..<4 {
            scrollView.swipeDown()
            if element.exists && element.isHittable { return }
        }
        print("Could not reveal \(element). Accessibility hierarchy:\n\(app.debugDescription)")
    }

    @MainActor
    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
