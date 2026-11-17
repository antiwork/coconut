#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// GitHub addresses in a message become cards (GitHubPreview.swift, GitHubEmbedViews.swift).
@MainActor
final class GitHubEmbedTests: XCTestCase {
    func testOnlyPagesOfGitHubWithACardAreFound() {
        let text = """
        Fixed in https://github.com/antiwork/gumroad/pull/2314 and https://github.com/antiwork/gumroad/issues/881.
        Same pull again: https://github.com/antiwork/gumroad/pull/2314. Not these: https://github.com/settings,
        https://example.com/antiwork/gumroad/pull/1, http://github.com/antiwork/gumroad/pull/3.
        Code: https://github.com/antiwork/gumroad/blob/main/app/jobs/payout_job.rb#L40-L52
        """
        XCTAssertEqual(GitHubLink.links(in: text).map(\.absoluteString), [
            "https://github.com/antiwork/gumroad/pull/2314",
            "https://github.com/antiwork/gumroad/issues/881",
            "https://github.com/antiwork/gumroad/blob/main/app/jobs/payout_job.rb#L40-L52",
        ])
        XCTAssertTrue(GitHubLink.isCard(URL(string: "https://github.com/antiwork/gumroad")!))
        XCTAssertTrue(GitHubLink.isCard(URL(string: "https://github.com/antiwork/gumroad/commit/8f3a2c1d")!))
        XCTAssertFalse(GitHubLink.isCard(URL(string: "https://github.com/antiwork/gumroad/commit/xyz")!))
        XCTAssertTrue(GitHubLink.links(in: "No links here").isEmpty)
    }

    func testThePreviewOfTheMacDecodesWithItsStateAndChecks() throws {
        let preview = try APIJSON.decoder().decode(GitHubPreview.self, from: Data(Self.pull.utf8))
        XCTAssertEqual(preview.kind, .pull)
        XCTAssertEqual(preview.reference, "#2314")
        XCTAssertEqual(preview.checks?.label, "1 check fails")
        XCTAssertEqual(preview.author?.avatar?.query, "v=4&s=64")
        let snippet = try XCTUnwrap(GitHubSnippet(try APIJSON.decoder().decode(GitHubPreview.self, from: Data(Self.file.utf8))))
        XCTAssertEqual(snippet.rows.first?.number, 40)
        XCTAssertEqual(snippet.rows.first?.text, "def perform(payout_id)", "The shared indentation is removed.")
    }

    func testTheCardsRender() throws {
        let previews = try [Self.pull, Self.issue, Self.commit, Self.file].map {
            try APIJSON.decoder().decode(GitHubPreview.self, from: Data($0.utf8))
        }
        let view = VStack(alignment: .leading, spacing: 10) {
            ForEach(previews, id: \.url) { GitHubEmbedCard(preview: $0) }
        }
        .padding(24).frame(width: 520).background(Color.white).environment(\.colorScheme, .light)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderBack(nil)
        for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
        XCTAssertGreaterThan(png.count, 20_000)
        if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
            try png.write(to: URL(fileURLWithPath: folder).appendingPathComponent("github-cards.png"))
        }
    }

    static let pull = """
    {"kind": "pull", "url": "https://github.com/antiwork/gumroad/pull/2314", "repository": "antiwork/gumroad",
     "number": 2314, "title": "Reuse the payout idempotency key after a Stripe timeout", "state": "open",
     "author": {"login": "gianfrancopiana", "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4"},
     "additions": 48, "deletions": 9, "changed_files": 3, "comments": 4,
     "checks": {"state": "failing", "passed": 11, "failed": 1, "pending": 0}, "review": "review_requested",
     "updated_at": "2026-11-16T22:10:00Z"}
    """
    static let issue = """
    {"kind": "issue", "url": "https://github.com/antiwork/gumroad/issues/881", "repository": "antiwork/gumroad",
     "number": 881, "title": "Sellers paid twice on Friday", "state": "open", "comments": 12,
     "labels": [{"name": "payouts", "color": "d73a4a"}, {"name": "p0", "color": "b60205"}],
     "assignees": [{"login": "slavingia"}], "updated_at": "2026-11-16T20:00:00Z"}
    """
    static let commit = """
    {"kind": "commit", "url": "https://github.com/antiwork/gumroad/commit/8f3a2c1d9b", "repository": "antiwork/gumroad",
     "sha": "8f3a2c1d9b7e", "title": "Store the payout key before the transfer call", "author": {"login": "slavingia"},
     "additions": 22, "deletions": 4}
    """
    static let file = """
    {"kind": "file", "url": "https://github.com/antiwork/gumroad/blob/main/app/jobs/payout_job.rb#L40-L44",
     "repository": "antiwork/gumroad", "path": "app/jobs/payout_job.rb", "ref": "main", "start_line": 40, "end_line": 44,
     "lines": ["  def perform(payout_id)", "    payout = Payout.find(payout_id)",
               "    Stripe::Payout.create(payout.params, idempotency_key: payout.external_id)", "  end", ""]}
    """
}
#endif
