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
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
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
        // A message of another member has no Edit and no Delete.
        hold(text("I booked a table for four."))
        XCTAssertTrue(app.buttons["held-reply"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["held-edit"].exists)
        XCTAssertFalse(app.buttons["held-delete"].exists)
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
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
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
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
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
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
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
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
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
        let footer = app.staticTexts["Get notified when someone writes in this team."]
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
