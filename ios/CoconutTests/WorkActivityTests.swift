import XCTest
@testable import Coconut

final class WorkActivityTests: XCTestCase {
    private func artifact(id: String = "a") -> Artifact {
        Artifact(id: id, botId: "bot", title: "Review", kind: "pr", status: "merged", preview: "",
                 url: "https://github.com/team/repo/pull/42", createdAt: "2026-09-13T00:00:00Z", prState: "merged")
    }

    func testADismissalOnTheMacHidesAllCopies() {
        var dismissed = artifact()
        dismissed.workDismissalId = "action"
        let copies = [dismissed, artifact(id: "b")]
        XCTAssertTrue(TrackedPullRequest.tracked(in: copies).isEmpty)
    }

}
