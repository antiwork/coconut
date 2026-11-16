import XCTest

extension XCUIApplication {
    /// Opens the cards that wait: the Home tab, then "Review cards" of "Unread". The cards show in their recommended
    /// order, as the Inbox tab showed them. With the cards open already, or with no card, the app stays where it is.
    @MainActor func openCards(timeout: TimeInterval = 15) {
        let home = tabBars.buttons["Home"]
        XCTAssertTrue(home.waitForExistence(timeout: timeout), "The Home tab is missing.")
        home.tap()
        let review = buttons["home-review-cards"]
        if review.waitForExistence(timeout: 3) { review.tap() }
    }

    /// Opens the list of every page of every agent: the Agents tab, then "All pages". It is the list that the Library
    /// tab showed.
    func openAllPages(timeout: TimeInterval = 15) {
        let agents = tabBars.buttons["Agents"]
        XCTAssertTrue(agents.waitForExistence(timeout: timeout), "The Agents tab is missing.")
        agents.tap()
        let pages = buttons["agents-all-pages"]
        // With the list open already, the tab keeps it.
        if pages.waitForExistence(timeout: 3) { pages.tap() }
    }
}
