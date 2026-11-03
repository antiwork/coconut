import XCTest
@testable import Coconut

final class WorkTests: XCTestCase {
    private func artifact() -> Artifact {
        Artifact(id: "a", botId: "bot-a", title: "Fix retry", kind: "pr", status: "preparing", preview: "",
                 url: "https://github.com/team/repo/pull/42", createdAt: "2026-09-13T00:00:00Z",
                 observedAt: "2026-09-13T00:00:00Z", prState: "open", draft: true, checksState: "running")
    }

    func testChatPRCardExpiresWithoutAnotherServerEvent() throws {
        var draft = artifact()
        draft.preview = "The pull request is a draft."
        let observed = try XCTUnwrap(draft.observedAt?.apiDate)
        XCTAssertEqual(draft.cardStatus(at: observed), "preparing")
        XCTAssertEqual(draft.cardPreview(at: observed), draft.preview)
        let later = observed.addingTimeInterval(121)
        XCTAssertEqual(draft.cardStatus(at: later), "status_out_of_date")
        XCTAssertFalse(draft.cardPreview(at: later).contains("is a draft"))
        draft.refreshError = "GitHub unavailable"
        XCTAssertEqual(draft.cardStatus(at: observed), "status_out_of_date")
        draft.refreshError = nil
        draft.observedAt = nil
        XCTAssertEqual(draft.cardStatus(at: observed), "status_out_of_date")
        draft.prState = "merged"
        XCTAssertEqual(draft.cardStatus(at: later), "merged")
        XCTAssertEqual(draft.cardPreview(at: later), "GitHub confirms this pull request merged.")
        draft.prState = "closed"
        XCTAssertEqual(draft.cardStatus(at: later), "closed")
        draft.kind = "task"
        XCTAssertEqual(draft.cardStatus(at: later), "preparing")
        XCTAssertEqual(draft.cardPreview(at: later), draft.preview)
    }
}
