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
}
