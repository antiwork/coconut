import XCTest
@testable import Coconut

final class LibraryDocumentTests: XCTestCase {
    func testTheBodyDropsATitleHeadingAtItsStart() {
        let markdown = "\n# #4274 — How the seller got through\n\nSource: gumroad-private#4274.\n\n## Timeline"
        XCTAssertEqual(BotDocument.body(markdown), "Source: gumroad-private#4274.\n\n## Timeline")
    }

    func testTheBodyKeepsTheIndentOfACodeBlockAfterTheTitle() {
        XCTAssertEqual(BotDocument.body("# Title\n\n    let x = 1\n    let y = 2"), "    let x = 1\n    let y = 2")
    }

    func testTheBodyKeepsOtherHeadingsAndText() {
        for markdown in ["## Timeline\nFirst step", "Intro\n# Later title", "#4274 is the issue", ""] {
            XCTAssertEqual(BotDocument.body(markdown), markdown)
        }
    }

    /// A Markdown page travels as JSON in a data block, which a "</script>" in the text cannot end.
    @MainActor
    func testAMarkdownPageTravelsAsJSONThatTheTextCannotEnd() throws {
        let markdown = "Lead.\n\n```html\n</script><script>alert(1)</script>\n```"
        let html = DocumentPage.html(title: "T", meta: "M", content: markdown, markdown: true)
        let start = try XCTUnwrap(html.range(of: "<script type=\"application/json\" id=\"coconut-markdown\">"))
        let end = try XCTUnwrap(html.range(of: "</script>", range: start.upperBound..<html.endIndex))
        let json = String(html[start.upperBound..<end.lowerBound]).replacingOccurrences(of: "<\\/", with: "</")
        XCTAssertEqual(try JSONDecoder().decode(String.self, from: Data(json.utf8)), markdown)
        XCTAssertEqual(html.components(separatedBy: "</script>").count, 2)
    }
}
