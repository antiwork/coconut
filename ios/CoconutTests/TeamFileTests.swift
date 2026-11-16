import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import Coconut

/// The files of the team chats (docs/team-log.md, "Files"): the decode of a message, the composer, the step that makes
/// an image fit, and the disk cache of the bytes.
final class TeamFileTests: XCTestCase {
    private static let room = String(repeating: "a", count: 64)
    private static let file = String(repeating: "f", count: 32)

    private func decode(_ json: String) throws -> TeamMessagePage {
        try APIJSON.decoder().decode(TeamMessagePage.self, from: Data(json.utf8))
    }

    // MARK: Model

    /// A Mac with files sends the key `files` on each message, also as an empty list. A Mac without files sends no key:
    /// the message has no files, and the app knows that this Mac takes none. A file that does not fit drops only that
    /// file.
    func testAMessageDecodesWithAndWithoutFiles() throws {
        let page = try decode("""
        {"messages": [
          {"id": "e3", "first": 3, "text": "", "created_at": "2026-11-16T10:00:00Z",
           "files": [{"id": "\(Self.file)", "name": "photo.jpg", "type": "image/jpeg", "size": 2048, "width": 640,
                      "height": 480, "complete": false},
                     {"id": "\(String(repeating: "e", count: 32))", "name": "report.pdf", "type": "application/pdf",
                      "size": 1200000, "complete": true},
                     {"name": "no id"}]},
          {"id": "e2", "first": 2, "text": "Hi", "created_at": "2026-11-16T10:00:00Z", "files": []},
          {"id": "e1", "first": 1, "text": "Old", "created_at": "2026-11-16T10:00:00Z"}
        ], "before": null}
        """)
        XCTAssertEqual(page.messages.map(\.filesKey), [true, true, false])
        let files = page.messages[0].files
        XCTAssertEqual(files.map(\.name), ["photo.jpg", "report.pdf"])
        XCTAssertEqual(files[0], TeamFile(id: Self.file, name: "photo.jpg", type: "image/jpeg", size: 2048, width: 640,
                                          height: 480, complete: false))
        XCTAssertTrue(files[0].isImage)
        XCTAssertFalse(files[0].isReady)
        XCTAssertFalse(files[1].isImage)
        XCTAssertEqual(files[1].detail, "PDF, 1.2 MB")
        XCTAssertEqual(page.messages[2].files, [])
    }

    /// The composer offers files only where the newest message has the key `files`, and never in a direct chat.
    func testOffersFilesFollowsTheNewestMessageAndNoDirectChat() {
        var chat = TeamChat()
        let team = TeamChatKey(room: Self.room)
        XCTAssertFalse(TeamFileRules.offered(in: team, chat), "A chat without a message.")
        var older = TeamMessage(id: "e1", first: 1, text: "A", createdAt: "2026-11-16T10:00:00Z")
        older.filesKey = true
        var newer = TeamMessage(id: "e2", first: 2, text: "B", createdAt: "2026-11-16T10:00:00Z")
        chat.messages = [older, newer]
        XCTAssertFalse(TeamFileRules.offered(in: team, chat), "A Mac without files.")
        newer.filesKey = true
        chat.messages = [older, newer]
        XCTAssertTrue(TeamFileRules.offered(in: team, chat))
        XCTAssertTrue(TeamFileRules.offered(in: TeamChatKey(room: Self.room, chat: "c1"), chat), "A named chat.")
        XCTAssertFalse(TeamFileRules.offered(in: TeamChatKey(room: Self.room, member: "m2"), chat))
        XCTAssertFalse(TeamFileRules.offered(in: TeamChatKey(room: Self.room, bot: "t-r-b"), chat))
    }

