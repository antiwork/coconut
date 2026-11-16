import XCTest
@testable import Coconut

/// Command-V in the Mac composer attaches an image or a file. Text pastes into the field as before.
final class PastedAttachmentTests: XCTestCase {
    func testTextPastesIntoTheField() {
        XCTAssertTrue(PastedAttachment.textWins("Hello"))
        XCTAssertTrue(PastedAttachment.textWins("see https://example.com/a.png"))
        XCTAssertTrue(PastedAttachment.textWins("line one\nline two"))
    }

    /// A copied image often carries its address as text. That paste attaches the image.
    func testAnImageWithOnlyALinkOrNoTextAttaches() {
        XCTAssertFalse(PastedAttachment.textWins(nil))
        XCTAssertFalse(PastedAttachment.textWins(""))
        XCTAssertFalse(PastedAttachment.textWins("  \n"))
        XCTAssertFalse(PastedAttachment.textWins("https://cdn.example.com/kitchen.jpg"))
    }

    /// The name has no colon, which the Mac does not allow in a file name, and no slash, which the service refuses.
    func testTheNameOfAPastedImage() {
        var components = DateComponents()
        components.year = 2026; components.month = 11; components.day = 13
        components.hour = 9; components.minute = 4; components.second = 7
        let date = Calendar.current.date(from: components)!
        XCTAssertEqual(PastedAttachment.imageName(at: date, ext: "png"), "Pasted image 09.04.07.png")
        XCTAssertEqual(PastedAttachment.imageName(at: date, ext: "jpg"), "Pasted image 09.04.07.jpg")
    }
}
