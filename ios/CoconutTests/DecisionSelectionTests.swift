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

    func testTheRowShowsOnlyAShortOneLineOutcome() {
        let eighty = String(repeating: "a", count: 80)
        XCTAssertEqual(label("resolved", draft: false, outcome: "Issue #12 closed on GitHub."), "Issue #12 closed on GitHub.")
        XCTAssertEqual(label("resolved", draft: false, outcome: eighty), eighty)
        XCTAssertEqual(label("resolved", draft: false, outcome: eighty + "a"), "Closed")
        XCTAssertEqual(label("resolved", draft: false, outcome: "Read the lock file.\nThe dependency is there."), "Closed")
        XCTAssertEqual(label("resolved", draft: false, outcome: ""), "Resolved")
        XCTAssertEqual(label("resolved", draft: true, outcome: eighty + "a"), "Closed")
        XCTAssertEqual(label("resolved", draft: true, outcome: "Two\nlines"), "Closed")
    }

    func testTheMarkOfEachStatus() {
        func mark(_ status: String, pending: Bool = false, dismissed: Bool = false) -> String? {
            DecisionSelection.mark(status: status, pending: pending, dismissed: dismissed)
        }
        for status in ["approved", "executed", "rejected", "modified", "replied"] {
            XCTAssertEqual(mark(status), "checkmark", status)
        }
        for status in ["resolved", "cancelled", "a-later-status"] {
            XCTAssertNil(mark(status), status)
        }
        XCTAssertEqual(mark("failed"), "exclamationmark.circle")
        XCTAssertEqual(mark("pending", pending: true), "arrow.up")
        XCTAssertEqual(mark("dismissed", dismissed: true), "xmark")
        XCTAssertEqual(mark("pending", pending: true, dismissed: true), "xmark")
    }

    func testACardThatTheServiceDecidedShowsNoCheck() {
        for decider in ["cancellation", "archive", "workflow_permission"] {
            XCTAssertFalse(DecisionSelection.showsCheck(status: "rejected", decidedBy: decider), decider)
            XCTAssertTrue(DecisionSelection.closedWithoutChoice(status: "rejected", dismissed: false, decidedBy: decider), decider)
            XCTAssertNil(DecisionSelection.mark(status: "rejected", pending: false, dismissed: false, decidedBy: decider), decider)
        }
        // A task cancellation rejects its cards with the outcome "Task cancelled.". A draft does not say "Discarded".
        XCTAssertEqual(DecisionSelection.label(status: "rejected", hasDraft: false, dismissed: false, choosingLabel: nil,
                                               selectedOptionLabel: nil, outcome: "Task cancelled.", decidedBy: "cancellation"),
                       "Task cancelled.")
        XCTAssertEqual(DecisionSelection.label(status: "rejected", hasDraft: true, dismissed: false, choosingLabel: nil,
                                               selectedOptionLabel: nil, outcome: nil, decidedBy: "cancellation"), "Closed")
        // A person on a paired device or on this Mac chose.
        for decider in ["device-1", "local"] {
            XCTAssertTrue(DecisionSelection.showsCheck(status: "rejected", decidedBy: decider), decider)
            XCTAssertEqual(DecisionSelection.mark(status: "rejected", pending: false, dismissed: false, decidedBy: decider),
                           "checkmark", decider)
        }
        XCTAssertEqual(DecisionSelection.label(status: "rejected", hasDraft: true, dismissed: false, choosingLabel: nil,
                                               selectedOptionLabel: nil, outcome: "Draft discarded. Nothing was sent.",
                                               decidedBy: "device-1"), "Discarded")
    }

    private func card(status: String, decidedBy: String?, outcome: String?, draft: Bool = false) -> Decision {
        var decision = Decision(id: "card", botId: "bot", taskId: "task", title: "Install the test dependencies?",
                                explanation: "", actionType: "question", resource: "", consequences: "",
                                recommended: "approve", options: [], status: status, urgency: "normal",
                                createdAt: "2026-11-13T12:00:00Z", decidedAt: nil, decidedBy: decidedBy, correction: nil,
                                outcome: outcome, revision: 2)
        if draft {
            decision.draft = MessageDraft(channel: "Email", recipient: "ada@example.com", body: "The refund page is live.")
        }
        return decision
    }

    func testTheReceiptOfACardPassesItsDecider() {
        let cancelled = card(status: "rejected", decidedBy: "cancellation", outcome: "Task cancelled.")
        XCTAssertEqual(DecisionSelection.receipt(cancelled, dismissed: false),
                       DecisionSelection.Receipt(label: "Task cancelled.", mark: nil, dimmed: true))
        let draft = card(status: "rejected", decidedBy: "cancellation", outcome: nil, draft: true)
        XCTAssertEqual(DecisionSelection.receipt(draft, dismissed: false),
                       DecisionSelection.Receipt(label: "Closed", mark: nil, dimmed: true))
        let declined = card(status: "rejected", decidedBy: "device-1", outcome: "Answer recorded. Hermes will continue.")
        XCTAssertEqual(DecisionSelection.receipt(declined, dismissed: false),
                       DecisionSelection.Receipt(label: "Rejected", mark: "checkmark", dimmed: false))
        let closed = card(status: "resolved", decidedBy: nil, outcome: "Issue #12 closed on GitHub.", draft: true)
        XCTAssertEqual(DecisionSelection.receipt(closed, dismissed: false),
                       DecisionSelection.Receipt(label: "Issue #12 closed on GitHub.", mark: nil, dimmed: true))
    }

    func testADismissalAndAChoiceInFlightKeepTheirWords() {
        XCTAssertEqual(label("resolved", draft: true, dismissed: true, outcome: "Issue #12 closed on GitHub."), "Dismissed")
        XCTAssertEqual(label("pending", draft: false, choosing: "Install", selected: "Leave"), "Install")
        XCTAssertEqual(label("approved", draft: false, selected: "Install"), "Install")
    }
}
