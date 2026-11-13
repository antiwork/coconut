import XCTest
@testable import Coconut

final class DecisionSelectionTests: XCTestCase {
    private func label(_ status: String, draft: Bool, dismissed: Bool = false, choosing: String? = nil,
                       selected: String? = nil, outcome: String? = nil) -> String {
        DecisionSelection.label(status: status, hasDraft: draft, dismissed: dismissed, choosingLabel: choosing,
                                selectedOptionLabel: selected, outcome: outcome)
    }

    func testADraftSaysApprovedOnlyWhenThePersonApprovedIt() {
        XCTAssertEqual(label("approved", draft: true), "Approved for sending")
        XCTAssertEqual(label("executed", draft: true), "Approved for sending")
        XCTAssertEqual(label("rejected", draft: true), "Discarded")
        XCTAssertEqual(label("failed", draft: true, outcome: "Hermes could not complete this action."), "Could not finish")
    }

    func testAClosedDraftShowsItsOutcomeOrClosed() {
        XCTAssertEqual(label("resolved", draft: true, outcome: "Issue #12 closed on GitHub."), "Issue #12 closed on GitHub.")
        for status in ["resolved", "replied", "cancelled", "a-later-status"] {
            XCTAssertEqual(label(status, draft: true), "Closed", status)
            XCTAssertEqual(label(status, draft: true, outcome: ""), "Closed", status)
        }
        XCTAssertEqual(label("replied", draft: true, outcome: "Reply received. The bot will continue."),
                       "Reply received. The bot will continue.")
    }

    func testAResolvedCardWithoutAChoiceShowsItsOutcome() {
        XCTAssertEqual(label("resolved", draft: false, outcome: "Pull request #14 merged on GitHub."),
                       "Pull request #14 merged on GitHub.")
        XCTAssertEqual(label("resolved", draft: false, selected: "Merge #14", outcome: "Already merged."), "Merge #14")
        XCTAssertEqual(label("resolved", draft: false), "Resolved")
        XCTAssertEqual(label("approved", draft: false, outcome: "Answer recorded. Hermes will continue."), "Approved")
    }

    func testADismissalAndAChoiceInFlightKeepTheirWords() {
        XCTAssertEqual(label("resolved", draft: true, dismissed: true, outcome: "Issue #12 closed on GitHub."), "Dismissed")
        XCTAssertEqual(label("pending", draft: false, choosing: "Install", selected: "Leave"), "Install")
        XCTAssertEqual(label("approved", draft: false, selected: "Install"), "Install")
    }
}
