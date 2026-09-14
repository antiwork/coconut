import XCTest
@testable import Coconut

final class DecisionFollowupTests: XCTestCase {
    func testEvidenceLimitRemainsVisibleWithContext() throws {
        var decision = try XCTUnwrap(PreviewStateFixture.load().decisions.first)
        decision.context = "The summary supports this scope."
        decision.uncertainty = "The original ruling remains unavailable."
        XCTAssertEqual(decision.cardContext, "The summary supports this scope.\n\nThe original ruling remains unavailable.")
        decision.context = "The original ruling remains unavailable."
        XCTAssertEqual(decision.cardContext, decision.context)
        decision.context = nil
        decision.explanation = ""
        XCTAssertEqual(decision.cardContext, decision.uncertainty)
    }
}
