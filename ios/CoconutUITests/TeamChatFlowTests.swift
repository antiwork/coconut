import XCTest

/// The main flow of the Team chat against real Coconut services: open the team from Chats, send, reply, edit, delete,
/// and load earlier messages. The test world of the services has the teams that the variables name: Example Team with
/// two people and a Mac without a person, whose bots Planner and Scribe answer each member; Busy Team with 60
/// messages of Grace; Old Project that Ada's Mac is no longer in; and Design Crew without a message. With
/// COCONUT_TEAM_SHOTS, the test saves a screenshot of each state in that folder.
///
/// A send that fails needs the Mac to refuse it. The test writes the file `delete-reply-target` into the folder of
/// COCONUT_TEAM_FLAGS, and a script of the test world removes the answered message on Grace's Mac. The reply then gets
/// "The message that you answer is no longer in the chat."
///
/// A message that names a bot checks the `mentions` that Ada's Mac saved: the test writes the file `check-mentions`
/// with the text, and a script of the test world writes the names of the mentioned bots into `mentions.json`.
final class TeamChatFlowTests: XCTestCase {
    private var app: XCUIApplication!
    private var shots: URL?

    @MainActor
    func testTheTeamChatFlow() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let example = env["COCONUT_TEAM_EXAMPLE"], let busy = env["COCONUT_TEAM_BUSY"] else {
            throw XCTSkip("Run with the test world of the Team chat.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()

        // The Chats tab lists the bots and the teams.
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let exampleRow = app.buttons["team-chat-\(example)"]
        XCTAssertTrue(exampleRow.waitForExistence(timeout: 15))
        XCTAssertTrue(exampleRow.label.contains("unread"), exampleRow.label)
        shot("01-iphone-chats-list-with-team-unread")

        // Each chip filters the list: People has the teams, Bots has the bots and the pinned tiles.
        let botRows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'bot-conversation-'"))
        let teamRows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'team-chat-'"))
        let pinnedRows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'pinned-bot-'"))
        XCTAssertGreaterThan(botRows.count, 0)
        XCTAssertGreaterThan(pinnedRows.count, 0)
        XCTAssertTrue(app.buttons["chats-filter-all"].isSelected)
        app.buttons["chats-filter-people"].tap()
        XCTAssertTrue(app.buttons["chats-filter-people"].isSelected)
        XCTAssertTrue(exampleRow.waitForExistence(timeout: 5))
        XCTAssertEqual(botRows.count, 0)
        XCTAssertEqual(pinnedRows.count, 0, "People has no pinned bots.")
        shot("02-iphone-chats-people")
        app.buttons["chats-filter-bots"].tap()
        XCTAssertTrue(botRows.firstMatch.waitForExistence(timeout: 5))
        XCTAssertEqual(teamRows.count, 0)
        XCTAssertGreaterThan(pinnedRows.count, 0)
        shot("03-iphone-chats-bots")
        app.buttons["chats-filter-all"].tap()
        XCTAssertTrue(exampleRow.waitForExistence(timeout: 5))
        XCTAssertGreaterThan(botRows.count, 0)

        // A chat of two people and a bot.
        exampleRow.tap()
        XCTAssertTrue(text("Planned the week").waitForExistence(timeout: 15))
        XCTAssertTrue(text("I will bring the printed slides.").exists)
        shot("02-iphone-team-chat-two-days-run-reply-edited")

        // The menu of an own message, the edit state, and the delete question.
        hold(text("I will bring the printed slides."), own: true)
        XCTAssertTrue(app.buttons["held-edit"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["held-delete"].exists)
        shot("03a-iphone-own-message-menu")
        app.buttons["held-edit"].tap()
        XCTAssertTrue(element("edit-preview").waitForExistence(timeout: 5))
        shot("03b-iphone-edit-state-of-composer")
        app.buttons["Cancel edit"].tap()
        XCTAssertFalse(element("edit-preview").exists)
        hold(text("I will bring the printed slides."), own: true)
        app.buttons["held-delete"].tap()
        XCTAssertTrue(app.buttons["team-delete-confirm"].waitForExistence(timeout: 5))
        shot("03c-iphone-delete-question")
        // The question shows as a popover. A tap beside it cancels it.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.55)).tap()
        XCTAssertTrue(app.buttons["team-delete-confirm"].waitForNonExistence(timeout: 5))
        XCTAssertTrue(text("I will bring the printed slides.").exists)
        // A message of another member has no Edit. Delete follows the admin (testTheAdminDeletesAMessageOfAnotherPerson).
        hold(text("I booked a table for four."))
        XCTAssertTrue(app.buttons["held-reply"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["held-edit"].exists)
        app.buttons["held-copy"].tap()
        back()

        if let crew = env["COCONUT_TEAM_CREW"] {
            app.buttons["team-chat-\(crew)"].tap()
            XCTAssertTrue(element("team-chat-empty").waitForExistence(timeout: 10))
            // The bot of the Mac without a person can answer, so the empty chat tells of @.
            XCTAssertTrue(element("team-chat-empty").label.contains("Type @ to ask a bot."),
                          element("team-chat-empty").label)
            shot("05a-iphone-empty-chat")
            back()
        }
        if let old = env["COCONUT_TEAM_OLD"] {
            app.buttons["team-chat-\(old)"].tap()
            XCTAssertTrue(element("team-chat-closed").waitForExistence(timeout: 10))
            XCTAssertTrue(text("This project is done").waitForExistence(timeout: 10), "The history stays readable.")
            XCTAssertFalse(element("message-input").exists)
            shot("05b-iphone-chat-of-a-team-this-mac-left")
            back()
        }

        // Busy Team: send, reply, edit, delete, load earlier messages.
        let busyRow = app.buttons["team-chat-\(busy)"]
        XCTAssertTrue(busyRow.waitForExistence(timeout: 10))
        busyRow.tap()
        XCTAssertTrue(text("Busy message 60.").waitForExistence(timeout: 15))
        write("Hello from the test")
        XCTAssertTrue(text("Hello from the test").waitForExistence(timeout: 15))

        hold(text("Busy message 60."))
        app.buttons["held-reply"].tap()
        XCTAssertTrue(element("reply-preview").waitForExistence(timeout: 5))
        write("A reply to Grace")
        let quote = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'team-message-quote-' AND label CONTAINS 'Grace'")).firstMatch
        XCTAssertTrue(quote.waitForExistence(timeout: 15))
        XCTAssertTrue(text("A reply to Grace").exists)

        hold(text("Hello from the test"), own: true)
        app.buttons["held-edit"].tap()
        XCTAssertTrue(element("edit-preview").waitForExistence(timeout: 5))
        let input = element("message-input")
        input.tap()
        input.typeText(" again")
        app.buttons["send-message"].tap()
        let edited = text("Hello from the test again")
        XCTAssertTrue(edited.waitForExistence(timeout: 15))
        XCTAssertTrue(edited.label.contains("Edited"), edited.label)

        hold(edited, own: true)
        app.buttons["held-delete"].tap()
        XCTAssertTrue(app.buttons["team-delete-confirm"].waitForExistence(timeout: 5))
        app.buttons["team-delete-confirm"].firstMatch.tap()
        XCTAssertTrue(edited.waitForNonExistence(timeout: 15))

        let earlier = app.buttons["load-earlier-messages"]
        let transcript = app.collectionViews["team-chat-transcript"]
        for _ in 0..<20 where !(earlier.exists && earlier.isHittable) { transcript.swipeDown(velocity: .fast) }
        XCTAssertTrue(earlier.isHittable)
        earlier.tap()
        let oldest = text("Busy message 1.")
        for _ in 0..<20 where !oldest.exists { transcript.swipeDown(velocity: .fast) }
        XCTAssertTrue(oldest.waitForExistence(timeout: 15))
        XCTAssertFalse(earlier.exists, "The chat has its oldest message.")

        // A send that the Mac refuses keeps its text, with a way to try again and a way to remove it.
        if let flags = env["COCONUT_TEAM_FLAGS"] {
            // Back to the newest messages.
            for _ in 0..<8 { transcript.swipeUp(velocity: .fast) }
            hold(text("Busy message 59."))
            app.buttons["held-reply"].tap()
            XCTAssertTrue(element("reply-preview").waitForExistence(timeout: 5))
            FileManager.default.createFile(atPath: flags + "/delete-reply-target", contents: Data())
            XCTAssertTrue(text("Busy message 59.").waitForNonExistence(timeout: 60), "Grace removed the message.")
            write("This reply cannot send")
            let retry = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'retry-message-'")).firstMatch
            XCTAssertTrue(retry.waitForExistence(timeout: 20))
            XCTAssertTrue(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'delete-message-'")).firstMatch.exists)
            XCTAssertTrue(text("This reply cannot send").exists)
            shot("04-iphone-message-not-sent")
            app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'delete-message-'")).firstMatch.tap()
            XCTAssertTrue(text("This reply cannot send").waitForNonExistence(timeout: 5))
        }
        if app.buttons["Dismiss error"].exists { app.buttons["Dismiss error"].firstMatch.tap() }
        back()

        exampleRow.tap()
        XCTAssertTrue(text("Planned the week").waitForExistence(timeout: 15))

        // @ opens the list of the team bots that answer each member, as in a bot chat. Letters filter it, and a
        // pick names the bot. A bot of a person is not in the list.
        let mentionRows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'mention-bot-t-'"))
        let field = element("message-input")
        field.tap()
        field.typeText("@")
        XCTAssertTrue(element("bot-mention-picker").waitForExistence(timeout: 5))
        XCTAssertEqual(mentionRows.count, 2, "Planner and Scribe of the Mac without a person")
        XCTAssertFalse(app.buttons["Reporter"].exists, "A bot of this person is not in the list.")
        XCTAssertTrue(app.buttons["Planner"].exists, "VoiceOver reads a row as the name of the bot.")
        shot("01-iphone-team-mention-picker")
        field.typeText("Pl")
        XCTAssertTrue(app.buttons["Scribe"].waitForNonExistence(timeout: 5))
        XCTAssertEqual(mentionRows.count, 1)
        shot("02-iphone-team-mention-picker-filtered")
        app.buttons["Planner"].tap()
        XCTAssertTrue(element("bot-mention-picker").waitForNonExistence(timeout: 5))
        field.typeText("what is the plan for Friday?")
        XCTAssertEqual(field.value as? String, "@Planner what is the plan for Friday?")
        shot("03-iphone-team-mention-in-composer")
        app.buttons["send-message"].tap()
        XCTAssertTrue(text("@Planner what is the plan for Friday?").waitForExistence(timeout: 15))
        shot("04-iphone-team-mention-sent")
        if let flags = env["COCONUT_TEAM_FLAGS"] {
            let answer = URL(fileURLWithPath: flags + "/mentions.json")
            try? FileManager.default.removeItem(at: answer)
            FileManager.default.createFile(atPath: flags + "/check-mentions",
                                           contents: Data("@Planner what is the plan for Friday?".utf8))
            var names: [String]?
            for _ in 0..<60 where names == nil {
                names = (try? Data(contentsOf: answer)).flatMap { try? JSONDecoder().decode([String].self, from: $0) }
                if names == nil { sleep(1) }
            }
            XCTAssertEqual(names, ["Planner"], "The Mac got the mention of Planner.")
        }

        // The title opens the Team screen.
        app.buttons["team-chat-title"].tap()
        XCTAssertTrue(app.buttons["team-leave"].waitForExistence(timeout: 10))
    }

    /// Example Team in dark mode. A test run sets the simulator to light, so the app gets the style as an argument.
    /// The Chats list, and the chat of Example Team, in one look of the simulator: "dark" (dark mode), "large" (the
    /// largest text size), or "solo" (a Mac without a team: no chips). A test run sets the simulator to light, so the
    /// app gets the look as a launch argument.
    @MainActor
    func testTheChatsInALook() throws {
        let env = ProcessInfo.processInfo.environment
        guard let look = env["COCONUT_TEAM_LOOK"], let url = env["COCONUT_TEAM_URL"],
              let code = env["COCONUT_TEAM_CODE"] else {
            throw XCTSkip("Run with the test world of the Team chat and COCONUT_TEAM_LOOK.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        if look == "pill", let example = env["COCONUT_TEAM_EXAMPLE"] {
            try measureDayPills(example: example)
            return
        }
        if look == "solo" {
            let anyRow = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'bot-conversation-'")).firstMatch
            XCTAssertTrue(anyRow.waitForExistence(timeout: 15))
            sleep(2)
            XCTAssertFalse(app.buttons["chats-filter-all"].exists, "A Mac without a team shows no chips.")
            shot("look-solo-list")
            return
        }
        XCTAssertTrue(app.buttons["chats-filter-all"].waitForExistence(timeout: 15))
        for chip in ["all", "people", "bots"] {
            // Hittable: the chip is on screen and not cut, also with the largest text.
            XCTAssertTrue(app.buttons["chats-filter-\(chip)"].isHittable, "Each chip shows: \(chip)")
        }
        shot("look-\(look)-list")
        let mention = env["COCONUT_TEAM_MENTION"] != nil
        guard look != "large" || mention, let example = env["COCONUT_TEAM_EXAMPLE"] else { return }
        let row = app.buttons["team-chat-\(example)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        XCTAssertTrue(text("Planned the week").waitForExistence(timeout: 15))
        guard mention else {
            shot("look-\(look)-chat")
            return
        }
        // COCONUT_TEAM_MENTION: the list of the team bots in this look.
        let field = element("message-input")
        field.tap()
        field.typeText("@")
        XCTAssertTrue(element("bot-mention-picker").waitForExistence(timeout: 5))
        shot("05-iphone-team-mention-picker-\(look)")
    }

    /// The Team screen of a team with Macs for a team only, in its own test world: Ada and Grace, Reporter Mac with
    /// the bots Reporter and Scribe, and Spare Mac without a bot. COCONUT_TEAM_BOTS names that team, and
    /// COCONUT_TEAM_BOT_ROWS has what VoiceOver reads for each row of the section "Bots", in order, with "|" between
    /// the rows. COCONUT_TEAM_PEOPLE names a team with people only. COCONUT_TEAM_LOOK "dark" or "large" sets the look.
    @MainActor
    func testTheTeamScreenShowsTheMacsForATeamOnlyAsBots() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"], let room = env["COCONUT_TEAM_BOTS"],
              let expected = env["COCONUT_TEAM_BOT_ROWS"]?.split(separator: "|").map(String.init) else {
            throw XCTSkip("Run with the test world of the Team screen.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        let look = env["COCONUT_TEAM_LOOK"] ?? "light"
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()

        let row = app.buttons["team-chat-\(room)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        shot("team-\(look)-chats-list")
        row.tap()
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        XCTAssertTrue(title.label.contains("2 people"), title.label)
        shot("team-\(look)-chat-title")
        title.tap()

        // The rows of the section "Bots", found by their identifiers, each read as one sentence.
        let rows = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'team-bot-' OR identifier BEGINSWITH 'team-mac-'"))
        XCTAssertTrue(rows.firstMatch.waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["Ada"].exists && app.staticTexts["Grace"].exists, "The people are members.")
        XCTAssertFalse(app.staticTexts["Reporter Mac"].exists, "A Mac with a bot shows as its bots, not as a person.")
        let ask = app.staticTexts["Type @ in the team chat to ask a bot."]
        // With the largest text the screen scrolls down to Leave, its last row, and reads each row on the way.
        var labels: [String] = []
        var asked = false
        let leave = app.buttons["team-leave"]
        for step in 0..<6 {
            for index in 0..<rows.count where !labels.contains(rows.element(boundBy: index).label) {
                labels.append(rows.element(boundBy: index).label)
            }
            asked = asked || ask.exists
            shot("team-\(look)-screen" + (step == 0 ? "" : "-\(step)"))
            if leave.exists && leave.isHittable { break }
            app.swipeUp(velocity: .slow)
        }
        XCTAssertEqual(labels, expected)
        XCTAssertEqual(asked, expected.contains { $0.hasSuffix(", bot") },
                       "The line under the section shows only while a bot answers.")

        guard let people = env["COCONUT_TEAM_PEOPLE"] else { return }
        // The Team screen, then the chat.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        back()
        let peopleRow = app.buttons["team-chat-\(people)"]
        XCTAssertTrue(peopleRow.waitForExistence(timeout: 10))
        peopleRow.tap()
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        XCTAssertTrue(app.buttons["team-leave"].waitForExistence(timeout: 10))
        XCTAssertEqual(rows.count, 0)
        XCTAssertFalse(app.staticTexts["Bots"].exists, "A team with people only has no section \"Bots\".")
        shot("team-\(look)-people-only")
    }

    /// The gap between the title of a chat and its first day pill, with the chat scrolled to its top: in the team chat
    /// (a title of two lines) and in a bot chat (one line). Both pills sit fully below their title, with the same gap.
    @MainActor
    private func measureDayPills(example: String) throws {
        func gap(title: String) -> CGFloat {
            let transcript = app.collectionViews.firstMatch
            for _ in 0..<6 { transcript.swipeDown(velocity: .fast) }
            sleep(2)
            let pill = app.descendants(matching: .any).matching(identifier: "day-separator").firstMatch
            XCTAssertTrue(pill.waitForExistence(timeout: 10))
            return pill.frame.minY - app.buttons[title].frame.maxY
        }
        let teamRow = app.buttons["team-chat-\(example)"]
        XCTAssertTrue(teamRow.waitForExistence(timeout: 15))
        teamRow.tap()
        XCTAssertTrue(text("Planned the week").waitForExistence(timeout: 15))
        let teamGap = gap(title: "team-chat-title")
        shot("pill-team-chat-top")
        // During a scroll the pinned day pill shows at the top of the chat, below the title.
        app.collectionViews.firstMatch.swipeUp(velocity: .slow)
        shot("pill-team-chat-scrolling", settle: false)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let pinned = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'pinned-bot-'")).firstMatch
        XCTAssertTrue(pinned.waitForExistence(timeout: 10))
        pinned.tap()
        let input = element("message-input")
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        input.tap()
        input.typeText("Hello")
        app.buttons["send-message"].tap()
        app.collectionViews.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3)).tap()
        let botGap = gap(title: "bot-settings")
        shot("pill-bot-chat-top")
        print("DAY PILL GAPS team=\(teamGap) bot=\(botGap)")
        let note = XCTAttachment(string: "team gap \(teamGap), bot gap \(botGap)")
        note.name = "Day pill gaps"
        note.lifetime = .keepAlways
        add(note)
        XCTAssertGreaterThan(teamGap, 0, "The day pill sits fully below the title of the team.")
        XCTAssertEqual(teamGap, botGap, accuracy: 2, "The same gap as in the bot chat.")
    }

    // MARK: Steps

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    /// A message by a part of its text. VoiceOver reads the writer, the time and the text.
    private func text(_ part: String) -> XCUIElement {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'team-message-' AND label CONTAINS %@", part)).firstMatch
    }

    /// The text of a message spans its row. The bubble sits on the right for an own message, else on the left.
    private func hold(_ message: XCUIElement, own: Bool = false) {
        XCTAssertTrue(message.waitForExistence(timeout: 10))
        // A tap on the chat closes the keyboard, so the message is not under it.
        if app.keyboards.firstMatch.exists {
            app.collectionViews["team-chat-transcript"].coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2)).tap()
            _ = app.keyboards.firstMatch.waitForNonExistence(timeout: 3)
        }
        message.coordinate(withNormalizedOffset: CGVector(dx: own ? 0.92 : 0.08, dy: 0.5)).press(forDuration: 1)
    }

    private func write(_ value: String) {
        let input = element("message-input")
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText(value)
        app.buttons["send-message"].tap()
    }

    /// A tap on a team notification opens the chat of the team, also after the app was terminated. As in
    /// NotificationFlowTests, the test world sends the notification: the test writes the file `backgrounded` into the
    /// folder of COCONUT_TEAM_FLAGS, and a script of the world pushes to the simulator the payload that the service
    /// makes for Ada's phone. COCONUT_TEAM_NOTIFY names the team, and COCONUT_PUSH_EXPECTED_TITLE and
    /// COCONUT_PUSH_EXPECTED_BODY the alert.
    @MainActor
    func testATapOnATeamNotificationOpensTheTeamChat() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"], let room = env["COCONUT_TEAM_NOTIFY"],
              let flags = env["COCONUT_TEAM_FLAGS"].map({ URL(fileURLWithPath: $0) }),
              let title = env["COCONUT_PUSH_EXPECTED_TITLE"], let body = env["COCONUT_PUSH_EXPECTED_BODY"] else {
            throw XCTSkip("Run with the test world of the team notifications.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        allowNotifications()
        XCUIDevice.shared.press(.home)
        app.terminate()
        try Data().write(to: flags.appendingPathComponent("backgrounded"))

        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let banner = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", body)).firstMatch
        XCTAssertTrue(banner.waitForExistence(timeout: 45), "The banner must show the text of the message.")
        XCTAssertTrue(banner.label.contains(title), banner.label)
        screen("team-push-banner")
        banner.tap()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        let chat = app.buttons["team-chat-title"]
        XCTAssertTrue(chat.waitForExistence(timeout: 15), "The tap opens the chat of the team.")
        XCTAssertFalse(app.buttons["team-chat-\(room)"].exists, "The chat is open, not the Chats list.")
        shot("team-push-chat-after-tap")
    }

    /// The switch "Notifications" on the Team screen saves at once and shows the same state after the app opens again.
    /// COCONUT_TEAM_NOTIFY names the team. COCONUT_TEAM_LOOK "dark" or "large" sets the look.
    @MainActor
    func testTheSwitchOfTheTeamSavesAndShowsAgainAfterAReopen() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let room = env["COCONUT_TEAM_NOTIFY"] else {
            throw XCTSkip("Run with the test world of the team notifications.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        let look = env["COCONUT_TEAM_LOOK"] ?? "light"
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        let toggle = try openTeamScreen(room)
        XCTAssertEqual(toggle.value as? String, "1", "A team notifies at the start.")
        XCTAssertTrue(app.staticTexts["Get notified when someone writes in this team."].exists)
        shot("team-push-\(look)-switch-on")
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertTrue(waitForValue(toggle, "0"))
        // The answer of the Mac keeps the switch off.
        sleep(2)
        XCTAssertEqual(toggle.value as? String, "0")
        shot("team-push-\(look)-switch-off")

        app.terminate()
        app.launchEnvironment = [:]
        app.launch()
        let again = try openTeamScreen(room)
        XCTAssertEqual(again.value as? String, "0", "The Mac kept the choice.")
        again.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertTrue(waitForValue(again, "1"))
    }

    /// When the Mac refuses the change, the switch goes back and the screen shows the error. The test writes the file
    /// `hold-store` into the folder of COCONUT_TEAM_FLAGS. A script of the test world then holds the write lock of
    /// the store of Ada's Mac and writes `store-held`, so the next write fails. The test writes `release-store` at
    /// its end.
    @MainActor
    func testTheSwitchGoesBackWhenTheMacRefusesTheChange() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let room = env["COCONUT_TEAM_NOTIFY"], env["COCONUT_TEAM_REFUSE"] == "1",
              let flags = env["COCONUT_TEAM_FLAGS"].map({ URL(fileURLWithPath: $0) }) else {
            throw XCTSkip("Run with the test world of the team notifications and COCONUT_TEAM_REFUSE.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        let toggle = try openTeamScreen(room)
        XCTAssertEqual(toggle.value as? String, "1")
        try Data().write(to: flags.appendingPathComponent("hold-store"))
        defer { try? Data().write(to: flags.appendingPathComponent("release-store")) }
        let held = flags.appendingPathComponent("store-held")
        for _ in 0..<30 where !FileManager.default.fileExists(atPath: held.path) { sleep(1) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: held.path), "The script holds the store.")
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        let banner = app.descendants(matching: .any).matching(identifier: "action-error").firstMatch
        XCTAssertTrue(banner.waitForExistence(timeout: 30), "The screen shows the error of the Mac.")
        XCTAssertTrue(waitForValue(toggle, "1"), "The switch goes back.")
        shot("team-push-switch-refused")
    }

    // MARK: Typing

    /// Another person types, and the row of the chat shows and then goes (docs/team-log.md, "Typing"). The test world of
    /// typing has Example Team (COCONUT_TYPING_TEAM) with Ada, the person of this app, Grace, Lin, and Reporter Mac
    /// with the bot Reporter. A script of the world acts on the files in COCONUT_TEAM_FLAGS: `grace-types` makes Grace
    /// type one time (then `grace-typed`), `both-type` makes Grace and Lin type (`both-typed`), and `bot-types` makes
    /// Grace ask Reporter, whose turn runs until `bot-done` (`bot-typing` when the gateway shows the turn).
    /// COCONUT_TEAM_LOOK "dark" or "large" sets the look and takes only the pictures of Grace.
    @MainActor
    func testAnotherPersonTypesAndTheRowShowsThenGoes() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let room = env["COCONUT_TYPING_TEAM"], let flags = env["COCONUT_TEAM_FLAGS"] else {
            throw XCTSkip("Run with the test world of typing.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        let look = env["COCONUT_TEAM_LOOK"] ?? "light"
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let row = app.buttons["team-chat-\(room)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))

        // The row of the team in the Chats list says who types, in place of the newest message.
        try typingAct(flags, "grace-types", done: "grace-typed")
        XCTAssertTrue(waitForLabel(row, contains: "Grace is typing", timeout: 10), row.label)
        shot("typing-\(look)-4-chats-row")
        row.tap()
        let typing = element("team-typing")
        XCTAssertTrue(typing.waitForExistence(timeout: 10), "The chat shows who types.")
        XCTAssertEqual(typing.label, "Grace is typing")
        shot("typing-\(look)-1-chat-grace", settle: false)
        // Grace stops: the row goes after its seconds, without a new entry.
        XCTAssertTrue(waitUntilGone(typing, timeout: 20), "The row goes after its seconds.")
        guard look == "light" else { return }

        try typingAct(flags, "both-type", done: "both-typed")
        XCTAssertTrue(waitForLabel(typing, contains: "Grace and Lin are typing", timeout: 10), typing.label)
        shot("typing-light-3-chat-grace-and-lin", settle: false)
        XCTAssertTrue(waitUntilGone(typing, timeout: 20))

        // A bot types while its turn runs, also after a message of the turn.
        try typingAct(flags, "bot-types", done: "bot-typing", wait: 150)
        XCTAssertTrue(waitForLabel(typing, contains: "Reporter is typing", timeout: 15), typing.label)
        shot("typing-light-2-chat-reporter", settle: false)
        try typingAct(flags, "bot-done", done: "bot-answered")
        XCTAssertTrue(waitUntilGone(typing, timeout: 15), "The row goes at the end of the turn.")
    }

    private func waitUntilGone(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        return XCTWaiter().wait(for: [gone], timeout: timeout) == .completed
    }

    /// Waits for a script of the test world of typing: the test writes `flag`, and the script writes `done`.
    private func typingAct(_ flags: String, _ flag: String, done: String, wait: Int = 60) throws {
        let folder = URL(fileURLWithPath: flags)
        try? FileManager.default.removeItem(at: folder.appendingPathComponent(done))
        try Data().write(to: folder.appendingPathComponent(flag))
        let path = folder.appendingPathComponent(done).path
        for _ in 0..<(wait * 2) where !FileManager.default.fileExists(atPath: path) { usleep(500_000) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: path), "The script of the test world did \(flag).")
    }

    // MARK: Named chats

    /// The test world of the named chats: Example Team (COCONUT_CHATS_TEAM) with Ada, the admin, Grace, Lin and the bot
    /// Reporter of a Mac for a team only. COCONUT_CHATS_DESIGN and COCONUT_CHATS_LAUNCH are the IDs of the chats
    /// "Design" and "Launch". Grace named Ada in "Design", "Launch" has no message, and Grace wrote Ada a direct message.
    private struct ChatsWorld {
        let url: String, code: String, room: String, design: String, launch: String
        let flags: String?

        init() throws {
            let env = ProcessInfo.processInfo.environment
            guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
                  let room = env["COCONUT_CHATS_TEAM"], let design = env["COCONUT_CHATS_DESIGN"],
                  let launch = env["COCONUT_CHATS_LAUNCH"] else {
                throw XCTSkip("Run with the test world of the named chats.")
            }
            self.url = url; self.code = code; self.room = room; self.design = design; self.launch = launch
            flags = env["COCONUT_TEAM_FLAGS"]
        }
    }

    /// The admin makes a chat on the Team screen. A message in it shows there and not in the Team chat. The admin
    /// deletes the chat with the swipe action of its row, and the row goes from the Team screen and from the Chats list.
    @MainActor
    func testTheAdminMakesAChatWritesThereAndDeletesIt() throws {
        let world = try ChatsWorld()
        launchTeamScreen(url: world.url, code: world.code, room: world.room)
        let new = app.buttons["team-new-chat"]
        XCTAssertTrue(new.waitForExistence(timeout: 10))
        new.tap()
        let field = app.textFields["team-chat-name"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.typeText("Planning")
        app.buttons["team-chat-name-save"].tap()
        let row = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'team-chat-row-' AND label CONTAINS 'Planning'")).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 15), "The new chat is a row of the section Chats.")
        let id = String(row.identifier.dropFirst("team-chat-row-".count))
        row.tap()
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        XCTAssertTrue(title.label.hasPrefix("Planning, Example Team"), title.label)
        write("Hello Planning")
        XCTAssertTrue(text("Hello Planning").waitForExistence(timeout: 15))
        // The Team chat does not have the message.
        back()
        let team = app.buttons["team-chat-\(world.room)"]
        XCTAssertTrue(app.buttons["team-chat-\(world.room)-\(id)"].waitForExistence(timeout: 10))
        team.tap()
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        XCTAssertTrue(text("The weekly report is out.").waitForExistence(timeout: 15))
        XCTAssertFalse(text("Hello Planning").exists)
        title.tap()
        // The admin deletes the chat: the swipe action asks first.
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.swipeLeft()
        let delete = app.buttons["team-chat-delete-swipe"]
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        delete.tap()
        let confirm = app.buttons["team-chat-delete-confirm"].firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Delete this chat for everyone?"].exists)
        confirm.tap()
        XCTAssertTrue(row.waitForNonExistence(timeout: 15), "The row goes from the Team screen.")
        // The Team screen goes back to the Team chat, and the Team chat to the Chats list.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        back()
        XCTAssertTrue(team.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["team-chat-\(world.room)-\(id)"].exists, "The row goes from the Chats list.")
    }

    /// The screens of the named chats for a look: the Chats list, the Team screen, the chat "Design" with Reporter. In
    /// the light look also the sheet "New chat" with its error, and the actions and the question of a delete. With
    /// COCONUT_CHATS_AS (the name of a person who is not the admin), only the Team screen.
    @MainActor
    func testTheNamedChatsInALook() throws {
        let world = try ChatsWorld()
        guard let look = ProcessInfo.processInfo.environment["COCONUT_CHATS_LOOK"] else {
            throw XCTSkip("Set COCONUT_CHATS_LOOK.")
        }
        if let person = ProcessInfo.processInfo.environment["COCONUT_CHATS_AS"] {
            launchTeamScreen(url: world.url, code: world.code, room: world.room)
            XCTAssertTrue(app.buttons["team-chat-row-\(world.design)"].waitForExistence(timeout: 10))
            XCTAssertFalse(app.buttons["team-new-chat"].exists, "Only the admin makes a chat.")
            shot("chats-\(person)-3-team-screen")
            return
        }
        app = XCUIApplication()
        shots = ProcessInfo.processInfo.environment["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: world.url, code: world.code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let design = app.buttons["team-chat-\(world.room)-\(world.design)"]
        XCTAssertTrue(design.waitForExistence(timeout: 15))
        XCTAssertTrue(waitForLabel(design, contains: "You are named", timeout: 10), design.label)
        XCTAssertTrue(design.label.hasPrefix("Design, Example Team"), design.label)
        XCTAssertTrue(app.buttons["team-chat-\(world.room)-\(world.launch)"].label.contains("No messages yet"))
        shot("chats-\(look)-1-list")

        // The Team screen of the admin: the section Chats with "New chat".
        app.buttons["team-chat-\(world.room)"].tap()
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        let new = app.buttons["team-new-chat"]
        XCTAssertTrue(new.waitForExistence(timeout: 10))
        shot("chats-\(look)-2-team-screen")
        if look == "light" {
            new.tap()
            let field = app.textFields["team-chat-name"]
            XCTAssertTrue(field.waitForExistence(timeout: 5))
            shot("chats-light-4-new-chat")
            field.typeText("design")
            app.buttons["team-chat-name-save"].tap()
            XCTAssertTrue(app.staticTexts["This team already has a chat with this name."].waitForExistence(timeout: 10))
            shot("chats-light-4-new-chat-error")
            app.buttons["Cancel"].tap()
            let row = app.buttons["team-chat-row-\(world.design)"]
            XCTAssertTrue(row.waitForExistence(timeout: 5))
            row.swipeLeft()
            XCTAssertTrue(app.buttons["team-chat-rename-swipe"].waitForExistence(timeout: 5))
            shot("chats-light-6-actions")
            app.buttons["team-chat-delete-swipe"].tap()
            XCTAssertTrue(app.buttons["team-chat-delete-confirm"].firstMatch.waitForExistence(timeout: 5))
            shot("chats-light-6-delete-question")
            cancelQuestion()
        }

        // The chat "Design": the message of Grace, then Reporter types and answers.
        let row = app.buttons["team-chat-row-\(world.design)"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        // The newest message of Grace: with the largest text, the older ones are above the screen.
        XCTAssertTrue(text("@Ada").waitForExistence(timeout: 15))
        shot("chats-\(look)-5-design")
        guard look == "light", let flags = world.flags else { return }
        try typingAct(flags, "bot-types", done: "bot-typing", wait: 150)
        let typing = element("team-typing")
        XCTAssertTrue(waitForLabel(typing, contains: "Reporter is typing", timeout: 15), typing.label)
        shot("chats-light-5-design-typing", settle: false)
        try typingAct(flags, "bot-done", done: "bot-answered")
        XCTAssertTrue(text("Two designs are open").waitForExistence(timeout: 15))
        XCTAssertFalse(text("The weekly report is out.").exists, "The Team chat stays apart.")
        shot("chats-light-5-design-answer")
    }

    // MARK: Direct messages

    /// The test world of the direct messages: Example Team (COCONUT_TEAM_EXAMPLE) with Ada, the person of this app,
    /// Grace and Lin, whose Macs read direct messages, and Noor, whose Mac does not. COCONUT_DIRECT_GRACE,
    /// COCONUT_DIRECT_LIN and COCONUT_DIRECT_NOOR are their member IDs. Grace and Ada wrote each other already.
    private struct DirectWorld {
        let url: String, code: String, room: String, grace: String, lin: String, noor: String
        let flags: URL?

        init() throws {
            let env = ProcessInfo.processInfo.environment
            guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
                  let room = env["COCONUT_TEAM_EXAMPLE"], let grace = env["COCONUT_DIRECT_GRACE"],
                  let lin = env["COCONUT_DIRECT_LIN"], let noor = env["COCONUT_DIRECT_NOOR"] else {
                throw XCTSkip("Run with the test world of the direct messages.")
            }
            self.url = url; self.code = code; self.room = room; self.grace = grace; self.lin = lin; self.noor = noor
            flags = env["COCONUT_TEAM_FLAGS"].map { URL(fileURLWithPath: $0) }
        }
    }

    @MainActor
    private func launchDirect(_ world: DirectWorld, look: String = "light") {
        shots = ProcessInfo.processInfo.environment["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: world.url, code: world.code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
    }

    /// Waits for a script of the test world: the test writes `flag`, and the script writes `done` when it acted.
    private func act(_ world: DirectWorld, _ flag: String, done: String) throws {
        let flags = try XCTUnwrap(world.flags, "Set COCONUT_TEAM_FLAGS.")
        try? FileManager.default.removeItem(at: flags.appendingPathComponent(done))
        try Data().write(to: flags.appendingPathComponent(flag))
        let path = flags.appendingPathComponent(done).path
        for _ in 0..<60 where !FileManager.default.fileExists(atPath: path) { sleep(1) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: path), "The script of the test world did \(flag).")
    }

    /// An element whose label contains the text, such as a message of a chat.
    private func labeled(_ text: String) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    private func waitForLabel(_ element: XCUIElement, contains text: String?, timeout: TimeInterval = 30) -> Bool {
        let format = text == nil ? "NOT (label CONTAINS 'unread')" : "label CONTAINS %@"
        let predicate = text.map { NSPredicate(format: format, $0) } ?? NSPredicate(format: format)
        return XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: element)],
                                timeout: timeout) == .completed
    }

    /// One tap on a person of the Team screen opens the direct chat. A message sends. The row of the chat shows a new
    /// message of Grace as unread, and the count goes when the chat opens. The name of Grace above her message in the
    /// team chat opens the direct chat too.
    @MainActor
    func testADirectChatOpensFromTheTeamScreenAndFromAName() throws {
        let world = try DirectWorld()
        launchDirect(world)
        let graceRow = app.buttons["direct-chat-\(world.grace)"]
        XCTAssertTrue(graceRow.waitForExistence(timeout: 15), "The direct chat with Grace is a row of the Chats list.")
        shot("dm-chats-list")
        app.buttons["chats-filter-people"].tap()
        XCTAssertTrue(graceRow.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["team-chat-\(world.room)"].exists)
        shot("dm-chats-people")
        app.buttons["chats-filter-bots"].tap()
        XCTAssertFalse(graceRow.waitForExistence(timeout: 2), "Bots shows no direct chat.")
        app.buttons["chats-filter-all"].tap()

        // The Team screen: an arrow for Grace and Lin, none for Noor.
        app.buttons["team-chat-\(world.room)"].tap()
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        let grace = app.buttons["team-member-\(world.grace)"]
        XCTAssertTrue(grace.waitForExistence(timeout: 15))
        XCTAssertTrue(app.buttons["team-member-\(world.lin)"].exists)
        XCTAssertFalse(app.buttons["team-member-\(world.noor)"].exists, "Noor's Mac has no direct messages.")
        XCTAssertTrue(app.staticTexts["Noor"].exists)
        shot("dm-team-screen")
        grace.tap()

        // The direct chat: Grace and the team in the title, the reply with its quote, and a send.
        let chatTitle = app.descendants(matching: .any).matching(identifier: "direct-chat-title").firstMatch
        XCTAssertTrue(chatTitle.waitForExistence(timeout: 10), "One tap opens the direct chat.")
        XCTAssertTrue(chatTitle.label.contains("Grace") && chatTitle.label.contains("Example Team"), chatTitle.label)
        XCTAssertTrue(labeled("I will bring the draft.").waitForExistence(timeout: 15))
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label == 'View replied message from You'")).firstMatch
            .exists, "Grace's reply quotes Ada's message.")
        shot("dm-chat-grace")
        let input = app.textViews["message-input"].exists ? app.textViews["message-input"] : app.textFields["message-input"]
        XCTAssertEqual(input.placeholderValue, "Message Grace")
        input.tap()
        input.typeText("Thanks, see you then.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(labeled("Thanks, see you then.").waitForExistence(timeout: 15))
        sleep(2)
        back()

        // The chat is read. A new message of Grace shows as unread, and the count goes when the chat opens.
        XCTAssertTrue(graceRow.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForLabel(graceRow, contains: nil), graceRow.label)
        XCTAssertTrue(graceRow.label.contains("You: Thanks, see you then."), graceRow.label)
        try act(world, "grace-writes", done: "grace-wrote")
        XCTAssertTrue(waitForLabel(graceRow, contains: "1 unread message"), graceRow.label)
        XCTAssertTrue(graceRow.label.contains("One more question about Friday."), graceRow.label)
        shot("dm-chats-unread")
        graceRow.tap()
        XCTAssertTrue(chatTitle.waitForExistence(timeout: 10))
        XCTAssertTrue(labeled("One more question about Friday.").waitForExistence(timeout: 15))
        sleep(2)
        back()
        XCTAssertTrue(waitForLabel(graceRow, contains: nil), "The count goes when the chat opens: \(graceRow.label)")

        // The name of Grace above her message in the team chat.
        app.buttons["team-chat-\(world.room)"].tap()
        let writer = app.buttons["team-writer-\(world.grace)"]
        XCTAssertTrue(writer.waitForExistence(timeout: 15))
        XCTAssertEqual(writer.label, "Message Grace")
        XCTAssertFalse(app.buttons["team-writer-\(world.noor)"].exists)
        writer.tap()
        XCTAssertTrue(chatTitle.waitForExistence(timeout: 10), "The name opens the direct chat.")
        back()
    }

    /// A tap on the banner of a direct message in a terminated app opens the direct chat. COCONUT_DIRECT_NOTIFY turns
    /// it on. A script of the test world pushes the payload of the newest message of Grace when the test writes
    /// `backgrounded`. COCONUT_PUSH_EXPECTED_BODY is its text.
    @MainActor
    func testATapOnADirectNotificationOpensTheDirectChat() throws {
        let world = try DirectWorld()
        let env = ProcessInfo.processInfo.environment
        guard env["COCONUT_DIRECT_NOTIFY"] == "1", let body = env["COCONUT_PUSH_EXPECTED_BODY"],
              let flags = world.flags else {
            throw XCTSkip("Run with the test world of the direct messages and COCONUT_DIRECT_NOTIFY.")
        }
        launchDirect(world)
        allowNotifications()
        XCUIDevice.shared.press(.home)
        app.terminate()
        try Data().write(to: flags.appendingPathComponent("backgrounded"))
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let banner = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", body)).firstMatch
        XCTAssertTrue(banner.waitForExistence(timeout: 45), "The banner must show the text of the message.")
        XCTAssertTrue(banner.label.contains("Grace"), banner.label)
        // The banner slides in. The picture waits until it is at rest.
        sleep(2)
        screen("dm-push-banner")
        banner.tap()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        XCTAssertTrue(app.descendants(matching: .any).matching(identifier: "direct-chat-title").firstMatch.waitForExistence(timeout: 15),
                      "The tap opens the direct chat.")
        XCTAssertFalse(app.buttons["direct-chat-\(world.grace)"].exists, "The chat is open, not the Chats list.")
        shot("dm-push-chat-after-tap")
    }

    /// The pictures of the Team screen, the direct chat, the Chats list and the sentence of the notification settings
    /// in one look. COCONUT_DIRECT_LOOK is "light", "dark" or "large".
    @MainActor
    func testTheDirectChatsInALook() throws {
        let world = try DirectWorld()
        guard let look = ProcessInfo.processInfo.environment["COCONUT_DIRECT_LOOK"] else {
            throw XCTSkip("Run with the test world of the direct messages and COCONUT_DIRECT_LOOK.")
        }
        launchDirect(world, look: look)
        let graceRow = app.buttons["direct-chat-\(world.grace)"]
        XCTAssertTrue(graceRow.waitForExistence(timeout: 15))
        shot("dm-\(look)-chats-list")
        app.buttons["team-chat-\(world.room)"].tap()
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        let grace = app.buttons["team-member-\(world.grace)"]
        XCTAssertTrue(grace.waitForExistence(timeout: 15))
        shot("dm-\(look)-team-screen")
        grace.tap()
        XCTAssertTrue(app.descendants(matching: .any).matching(identifier: "direct-chat-title").firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(labeled("Thanks, see you then.").waitForExistence(timeout: 15))
        shot("dm-\(look)-chat-grace")
        back()
        guard look == "light" else { return }
        app.buttons["app-settings"].tap()
        let row = app.buttons["notification-settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        let summary = app.staticTexts["notification-summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        XCTAssertTrue(summary.label.contains("direct messages") || summary.label.contains("every direct message"),
                      summary.label)
        shot("dm-settings-notifications")
    }

    /// The direct chat after Grace left the team: its history, and one quiet line in the place of the composer.
    /// COCONUT_DIRECT_LEAVE turns it on, because the leave changes the test world for good.
    @MainActor
    func testADirectChatKeepsItsHistoryAfterThePersonLeft() throws {
        let world = try DirectWorld()
        guard ProcessInfo.processInfo.environment["COCONUT_DIRECT_LEAVE"] == "1" else {
            throw XCTSkip("Run with the test world of the direct messages and COCONUT_DIRECT_LEAVE.")
        }
        try act(world, "grace-leaves", done: "grace-left")
        launchDirect(world)
        let graceRow = app.buttons["direct-chat-\(world.grace)"]
        XCTAssertTrue(graceRow.waitForExistence(timeout: 15), "The chat stays in the list.")
        graceRow.tap()
        let closed = app.staticTexts["team-chat-closed"]
        XCTAssertTrue(closed.waitForExistence(timeout: 15))
        XCTAssertEqual(closed.label, "Grace is no longer in this team.")
        XCTAssertTrue(labeled("I will bring the draft.").waitForExistence(timeout: 15), "The history stays.")
        XCTAssertFalse(app.textViews["message-input"].exists || app.textFields["message-input"].exists)
        shot("dm-chat-after-leave")
    }

    /// The test world of a removal: Example Team (COCONUT_TEAM_REMOVE) with Ada, the admin and the person of this app,
    /// the people Grace and Lin, and Spare Mac, a Mac for a team only with the bots Reporter and Scribe.
    /// COCONUT_REMOVE_GRACE is the member ID of Grace. COCONUT_TEAM_LOOK
    /// "dark" or "large" sets the look. Only with COCONUT_REMOVE_CONFIRM=1 does the test remove Grace, because the
    /// removal changes the test world for good.
    @MainActor
    func testTheAdminRemovesAPersonWithTheSwipeAction() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"], let room = env["COCONUT_TEAM_REMOVE"],
              let grace = env["COCONUT_REMOVE_GRACE"] else {
            throw XCTSkip("Run with the test world of a member removal.")
        }
        let look = launchTeamScreen(url: url, code: code, room: room)
        let row = app.buttons["team-member-\(grace)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))

        // A swipe to the left shows the red action, and VoiceOver finds it by its name.
        row.swipeLeft()
        let swipe = app.buttons["team-remove-swipe"]
        XCTAssertTrue(swipe.waitForExistence(timeout: 5))
        XCTAssertEqual(swipe.label, "Remove")
        shot("remove-\(look)-1-swipe")
        swipe.tap()
        let confirm = app.buttons["team-remove-confirm"].firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Remove Grace from Example Team?"].exists)
        XCTAssertTrue(app.staticTexts["Grace can no longer read or write in this team. The pages that Grace shared leave the Library."].exists)
        shot("remove-\(look)-3-confirm-grace")
        cancelQuestion()
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 5))
        XCTAssertTrue(row.exists, "Cancel keeps the row.")
        guard look == "light" else { return }

        // A long press shows the menu: the direct chat, then the removal.
        row.press(forDuration: 1.2)
        let menu = app.buttons["team-remove-menu"]
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
        XCTAssertEqual(menu.label, "Remove from team")
        XCTAssertTrue(app.buttons["Message Grace"].exists)
        shot("remove-\(look)-2-menu-grace")
        menu.tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        cancelQuestion()
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 5))

        // The row of a bot of Spare Mac removes that Mac with its two bots.
        let bot = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'team-bot-' AND label BEGINSWITH 'Reporter'")).firstMatch
        for _ in 0..<4 where !(bot.exists && bot.isHittable) { app.swipeUp(velocity: .slow) }
        bot.press(forDuration: 1.2)
        XCTAssertTrue(menu.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Message Spare Mac"].exists, "A Mac for a team only gets no direct message.")
        menu.tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Remove Spare Mac from Example Team?"].exists)
        XCTAssertTrue(app.staticTexts["Spare Mac can no longer read or write in this team. Its bots Reporter and Scribe leave the team with it."].exists)
        shot("remove-\(look)-3-confirm-spare-mac")
        cancelQuestion()
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 5))
        XCTAssertTrue(bot.exists, "Cancel keeps the bots.")

        guard env["COCONUT_REMOVE_CONFIRM"] == "1" else { return }
        for _ in 0..<4 where !(row.exists && row.isHittable) { app.swipeDown(velocity: .slow) }
        row.swipeLeft()
        XCTAssertTrue(swipe.waitForExistence(timeout: 5))
        swipe.tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        confirm.tap()
        XCTAssertTrue(row.waitForNonExistence(timeout: 30), "The row of Grace goes after the removal.")
        XCTAssertFalse(app.staticTexts["Grace"].exists)
        XCTAssertTrue(app.staticTexts["Lin"].exists)
        shot("remove-\(look)-4-after")
    }

    /// The Team screen of a member that is not the admin, or of the removed person. COCONUT_TEAM_REMOVE names the
    /// team. COCONUT_REMOVE_AS names the picture. With COCONUT_REMOVE_SWIPE=1 the test swipes the row of the bot
    /// Reporter and finds no removal. A bot without the mark `direct` has a plain row: the test also presses it and
    /// finds no menu. A bot with the mark has a row that opens its direct chat, as the row of a person: a swipe on it
    /// counts as a tap. The chat takes the place of the stack (showTeamChat), so Back leads to the Chats list. The test
    /// then opens the Team screen again and finds no removal there.
    @MainActor
    func testATeamScreenWithoutTheRemoval() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"], let room = env["COCONUT_TEAM_REMOVE"],
              let name = env["COCONUT_REMOVE_AS"] else {
            throw XCTSkip("Run with the test world of a member removal and COCONUT_REMOVE_AS.")
        }
        launchTeamScreen(url: url, code: code, room: room, chatShot: "remove-\(name)-chat")
        shot("remove-\(name)-team-screen")
        guard env["COCONUT_REMOVE_SWIPE"] == "1" else { return }
        let bot = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'team-bot-' AND label BEGINSWITH 'Reporter'")).firstMatch
        XCTAssertTrue(bot.waitForExistence(timeout: 15))
        let opensChat = bot.elementType == .button
        bot.swipeLeft()
        XCTAssertFalse(app.buttons["team-remove-swipe"].waitForExistence(timeout: 2), "A member has no removal.")
        shot("remove-\(name)-after-swipe")
        if opensChat {
            let chatTitle = app.descendants(matching: .any).matching(identifier: "direct-chat-title").firstMatch
            if chatTitle.waitForExistence(timeout: 3) {
                app.navigationBars.buttons.element(boundBy: 0).tap()
                let row = app.buttons["team-chat-\(room)"]
                XCTAssertTrue(row.waitForExistence(timeout: 10), "Back leads to the Chats list.")
                row.tap()
                let title = app.buttons["team-chat-title"]
                XCTAssertTrue(title.waitForExistence(timeout: 10))
                title.tap()
            }
            XCTAssertTrue(bot.waitForExistence(timeout: 10))
            XCTAssertFalse(app.buttons["team-remove-swipe"].exists, "A member has no removal.")
        } else {
            bot.press(forDuration: 1.2)
            XCTAssertFalse(app.buttons["team-remove-menu"].waitForExistence(timeout: 2), "A member has no menu.")
        }
        XCTAssertFalse(app.buttons["Remove from team"].exists)
    }

    // MARK: Mentions of a person

    /// The test world of person mentions: Example Team (COCONUT_MENTION_TEAM) with Ada, the person of this app, the
    /// people Grace (COCONUT_MENTION_GRACE) and Lin, and Reporter Mac, a Mac for a team only whose bot Reporter answers
    /// each member. Grace wrote "The weekly report is out." A script of the world acts for Grace: when the test writes
    /// `grace-names-ada` into the folder of COCONUT_TEAM_FLAGS, Grace writes "@Ada can you check the numbers?" with a
    /// mention of Ada, and the script writes `grace-named-ada`. COCONUT_TEAM_LOOK "dark" or "large" sets the look.
    ///
    /// `@` lists the bot, then the people. A pick of Grace names her in the sent message. A message that names Ada puts
    /// the "@" mark on the row of the team, and the bubble shows her name in bold. The mark goes when Ada reads the
    /// chat. The switch of the team, off, says that only a mention notifies.
    @MainActor
    func testAPersonNamesAnotherPersonWithAt() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let room = env["COCONUT_MENTION_TEAM"], let grace = env["COCONUT_MENTION_GRACE"],
              let flags = env["COCONUT_TEAM_FLAGS"].map({ URL(fileURLWithPath: $0) }) else {
            throw XCTSkip("Run with the test world of person mentions.")
        }
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        let look = env["COCONUT_TEAM_LOOK"] ?? "light"
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let row = app.buttons["team-chat-\(room)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        // The chat opens at its newest message. A run before can leave the first message above the screen.
        XCTAssertTrue(element("team-chat-transcript").waitForExistence(timeout: 15))

        // @ lists the bot that answers each member, then the people. Ada and Reporter Mac are no rows.
        let bots = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'mention-bot-'"))
        let people = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'mention-person-'"))
        let field = element("message-input")
        field.tap()
        field.typeText("@")
        XCTAssertTrue(element("bot-mention-picker").waitForExistence(timeout: 5))
        XCTAssertEqual(bots.count, 1, "Reporter")
        XCTAssertEqual(people.count, 2, "Grace and Lin")
        let graceRow = app.buttons["mention-person-\(grace)"]
        XCTAssertEqual(graceRow.label, "Grace")
        XCTAssertLessThan(bots.firstMatch.frame.minY, graceRow.frame.minY, "The bot comes first.")
        XCTAssertLessThan(graceRow.frame.minY, app.buttons["Lin"].frame.minY, "The people in the order of the names.")
        XCTAssertFalse(app.buttons["Ada"].exists, "This person is not in the list.")
        XCTAssertFalse(app.buttons["Reporter Mac"].exists, "A Mac for a team only is not in the list.")
        shot("mention-\(look)-1-list")

        // The letters filter, and a pick names Grace.
        field.typeText("g")
        XCTAssertTrue(app.buttons["Reporter"].waitForNonExistence(timeout: 5))
        XCTAssertEqual(people.count, 1)
        if look == "light" { shot("mention-\(look)-2-list-after-g") }
        graceRow.tap()
        XCTAssertTrue(element("bot-mention-picker").waitForNonExistence(timeout: 5))
        field.typeText("the report is yours.")
        XCTAssertEqual(field.value as? String, "@Grace the report is yours.")
        app.buttons["send-message"].tap()
        XCTAssertTrue(text("@Grace the report is yours.").waitForExistence(timeout: 15))
        sleep(2)
        back()

        // Grace names Ada. The row of the team shows the mark.
        try? FileManager.default.removeItem(at: flags.appendingPathComponent("grace-named-ada"))
        try Data().write(to: flags.appendingPathComponent("grace-names-ada"))
        let named = flags.appendingPathComponent("grace-named-ada").path
        for _ in 0..<60 where !FileManager.default.fileExists(atPath: named) { sleep(1) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: named), "Grace wrote the message.")
        XCTAssertTrue(waitForLabel(row, contains: "You are named"), row.label)
        shot("mention-\(look)-4-chats-mark")

        // The bubble shows the name of Ada in bold, and VoiceOver says so.
        row.tap()
        let received = text("@Ada can you check the numbers?")
        XCTAssertTrue(received.waitForExistence(timeout: 15))
        XCTAssertTrue(received.label.contains("Names you"), received.label)
        XCTAssertTrue(text("@Grace the report is yours.").exists)
        shot("mention-\(look)-3-chat")
        sleep(2)
        back()
        let read = XCTNSPredicateExpectation(predicate: NSPredicate(format: "NOT (label CONTAINS 'You are named')"),
                                             object: row)
        XCTAssertEqual(XCTWaiter().wait(for: [read], timeout: 30), .completed, "The mark goes after the read: \(row.label)")
        guard look == "light" else { return }

        // The switch off says that only a mention notifies.
        let toggle = try openTeamScreen(room)
        if toggle.value as? String == "1" {
            toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
            XCTAssertTrue(waitForValue(toggle, "0"))
        }
        let footer = element("team-notifications-footer")
        XCTAssertTrue(footer.waitForExistence(timeout: 5))
        XCTAssertEqual(footer.label, "You get a notification only when someone names you with @.")
        shot("mention-\(look)-5-switch-off")
    }

    /// The test world of a removal, with a message of Grace and a message of the bot Reporter in the chat of Example
    /// Team (COCONUT_TEAM_ERASE). As Ada, the admin, the test opens the menu of each message and finds Delete, and asks
    /// the question of a delete. COCONUT_TEAM_LOOK "dark" or "large" sets the look, and stops after the question. In
    /// the light look the test deletes the message of Grace, and it goes from the chat. With COCONUT_ERASE_AS, the
    /// test runs as a person who is not the admin, and the message of Grace has no Delete.
    @MainActor
    func testTheAdminDeletesAMessageOfAnotherPerson() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_TEAM_URL"], let code = env["COCONUT_TEAM_CODE"],
              let room = env["COCONUT_TEAM_ERASE"] else {
            throw XCTSkip("Run with the test world of a member removal and COCONUT_TEAM_ERASE.")
        }
        let look = launchTeamChat(url: url, code: code, room: room)
        let grace = text("The weekly report is out.")
        XCTAssertTrue(grace.waitForExistence(timeout: 15))
        hold(grace)
        XCTAssertTrue(app.buttons["held-reply"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["held-edit"].exists, "Only the writer edits a message.")
        if let name = env["COCONUT_ERASE_AS"] {
            XCTAssertFalse(app.buttons["held-delete"].exists, "A person who is not the admin has no Delete.")
            shot("erase-\(name)-menu-grace")
            return
        }
        XCTAssertTrue(app.buttons["held-delete"].exists)
        shot("erase-\(look)-1-menu-grace")
        app.buttons["held-delete"].tap()
        let confirm = app.buttons["team-delete-confirm"].firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Delete this message for everyone?"].exists)
        shot("erase-\(look)-2-confirm")
        guard look == "light" else { return }
        confirm.tap()
        XCTAssertTrue(grace.waitForNonExistence(timeout: 15), "The message goes at once.")
        let reporter = text("The numbers of the week are ready.")
        XCTAssertTrue(reporter.exists)
        shot("erase-\(look)-3-after")
        // A message of a bot of the team has Delete too.
        hold(reporter)
        XCTAssertTrue(app.buttons["held-delete"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["held-edit"].exists)
        shot("erase-\(look)-4-menu-reporter")
    }

    /// Opens the chat of a team in the look of COCONUT_TEAM_LOOK. Return the look.
    @MainActor
    private func launchTeamChat(url: String, code: String, room: String) -> String {
        let env = ProcessInfo.processInfo.environment
        shots = env["COCONUT_TEAM_SHOTS"].map { URL(fileURLWithPath: $0) }
        let look = env["COCONUT_TEAM_LOOK"] ?? "light"
        continueAfterFailure = false
        app = XCUIApplication()
        if look == "dark" { app.launchArguments += ["-AppleInterfaceStyle", "Dark", "-UIUserInterfaceStyle", "Dark"] }
        if look == "large" {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        }
        app.launchEnvironment = Self.pairing(url: url, code: code)
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        let row = app.buttons["team-chat-\(room)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        return look
    }

    /// Closes a question without its action. An iPhone shows it as a popover without a Cancel button, so a tap beside
    /// it cancels it.
    private func cancelQuestion() {
        if app.buttons["Cancel"].exists { app.buttons["Cancel"].tap() } else {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.6)).tap()
        }
    }

    /// Opens the Team screen of a team from its chat, in the look of COCONUT_TEAM_LOOK. Return the look.
    @MainActor @discardableResult
    private func launchTeamScreen(url: String, code: String, room: String, chatShot: String? = nil) -> String {
        let look = launchTeamChat(url: url, code: code, room: room)
        let title = app.buttons["team-chat-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        if let chatShot { shot(chatShot) }
        title.tap()
        return look
    }

    /// The Team screen of a team, from its chat. Return the switch "Notifications".
    private func openTeamScreen(_ room: String) throws -> XCUIElement {
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Chats"].tap()
        // The app can open again on the chat that it showed last, for example after a tap on a notification.
        let row = app.buttons["team-chat-\(room)"]
        let title = app.buttons["team-chat-title"]
        if !title.waitForExistence(timeout: 3) {
            XCTAssertTrue(row.waitForExistence(timeout: 15))
            row.tap()
        }
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        let toggle = app.switches["team-notifications"]
        // With the largest text, the switch and its footer are below the first screen.
        let footer = element("team-notifications-footer")
        for _ in 0..<6 where !(toggle.exists && toggle.isHittable && footer.exists) { app.swipeUp(velocity: .slow) }
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        return toggle
    }

    private func waitForValue(_ element: XCUIElement, _ value: String) -> Bool {
        let expectation = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", value), object: element)
        return XCTWaiter().wait(for: [expectation], timeout: 10) == .completed
    }

    /// Allows notifications in Settings, as a person does. The services of the test world have no push relay, so the
    /// app registers with no relay.
    private func allowNotifications() {
        app.buttons["app-settings"].tap()
        let row = app.buttons["notification-settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        // The button is the last row, and the list makes a row only when it comes on screen.
        let enable = app.buttons["Enable notifications"]
        let allowed = app.buttons["iOS notification settings"]
        for _ in 0..<6 where !enable.exists && !allowed.exists { app.swipeUp(velocity: .slow) }
        if enable.waitForExistence(timeout: 5) {
            enable.tap()
            let allow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Allow"]
            if allow.waitForExistence(timeout: 10) { allow.tap() }
        }
        XCTAssertTrue(allowed.waitForExistence(timeout: 10), "Notifications are allowed.")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["close-settings"].tap()
    }

    /// A screenshot of the whole screen, for a page of the system such as the Home Screen with a banner.
    private func screen(_ name: String) {
        guard let shots else { return }
        let image = XCUIScreen.main.screenshot()
        try? image.pngRepresentation.write(to: shots.appendingPathComponent(name + ".png"))
        let attachment = XCTAttachment(screenshot: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// The pairing codes that a test of this run already gave to the app.
    @MainActor private static var usedCodes: Set<String> = []

    /// The launch environment of the app for the service at `url`. A pairing code works one time: the Mac refuses it
    /// at a second pairing, and the app then stays disconnected. All tests of one run share one code and one app,
    /// which keeps its pairing, so only the first launch of the run gets the code.
    @MainActor private static func pairing(url: String, code: String) -> [String: String] {
        guard usedCodes.insert(code).inserted else { return ["COCONUT_SERVER_URL": url] }
        return ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
    }

    private func back() {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.tabBars.buttons["Chats"].waitForExistence(timeout: 10))
    }

    private func shot(_ name: String, settle: Bool = true) {
        guard let shots else { return }
        if settle { sleep(1) }
        // COCONUT_TEAM_SHOT_SUFFIX keeps the screenshots of an earlier run.
        let suffix = ProcessInfo.processInfo.environment["COCONUT_TEAM_SHOT_SUFFIX"] ?? ""
        try? app.screenshot().pngRepresentation.write(to: shots.appendingPathComponent(name + suffix + ".png"))
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
