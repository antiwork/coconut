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
