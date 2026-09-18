import XCTest
@testable import Coconut

final class DecisionSourceTests: XCTestCase {
    func testSourceDecodesAndPreservesAnExactThreadLink() throws {
        let source = try APIJSON.decoder().decode(DecisionSource.self, from: Data(
            #"{"kind":"email","title":"Weekly failures","url":"https://mail.google.com/mail/u/0/#inbox/example"}"#.utf8))
        XCTAssertEqual(source.label, "Email")
        XCTAssertEqual(source.link?.absoluteString, "https://mail.google.com/mail/u/0/#inbox/example")
    }

    func testMissingAndUnsafeLinksRemainPlainSourceLabels() {
        for url in [nil, "file:///etc/passwd", "javascript:alert(1)", "https://", "https://name:secret@example.com"] as [String?] {
            let source = DecisionSource(kind: "email", title: "Weekly failures", url: url)
            XCTAssertNil(source.link)
            XCTAssertEqual(source.label, "Email")
        }
    }

    func testOldCardsStillDecodeAndExposeTheirRecordedMergeSource() throws {
        let fixture = try PreviewStateFixture.load(bundle: Bundle(for: AppStore.self))
        var card = try XCTUnwrap(fixture.decisions.first)
        XCTAssertTrue(card.cardSources.isEmpty)
        card.mergePrUrl = "https://github.com/example/project/pull/42"
        XCTAssertEqual(card.cardSources.first?.title, "project #42")
        card.sources = [DecisionSource(kind: "pull_request", title: "Fix reconnect", url: card.mergePrUrl)]
        XCTAssertEqual(card.cardSources.count, 1)
    }
}
