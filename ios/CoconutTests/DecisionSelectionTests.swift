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

    func testOnlyAChoiceOfAPersonShowsTheCheck() {
        for status in ["approved", "executed", "rejected", "modified", "replied"] {
            XCTAssertTrue(DecisionSelection.showsCheck(status: status), status)
            XCTAssertFalse(DecisionSelection.closedWithoutChoice(status: status, dismissed: false), status)
        }
        for status in ["resolved", "cancelled", "a-later-status"] {
            XCTAssertFalse(DecisionSelection.showsCheck(status: status), status)
            XCTAssertTrue(DecisionSelection.closedWithoutChoice(status: status, dismissed: false), status)
        }
        // A pending card, a failed card and a dismissed card keep their own marks.
        for status in ["pending", "failed", "dismissed"] {
            XCTAssertFalse(DecisionSelection.showsCheck(status: status), status)
        }
        XCTAssertFalse(DecisionSelection.closedWithoutChoice(status: "pending", dismissed: false))
        XCTAssertFalse(DecisionSelection.closedWithoutChoice(status: "failed", dismissed: false))
        XCTAssertFalse(DecisionSelection.closedWithoutChoice(status: "dismissed", dismissed: true))
        XCTAssertFalse(DecisionSelection.closedWithoutChoice(status: "resolved", dismissed: true))
    }

    func testADismissalAndAChoiceInFlightKeepTheirWords() {
        XCTAssertEqual(label("resolved", draft: true, dismissed: true, outcome: "Issue #12 closed on GitHub."), "Dismissed")
        XCTAssertEqual(label("pending", draft: false, choosing: "Install", selected: "Leave"), "Install")
        XCTAssertEqual(label("approved", draft: false, selected: "Install"), "Install")
    }
}
