import XCTest
@testable import Coconut

/// An image that the person attaches shows inline in the chat, not as a file card.
final class InlineAttachmentTests: XCTestCase {
    private func attachment(_ name: String, role: String = "user", id: String = "a1") -> Message {
        Message(id: "m-" + id, botId: "bot", taskId: "t", role: role, kind: "artifact", text: name, decisionId: nil,
                artifactId: id, createdAt: "2026-11-16T20:38:03Z", streaming: false)
    }

    func testAnAttachedImageIsAPhotoOfTheMac() {
        let photo = attachment("Pasted image 15.38.03.png").photo
        XCTAssertEqual(photo?.url.absoluteString, "coconut-file://a1")
        XCTAssertEqual(photo?.name, "Pasted image 15.38.03.png")
        XCTAssertNotNil(attachment("IMG_0001.HEIC").photo)
        XCTAssertNil(attachment("notes.pdf").photo, "Another file keeps its card.")
    }

    func testTwoPastedImagesShareOneRow() {
        let entries = TranscriptEntry.grouped([attachment("a.png", id: "a1"), attachment("b.jpg", id: "a2")])
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].photos.count, 2)
    }
}
