import XCTest

/// A person shares a page with a team against real Coconut services: one act on the page, a quiet line near its title,
/// and one act to stop. The test world has Ada's Mac with the bot Reporter and its page "Weekly report", and Example
/// Team of Ada and Grace. COCONUT_SHARE_TEAM names the room of Example Team. With COCONUT_SHARE_SHOTS, the test saves a
/// screenshot of each state in that folder.
final class PageShareFlowTests: XCTestCase {
    private var app: XCUIApplication!
    private var shots: URL?

    /// Ada's Mac, with the page private: share it, see the line, and stop.
    @MainActor
    func testShareAPageWithTheTeamAndStop() throws {
        let env = ProcessInfo.processInfo.environment
        guard let team = env["COCONUT_SHARE_TEAM"] else { throw XCTSkip("Run with the test world of a page share.") }
        try launch()
        openPage("weekly-report")
        let share = app.buttons["document-share-\(team)"]
        openActions()
        XCTAssertTrue(share.waitForExistence(timeout: 10))
        XCTAssertEqual(share.label, "Share with Example Team")
        shot("01-iphone-page-menu-share")
        share.tap()
        let line = app.webViews.staticTexts["Shared with Example Team"]
        XCTAssertTrue(line.waitForExistence(timeout: 15), "The page shows that the team has it.")
        shot("02a-iphone-page-shared-line")
        openActions()
        XCTAssertTrue(share.waitForExistence(timeout: 10))
        XCTAssertEqual(share.label, "Stop sharing with Example Team")
        shot("02b-iphone-page-menu-stop")
        share.tap()
        XCTAssertTrue(line.waitForNonExistence(timeout: 15), "The line goes with the stop.")
        openActions()
        XCTAssertTrue(share.waitForExistence(timeout: 10))
        XCTAssertEqual(share.label, "Share with Example Team")
    }