    /// A saved message loads its files from the Mac with an address that names the message. A message on its way shows
    /// its files from the bytes on the phone. The spoken label of a tile is the file name.
    func testTheRowsOfAMessageAndOfAMessageOnItsWay() throws {
        let message = String(repeating: "b", count: 32)
        let image = TeamFile(id: Self.file, name: "kitchen.png", type: "image/png", size: 10, complete: false)
        let pdf = TeamFile(id: String(repeating: "e", count: 32), name: "plan.pdf", type: "application/pdf", size: 10,
                           complete: true)
        let saved = TeamMessage(id: message, first: 1, text: "", createdAt: "2026-11-16T10:00:00Z",
                                files: [image, pdf], filesKey: true)
        var chat = TeamChat()
        chat.messages = [saved]
        chat.pending = [TeamPendingMessage(id: "p1", text: "", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-16T10:01:00Z", files: [image])]
        let rows = TeamChatRow.rows(chat)
        let photos = rows[0].photos(room: Self.room)
        XCTAssertEqual(photos.map(\.url.absoluteString),
                       ["coconut-team-file://\(Self.room)/\(message)/\(Self.file)"])
        XCTAssertEqual(photos.map(\.label), ["kitchen.png"])
        XCTAssertEqual(photos.map(\.ready), [false])
        XCTAssertTrue(photos[0].isMacFile)
        XCTAssertEqual(rows[0].documents.map(\.name), ["plan.pdf"])
        let address = try XCTUnwrap(TeamFileAddress(photos[0].url))
        XCTAssertEqual(address.path, "team/rooms/\(Self.room)/messages/\(message)/files/\(Self.file)")
        let pending = rows[1].photos(room: Self.room)
        XCTAssertEqual(pending.map(\.url.absoluteString), ["coconut-team-draft://\(Self.file)"])
        XCTAssertEqual(pending.map(\.ready), [true], "The bytes of a draft are on the phone.")
        XCTAssertNil(TeamFileAddress(room: Self.room, message: "../x", file: Self.file))
    }

    // MARK: Short text

    /// The rule of `shown_text` of the service: the text, else "Photo" for a first image, else the first file name.
    func testTheShortTextOfAMessageWithOnlyFiles() {
        let photo = TeamFile(id: Self.file, name: "IMG_1.jpg", type: "image/jpeg", size: 1)
        let pdf = TeamFile(id: Self.file, name: "Plan.pdf", type: "application/pdf", size: 1)
        XCTAssertEqual(TeamShortText.text("Look", files: [photo]), "Look")
        XCTAssertEqual(TeamShortText.text("", files: [photo, pdf]), "Photo")
        XCTAssertEqual(TeamShortText.text(" \n", files: [pdf, photo]), "Plan.pdf", "White space with files is empty.")
        XCTAssertEqual(TeamShortText.text("", files: [TeamFile(id: Self.file, name: "a", size: 1)]), "a")
        XCTAssertEqual(TeamShortText.text("", files: []), "")
        let message = TeamMessage(id: "e1", first: 1, text: "", createdAt: "2026-11-16T10:00:00Z", files: [photo])
        XCTAssertEqual(message.shortText, "Photo")
        let pending = TeamPendingMessage(id: "p1", text: "", replyTo: nil, reply: nil,
                                         createdAt: "2026-11-16T10:01:00Z", files: [pdf])
        XCTAssertEqual(pending.shortText, "Plan.pdf")
        // The row of a direct chat that the list of the Mac does not have yet uses it too.
        var chat = TeamChat()
        chat.messages = [message]
        XCTAssertEqual(chat.newestLocal?.text, "Photo")
        chat.pending = [pending]
        XCTAssertEqual(chat.newestLocal?.text, "Plan.pdf")
    }

    /// A file that is not complete gives a photo that is not ready: its tile opens nothing, and a viewer page waits.
    /// A later view with the file complete gives the same address, now ready, so the page loads it.
    func testAPhotoTurnsReadyWithTheSameAddress() throws {
        let message = String(repeating: "b", count: 32)
        var file = TeamFile(id: Self.file, name: "a.jpg", type: "image/jpeg", size: 10, complete: false)
        func photo() throws -> MessagePhoto {
            let saved = TeamMessage(id: message, first: 1, text: "", createdAt: "2026-11-16T10:00:00Z", files: [file])
            var chat = TeamChat()
            chat.messages = [saved]
            return try XCTUnwrap(TeamChatRow.rows(chat).first?.photos(room: Self.room).first)
        }
        let waiting = try photo()
        file.complete = true
        let ready = try photo()
        XCTAssertFalse(waiting.ready)
        XCTAssertTrue(ready.ready)
        XCTAssertEqual(waiting.url, ready.url)
    }

    // MARK: Composer

    /// Send is on with a text or with a file, and off while a file uploads. The composer takes at most four files, and
    /// a chip removes its draft.
    func testTheComposerSendsATextOrAFileAndTakesAtMostFour() throws {
        var state = TeamComposerState()
        XCTAssertFalse(state.canSend(enabled: true))
        let first = try XCTUnwrap(state.startUpload())
        XCTAssertFalse(state.canSend(enabled: true), "A file on its way.")
        state.finishUpload(first, TeamFile(id: "d1", name: "a.png", size: 1))
        XCTAssertTrue(state.canSend(enabled: true), "One file and no text.")
        XCTAssertFalse(state.canSend(enabled: false))
        for index in 2...4 {
            let id = try XCTUnwrap(state.startUpload())
            state.finishUpload(id, TeamFile(id: "d\(index)", name: "\(index).png", size: 1))
        }
        XCTAssertFalse(state.canAddFile)
        XCTAssertNil(state.startUpload(), "A fifth file does not fit.")
        XCTAssertEqual(state.uploading, 0)
        XCTAssertEqual(state.removeFile("d2")?.name, "2.png")
        XCTAssertEqual(state.files.map(\.id), ["d1", "d3", "d4"])
        let again = try XCTUnwrap(state.startUpload(), "A removed chip makes space.")
        state.finishUpload(again, nil)
        XCTAssertEqual(state.files.count, 3, "A failed upload adds no chip.")
        XCTAssertEqual(state.uploading, 0, "A failed upload gives its place back.")
        state.sent()
        XCTAssertEqual(state.files, [])
        XCTAssertFalse(state.canSend(enabled: true))
        state.draft = "Hi"
        XCTAssertTrue(state.canSend(enabled: true))
    }

    /// Photos count as on their way from the pick, also while they still load: Send stays off, and the limit counts
    /// them.
    func testPickedPhotosCountFromThePick() throws {
        var state = TeamComposerState()
        state.draft = "Two photos"
        state.files = [TeamFile(id: "d1", name: "a.png", size: 1), TeamFile(id: "d2", name: "b.png", size: 1)]
        let one = try XCTUnwrap(state.startUpload())
        let two = try XCTUnwrap(state.startUpload())
        XCTAssertFalse(state.canSend(enabled: true), "No load ended yet.")
        XCTAssertFalse(state.canAddFile, "Two files and two picks are four.")
        XCTAssertEqual(state.uploading, 2)
        state.finishUpload(two, nil)
        XCTAssertFalse(state.canSend(enabled: true), "The first photo still loads.")
        XCTAssertTrue(state.canAddFile, "A failed load gives its place back.")
        state.finishUpload(one, TeamFile(id: "d3", name: "c.png", size: 1))
        XCTAssertTrue(state.canSend(enabled: true))
        XCTAssertEqual(state.files.map(\.id), ["d1", "d2", "d3"])
    }

    /// The files keep the order of the picks, also when the first upload ends last.
    func testTheFilesKeepTheOrderOfThePicks() throws {
        var state = TeamComposerState()
        let ids = try (1...3).map { _ in try XCTUnwrap(state.startUpload()) }
        state.finishUpload(ids[1], TeamFile(id: "d2", name: "2.png", size: 1))
        state.finishUpload(ids[2], TeamFile(id: "d3", name: "3.png", size: 1))
        XCTAssertEqual(state.files, [], "The later files wait for the first one.")
        XCTAssertFalse(state.canSend(enabled: true))
        state.finishUpload(ids[0], TeamFile(id: "d1", name: "1.png", size: 1))
        XCTAssertEqual(state.files.map(\.id), ["d1", "d2", "d3"])
        XCTAssertEqual(state.uploading, 0)
        // The agent chat composer uses the same order.
        var uploads = OrderedUploads<String>()
        let a = uploads.start(), b = uploads.start(), c = uploads.start()
        XCTAssertEqual(uploads.finish(c, "3"), [])
        XCTAssertEqual(uploads.finish(a, "1"), ["1"])
        XCTAssertEqual(uploads.finish(b, nil), ["3"], "A failure leaves no gap.")
        XCTAssertEqual(uploads.count, 0)
    }

    /// An edit needs a text, except for a message with files, which the Mac takes with an empty text.
    func testAnEditOfAMessageWithFilesTakesAnEmptyText() {
        var state = TeamComposerState()
        state.edit(TeamMessage(id: "e1", first: 1, text: "Old", createdAt: "2026-11-16T10:00:00Z"))
        state.draft = " "
        XCTAssertFalse(state.canSend(enabled: true))
        state.edit(TeamMessage(id: "e2", first: 2, text: "Photo", createdAt: "2026-11-16T10:00:00Z",
                               files: [TeamFile(id: Self.file, name: "a.png", type: "image/png", size: 1)]))
        state.draft = ""
        XCTAssertTrue(state.canSend(enabled: true))
    }

    // MARK: Fit

    private func image(width: Int, height: Int, type: UTType, noise: Bool = false, gps: Bool = false) throws -> Data {
        let context = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        if noise, let pixels = context.data?.assumingMemoryBound(to: UInt32.self) {
            // Random pixels do not compress, so the PNG is larger than the limit.
            var generator = SystemRandomNumberGenerator()
            for index in 0..<(context.bytesPerRow / 4 * height) { pixels[index] = generator.next() | 0xFF00_0000 }
        } else {
            context.setFillColor(red: 0.2, green: 0.5, blue: 0.8, alpha: 1)
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }
        let buffer = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(buffer, type.identifier as CFString, 1, nil))
        let position: [CFString: Any] = [kCGImagePropertyGPSDictionary: [
            kCGImagePropertyGPSLatitude: 52.37, kCGImagePropertyGPSLatitudeRef: "N",
            kCGImagePropertyGPSLongitude: 4.89, kCGImagePropertyGPSLongitudeRef: "E"]]
        CGImageDestinationAddImage(destination, try XCTUnwrap(context.makeImage()),
                                   gps ? position as CFDictionary : nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return buffer as Data
    }

    private func hasGPS(_ data: Data) throws -> Bool {
        FileFit.hasPosition(try XCTUnwrap(CGImageSourceCreateWithData(data as CFData, nil)))
    }

    // MARK: Position

    /// A JPEG within 2400 pixels with a GPS position goes out at the same size without the position.
    func testAPhotoThatStaysLosesItsPosition() throws {
        let jpeg = try image(width: 800, height: 600, type: .jpeg, gps: true)
        XCTAssertTrue(try hasGPS(jpeg), "The test photo has a position.")
        let fitted = try XCTUnwrap(FileFit.fit(jpeg, name: "IMG_3.JPG"))
        XCTAssertFalse(try hasGPS(fitted.data))
        XCTAssertEqual([fitted.width, fitted.height], [800, 600])
        XCTAssertEqual(fitted.type, "image/jpeg")
        XCTAssertEqual(fitted.name, "IMG_3.JPG", "A copy, not a new JPEG.")
        let source = try XCTUnwrap(CGImageSourceCreateWithData(fitted.data as CFData, nil))
        let properties = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
        XCTAssertNil(properties[kCGImagePropertyGPSDictionary])
        XCTAssertEqual(properties[kCGImagePropertyPixelWidth] as? Int, 800)
        // The agent chat sends to the Mac of the person and keeps the metadata.
        XCTAssertEqual(FileFit.fit(jpeg, name: "IMG_3.JPG", removesPosition: false)?.data, jpeg)
    }

    /// ImageIO keeps a GPS position in a GIF too, as XMP data. Such a GIF goes out without it.
    func testAGIFWithAPositionLosesIt() throws {
        let gif = try image(width: 40, height: 30, type: .gif, gps: true)
        XCTAssertTrue(try hasGPS(gif), "A GIF can carry a position.")
        let fitted = try XCTUnwrap(FileFit.fit(gif, name: "a.gif"))
        XCTAssertFalse(try hasGPS(fitted.data))
        XCTAssertEqual([fitted.width, fitted.height], [40, 30])
        // A GIF without a position stays as it is (testAGIFStaysAsItIs).
    }

    /// A file of two images where only the second one has a position: the check sees it, and the file goes out
    /// without a position on any image.
    func testAPositionOnASecondImageCounts() throws {
        let context = try XCTUnwrap(CGContext(data: nil, width: 60, height: 40, bitsPerComponent: 8, bytesPerRow: 0,
                                              space: CGColorSpaceCreateDeviceRGB(),
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(red: 0, green: 0.5, blue: 0, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: 60, height: 40))
        let picture = try XCTUnwrap(context.makeImage())
        let buffer = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(buffer, UTType.tiff.identifier as CFString,
                                                                         2, nil))
        CGImageDestinationAddImage(destination, picture, nil)
        let position: [CFString: Any] = [kCGImagePropertyGPSDictionary: [
            kCGImagePropertyGPSLatitude: 52.37, kCGImagePropertyGPSLatitudeRef: "N"]]
        CGImageDestinationAddImage(destination, picture, position as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        let tiff = buffer as Data
        let source = try XCTUnwrap(CGImageSourceCreateWithData(tiff as CFData, nil))
        XCTAssertEqual(CGImageSourceGetCount(source), 2)
        let first = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
        XCTAssertNil(first?[kCGImagePropertyGPSDictionary], "The first image has no position.")
        XCTAssertTrue(FileFit.hasPosition(source))
        let fitted = try XCTUnwrap(FileFit.fit(tiff, name: "pages.tiff"))
        XCTAssertFalse(try hasGPS(fitted.data))
        XCTAssertNotEqual(fitted.data, tiff)
    }

    /// The same photo without a position goes out with its own bytes.
    func testAPhotoWithoutAPositionKeepsItsBytes() throws {
        let jpeg = try image(width: 800, height: 600, type: .jpeg)
        XCTAssertFalse(try hasGPS(jpeg))
        XCTAssertEqual(FileFit.fit(jpeg, name: "IMG_4.JPG")?.data, jpeg)
    }

    /// A photo over 2400 pixels becomes a new JPEG, which carries no position.
    func testALargePhotoWithAPositionLosesIt() throws {
        let jpeg = try image(width: 3000, height: 2000, type: .jpeg, gps: true)
        XCTAssertTrue(try hasGPS(jpeg))
        let fitted = try XCTUnwrap(FileFit.fit(jpeg, name: "IMG_5.JPG"))
        XCTAssertEqual([fitted.width, fitted.height], [2400, 1600])
        XCTAssertFalse(try hasGPS(fitted.data))
    }

    /// A PNG under the limit goes as it is, with its size in pixels.
    func testASmallPNGStaysAsItIs() throws {
        let png = try image(width: 300, height: 200, type: .png)
        let fitted = try XCTUnwrap(FileFit.fit(png, name: "chart.png"))
        XCTAssertEqual(fitted.data, png)
        XCTAssertEqual(fitted.name, "chart.png")
        XCTAssertEqual(fitted.type, "image/png")
        XCTAssertEqual([fitted.width, fitted.height], [300, 200])
    }

    /// An image over the limit becomes a JPEG of at most 2400 pixels on its longest side.
    func testALargeImageBecomesASmallerJPEG() throws {
        let png = try image(width: 3000, height: 1500, type: .png, noise: true)
        XCTAssertGreaterThan(png.count, TeamFileRules.largest)
        let fitted = try XCTUnwrap(FileFit.fit(png, name: "scan.png"))
        XCTAssertLessThanOrEqual(fitted.data.count, TeamFileRules.largest)
        XCTAssertEqual(fitted.name, "scan.jpg")
        XCTAssertEqual(fitted.type, "image/jpeg")
        XCTAssertEqual([fitted.width, fitted.height], [2400, 1200])
        let source = try XCTUnwrap(CGImageSourceCreateWithData(fitted.data as CFData, nil))
        XCTAssertEqual(CGImageSourceGetType(source) as String?, UTType.jpeg.identifier)
    }

    /// A camera photo of 4032 by 3024 pixels as a JPEG under the limit still becomes a JPEG of 2400 pixels: each
    /// piece of a file is one write and one download on each device.
    func testALargeCameraJPEGUnderTheLimitBecomesSmaller() throws {
        let jpeg = try image(width: 4032, height: 3024, type: .jpeg)
        XCTAssertLessThan(jpeg.count, TeamFileRules.largest)
        let fitted = try XCTUnwrap(FileFit.fit(jpeg, name: "IMG_0002.JPG"))
        XCTAssertEqual([fitted.width, fitted.height], [2400, 1800], "The size of the pixels that go to the Mac.")
        XCTAssertEqual(fitted.type, "image/jpeg")
        XCTAssertEqual(fitted.name, "IMG_0002.jpg")
        let source = try XCTUnwrap(CGImageSourceCreateWithData(fitted.data as CFData, nil))
        let properties = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
        XCTAssertEqual(properties[kCGImagePropertyPixelWidth] as? Int, 2400)
        XCTAssertEqual(properties[kCGImagePropertyPixelHeight] as? Int, 1800)
    }

    /// An image within 2400 pixels but over the limit becomes a JPEG of the same size.
    func testASmallImageOverTheLimitKeepsItsSizeAsAJPEG() throws {
        let png = try image(width: 1200, height: 1000, type: .png, noise: true)
        XCTAssertGreaterThan(png.count, TeamFileRules.largest)
        let fitted = try XCTUnwrap(FileFit.fit(png, name: "noise.png"))
        XCTAssertEqual(fitted.type, "image/jpeg")
        XCTAssertEqual([fitted.width, fitted.height], [1200, 1000])
        XCTAssertLessThanOrEqual(fitted.data.count, TeamFileRules.largest)
    }

    /// A GIF never changes, also when it is wider than 2400 pixels: a JPEG loses its frames.
    func testAGIFStaysAsItIs() throws {
        let gif = try image(width: 3000, height: 100, type: .gif)
        let fitted = try XCTUnwrap(FileFit.fit(gif, name: "banner.gif"))
        XCTAssertEqual(fitted.data, gif)
        XCTAssertEqual(fitted.type, "image/gif")
        XCTAssertEqual([fitted.width, fitted.height], [3000, 100])
        XCTAssertNil(FileFit.fit(gif, name: "banner.gif", limit: gif.count - 1), "A GIF over the limit does not fit.")
    }

    /// A HEIC image becomes a JPEG also under the limit, so each Mac and each app can show it.
    func testAHEICImageBecomesAJPEG() throws {
        let heic = try image(width: 400, height: 300, type: .heic)
        let fitted = try XCTUnwrap(FileFit.fit(heic, name: "IMG_0001.HEIC"))
        XCTAssertEqual(fitted.name, "IMG_0001.jpg")
        XCTAssertEqual(fitted.type, "image/jpeg")
        XCTAssertEqual([fitted.width, fitted.height], [400, 300])
    }

    /// A file of another kind must be 1 byte to 4 MB as it is.
    func testAnotherFileMustFitAsItIs() {
        XCTAssertEqual(FileFit.fit(Data("%PDF".utf8), name: "a.pdf")?.type, "application/pdf")
        XCTAssertNil(FileFit.fit(Data(), name: "a.pdf"))
        XCTAssertNil(FileFit.fit(Data(count: TeamFileRules.largest + 1), name: "a.zip"))
    }

    // MARK: Cache

    /// Two messages name the same file ID with other bytes. Each one gets its own bytes: the disk cache and the route
    /// of the Mac have the message in the key.
    func testTwoMessagesWithOneFileIDAndOtherBytesStayApart() async throws {
        let first = String(repeating: "1", count: 32)
        let second = String(repeating: "2", count: 32)
        let red = try image(width: 4, height: 4, type: .png)
        let wide = try image(width: 8, height: 2, type: .png)
        let previous = PhotoLoader.shared.teamFile
        defer { PhotoLoader.shared.teamFile = previous }
        let asked = AskedAddresses()
        PhotoLoader.shared.teamFile = { address in
            await asked.add(address.message)
            return address.message == first ? red : wide
        }
        let room = String(UUID().uuidString.lowercased().filter(\.isHexDigit))
        let one = try XCTUnwrap(TeamFileAddress(room: room, message: first, file: Self.file))
        let two = try XCTUnwrap(TeamFileAddress(room: room, message: second, file: Self.file))
        let firstData = await PhotoLoader.shared.data(one.url)
        let secondData = await PhotoLoader.shared.data(two.url)
        XCTAssertEqual(firstData, red)
        XCTAssertEqual(secondData, wide)
        let firstImage = await PhotoLoader.shared.image(one.url, size: .tile)
        let secondImage = await PhotoLoader.shared.image(two.url, size: .tile)
        XCTAssertEqual(firstImage?.size, CGSize(width: 4, height: 4))
        XCTAssertEqual(secondImage?.size, CGSize(width: 8, height: 2))
        // The bytes stay on the phone: a second read asks the Mac nothing.
        PhotoLoader.shared.teamFile = { _ in nil }
        let again = await PhotoLoader.shared.data(two.url)
        XCTAssertEqual(again, wide)
        let messages = await asked.messages
        XCTAssertEqual(messages, [first, second])
    }

    /// Copy and Share of a held photo get the full image, also when the chat has only loaded the tile.
    func testCopyAndShareGetTheFullImageAfterTheTile() async throws {
        let png = try image(width: 2400, height: 1600, type: .png)
        let previous = PhotoLoader.shared.teamFile
        defer { PhotoLoader.shared.teamFile = previous }
        PhotoLoader.shared.teamFile = { _ in png }
        let room = String(UUID().uuidString.lowercased().filter(\.isHexDigit))
        let address = try XCTUnwrap(TeamFileAddress(room: room, message: String(repeating: "d", count: 32),
                                                    file: Self.file))
        let tile = await PhotoLoader.shared.image(address.url, size: .tile)
        XCTAssertEqual(tile?.size, CGSize(width: 900, height: 600), "The chat shows the tile.")
        let shared = await PhotoLoader.shared.shareImage(address.url)
        XCTAssertEqual(shared?.size, CGSize(width: 2400, height: 1600), "Each pixel that the Mac got.")
    }

    /// The bytes of a draft show for the message on its way, and become the bytes of the file of the saved message.
    func testTheBytesOfADraftBecomeTheBytesOfTheMessage() async throws {
        let draft = String(UUID().uuidString.lowercased().filter(\.isHexDigit))
        let png = try image(width: 4, height: 4, type: .png)
        let previous = PhotoLoader.shared.teamFile
        defer { PhotoLoader.shared.teamFile = previous }
        PhotoLoader.shared.teamFile = { _ in nil }
        PhotoLoader.shared.keepDraft(png, id: draft)
        let local = await PhotoLoader.shared.data(try XCTUnwrap(TeamDraftAddress.url(draft)))
        XCTAssertEqual(local, png)
        let address = try XCTUnwrap(TeamFileAddress(room: Self.room, message: String(repeating: "c", count: 32),
                                                    file: draft))
        PhotoLoader.shared.adoptDraft(draft, as: address)
        let saved = await PhotoLoader.shared.data(address.url)
        XCTAssertEqual(saved, png)
        let gone = await PhotoLoader.shared.data(try XCTUnwrap(TeamDraftAddress.url(draft)))
        XCTAssertNil(gone)
    }
}

private actor AskedAddresses {
    var messages: [String] = []
    func add(_ message: String) { messages.append(message) }
}
