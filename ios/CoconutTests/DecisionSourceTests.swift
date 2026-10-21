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
        XCTAssertNil(card.mergePrDiff)
    }

    func testMergeCardDiffDecodesAndReadsCompactly() throws {
        let diff = try APIJSON.decoder().decode(PullRequestDiff.self, from: Data(
            #"{"additions":1171,"deletions":25,"changed_files":1}"#.utf8))
        XCTAssertEqual(diff.changedFiles, 1)
        XCTAssertEqual(diff.summary, "+\(1171.formatted()) −25 · 1 file")
        XCTAssertEqual(PullRequestDiff(additions: 3, deletions: 0, changedFiles: 2).accessibilityLabel,
                       "3 lines added, 0 lines removed, in 2 files")
    }
}