    /// The screens of a world whose state a script set before the run. COCONUT_SHARE_LOOK names them:
    /// - "shared": Ada's page is shared. The Library list, the page with its line and its menu, and the Team screen.
    /// - "dark": the same page and Team screen. The script of the run sets the simulator to dark mode first.
    /// - "grace": the app is paired with Grace's Mac. Her Library has the page under the bot of Ada.
    /// - "two": Ada's Mac is in two teams. The menu of the page has an item for each.
    @MainActor
    func testTheSharedPageInALook() throws {
        let env = ProcessInfo.processInfo.environment
        guard let look = env["COCONUT_SHARE_LOOK"], let team = env["COCONUT_SHARE_TEAM"] else {
            throw XCTSkip("Run with the test world of a page share and COCONUT_SHARE_LOOK.")
        }
        try launch()
        switch look {
        case "grace":
            app.tabBars.buttons["Library"].tap()
            let page = app.buttons["library-document-weekly-report"]
            XCTAssertTrue(page.waitForExistence(timeout: 15))
            XCTAssertTrue(app.staticTexts["Reporter"].exists)
            XCTAssertTrue(app.staticTexts["Example Team"].exists, "The group of a team bot names its team.")
            shot("04-iphone-grace-library-page-under-reporter-of-ada")
            page.tap()
            XCTAssertTrue(app.webViews.staticTexts["Weekly report"].waitForExistence(timeout: 15))
            openActions()
            XCTAssertTrue(app.buttons["document-pin"].waitForExistence(timeout: 10))
            XCTAssertFalse(app.buttons["document-share-\(team)"].exists, "A team page of another Mac has no item.")
            shot("04b-iphone-grace-team-page-menu")
        case "two":
            openPage("weekly-report")
            openActions()
            let items = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'document-share-'"))
            XCTAssertTrue(items.firstMatch.waitForExistence(timeout: 10))
            XCTAssertEqual(items.count, 2)
            XCTAssertEqual(items.allElementsBoundByIndex.map(\.label),
                           ["Share with Design Crew", "Stop sharing with Example Team"])
            shot("06-iphone-menu-two-teams")
        default:
            let suffix = look == "dark" ? "-dark" : ""
            app.tabBars.buttons["Library"].tap()
            XCTAssertTrue(app.buttons["library-document-weekly-report"].waitForExistence(timeout: 15))
            XCTAssertTrue(app.buttons["library-document-draft-notes"].exists)
            if look == "shared" { shot("03-iphone-library-shared-and-private") }
            openPage("weekly-report")
            XCTAssertTrue(app.webViews.staticTexts["Shared with Example Team"].waitForExistence(timeout: 15))
            shot("07a-iphone-page-shared-line\(suffix)")
            openActions()
            XCTAssertTrue(app.buttons["document-share-\(team)"].waitForExistence(timeout: 10))
            shot("07b-iphone-page-menu-stop\(suffix)")
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9)).tap()
            XCTAssertTrue(app.buttons["document-share-\(team)"].waitForNonExistence(timeout: 5))
            app.navigationBars.buttons.element(boundBy: 0).tap()
            // The Team screen opens from the title of the Team chat.
            app.tabBars.buttons["Chats"].tap()
            let row = app.buttons["team-chat-\(team)"]
            XCTAssertTrue(row.waitForExistence(timeout: 15))
            row.tap()
            app.buttons["team-chat-title"].tap()
            XCTAssertTrue(app.buttons["team-leave"].waitForExistence(timeout: 10))
            XCTAssertFalse(app.staticTexts["Agents"].exists, "A team without a bot to ask has no Agents section.")
            XCTAssertFalse(app.staticTexts["Add one of your bots"].exists)
            shot("05-iphone-team-screen-without-bots\(suffix)")
        }
    }

    // MARK: Steps

    private func launch() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_SHARE_URL"], let code = env["COCONUT_SHARE_CODE"] else {
            throw XCTSkip("Run with the test world of a page share.")
        }
        shots = env["COCONUT_SHARE_SHOTS"].map { URL(fileURLWithPath: $0) }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Library"].waitForExistence(timeout: 20))
    }

    private func openPage(_ key: String) {
        app.tabBars.buttons["Library"].tap()
        let row = app.buttons["library-document-\(key)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        XCTAssertTrue(app.webViews.staticTexts["Weekly report"].waitForExistence(timeout: 15))
    }

    private func openActions() {
        let actions = app.buttons["Page actions"]
        XCTAssertTrue(actions.waitForExistence(timeout: 10))
        actions.tap()
    }

    private func shot(_ name: String) {
        guard let shots else { return }
        sleep(1)
        try? app.screenshot().pngRepresentation.write(to: shots.appendingPathComponent(name + ".png"))
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

/// "Chat about this page" on a page that a team agent shares, against real Coconut services. The test world has the
/// person's Mac in a team with a Mac for a team only, whose agent answers direct messages (`direct`) and shares the page
/// "Sales" (key `sales`). COCONUT_AGENT_BOT is the team bot ID of the agent. The world reads one command from the file
/// COCONUT_AGENT_COMMANDS: "silent" takes the mark `direct` from the agent, and "back" gives it back.
final class AgentPageChatFlowTests: XCTestCase {
    private var app: XCUIApplication!
    private var preview: XCUIElement { app.descendants(matching: .any).matching(identifier: "reply-preview").firstMatch }
    private var field: XCUIElement { app.descendants(matching: .any).matching(identifier: "message-input").firstMatch }
    /// The quotes of the page "Sales" in the messages of the chat.
    private var quotes: XCUIElementQuery { app.buttons.matching(NSPredicate(format: "label == %@", "Open the page Sales")) }

    /// The item, the quote in the chat of the agent only, the cross, the send, the quote in the message, a tap on the
    /// quote, and the closed line that takes the place of the quote.
    @MainActor
    func testChatAboutAPageOfATeamAgent() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["COCONUT_AGENT_URL"], let code = env["COCONUT_AGENT_CODE"], let bot = env["COCONUT_AGENT_BOT"],
              let commands = env["COCONUT_AGENT_COMMANDS"] else {
            throw XCTSkip("Run with the test world of a direct chat with an agent.")
        }
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Library"].waitForExistence(timeout: 30))

        // The item opens the chat of the agent with the page above the text field.
        openPage()
        ask()
        XCTAssertTrue(app.staticTexts["Sales"].exists, "The quote names the page.")
        XCTAssertTrue(app.buttons["Remove the page"].exists)

        // The page went with the chat: the chat opens again without it.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let row = app.buttons["bot-direct-chat-\(bot)"]
        XCTAssertTrue(row.waitForExistence(timeout: 15))
        row.tap()
        XCTAssertTrue(field.waitForExistence(timeout: 15))
        XCTAssertFalse(preview.waitForExistence(timeout: 3), "A new open of the chat shows no page.")

        // The cross removes the page.
        toLibrary()
        ask()
        app.buttons["Remove the page"].tap()
        XCTAssertTrue(preview.waitForNonExistence(timeout: 5))

        // The send: the new message shows the page as a quote, and the composer is empty again.
        toLibrary()
        ask()
        let earlier = quotes.count
        field.tap()
        field.typeText("Why are the refunds higher this week?")
        app.buttons["Send message"].tap()
        XCTAssertTrue(preview.waitForNonExistence(timeout: 10))
        let quote = quotes.element(boundBy: earlier)
        XCTAssertTrue(quote.waitForExistence(timeout: 15), "The new message shows its page.")
        XCTAssertEqual(quotes.count, earlier + 1)

        // A tap on the quote opens the page.
        quote.tap()
        XCTAssertTrue(app.buttons["Page actions"].waitForExistence(timeout: 15), "The quote opens the page.")

        // The agent stops to answer while the page waits: the closed line takes the place of the composer, and the page
        // goes. When the agent answers again, the composer comes back without the page.
        ask()
        try "silent".write(toFile: commands, atomically: true, encoding: .utf8)
        let closed = app.descendants(matching: .any)["team-chat-closed"]
        XCTAssertTrue(closed.waitForExistence(timeout: 60))
        try "back".write(toFile: commands, atomically: true, encoding: .utf8)
        XCTAssertTrue(closed.waitForNonExistence(timeout: 60))
        XCTAssertTrue(field.waitForExistence(timeout: 10), "The composer comes back.")
        XCTAssertFalse(preview.waitForExistence(timeout: 3), "The page went with the closed line.")
    }

    /// The page "Sales" in the Library.
    private func openPage() {
        app.tabBars.buttons["Library"].tap()
        let page = app.buttons["library-document-sales"]
        XCTAssertTrue(page.waitForExistence(timeout: 15))
        page.tap()
    }

    /// Back from the chat to the Chats list, where the tab bar shows, and then the Library tab with its open page.
    private func toLibrary() {
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let library = app.tabBars.buttons["Library"]
        XCTAssertTrue(library.waitForExistence(timeout: 10))
        library.tap()
    }

    /// "Chat about this page" on the open page.
    private func ask() {
        let actions = app.buttons["Page actions"]
        XCTAssertTrue(actions.waitForExistence(timeout: 15))
        actions.tap()
        let item = app.buttons["document-ask"]
        XCTAssertTrue(item.waitForExistence(timeout: 10), "A page of an agent with the mark has the item.")
        item.tap()
        XCTAssertTrue(preview.waitForExistence(timeout: 15), "The chat shows the page above the text field.")
    }
}
