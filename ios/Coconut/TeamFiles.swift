import Foundation
import ImageIO
import UniformTypeIdentifiers

// The files of the messages of the team chats (docs/team-log.md, "Files"). The Mac keeps a picked file as a draft, and
// a send names the drafts. The chat shows an image in the tile row of the agent chat (PhotoTileRow) and each other file
// in the form of a file card of the agent chat (ChatFileCard).

/// One file of a message, or a draft of the composer: the item of the Mac. `complete` is false while pieces of the file
/// are still on the way to this Mac. A draft and a message on its way have no `complete`: their bytes are on the phone.
struct TeamFile: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var name: String
    var type: String? = nil
    var size: Int
    var width: Int? = nil
    var height: Int? = nil
    var complete: Bool? = nil

    /// The tile row shows the file: a type of an image that the app can draw. Each other file shows as a card.
    var isImage: Bool {
        guard let type = type?.lowercased(), type.hasPrefix("image/") else { return false }
        return type != "image/svg+xml"
    }

    /// The bytes are on this Mac, or on the phone for a draft.
    var isReady: Bool { complete ?? true }

    /// The second line of a file card: the kind and the size, for example "PDF, 1.2 MB".
    var detail: String {
        let ext = (name as NSString).pathExtension
        let kind = !ext.isEmpty && ext.count <= 5 ? ext.uppercased() : "File"
        return kind + ", " + ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file)
    }
}

extension TeamFile {
    /// A file of an item that does not fit (no ID, no name, no size) decodes to nil, so the list keeps the others.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        size = try container.decode(Int.self, forKey: .size)
        type = try? container.decodeIfPresent(String.self, forKey: .type)
        width = try? container.decodeIfPresent(Int.self, forKey: .width)
        height = try? container.decodeIfPresent(Int.self, forKey: .height)
        complete = try? container.decodeIfPresent(Bool.self, forKey: .complete)
    }

    private enum CodingKeys: String, CodingKey { case id, name, type, size, width, height, complete }
}

/// The short text of a message of a team chat, for a quote, the bar of a reply or an edit, and a row of a list: the
/// text, or for an empty text "Photo" when the first file is an image, else the name of the first file. This is the
/// rule of `shown_text` in backend/coconut/team_store.py, which gives the chat list and the notifications their text.
/// With files, a text of white space only is an empty text, as the Mac takes it.
enum TeamShortText {
    static func text(_ text: String, files: [TeamFile]) -> String {
        guard let first = files.first, text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return text }
        return (first.type ?? "").hasPrefix("image/") ? "Photo" : first.name
    }
}

extension TeamMessage {
    /// The short text of the message (TeamShortText).
    var shortText: String { TeamShortText.text(text, files: files) }
}

extension TeamPendingMessage {
    /// The short text of the message on its way (TeamShortText).
    var shortText: String { TeamShortText.text(text, files: files ?? []) }
}

/// The texts and the limits of the files of a message, in one place.
enum TeamFileRules {
    /// The most files of one message (`MAX_FILES` in backend/coconut/team_store.py).
    static let limit = 4
    /// The largest file (`MAX_FILE`).
    static let largest = PastedAttachment.limit
    /// The text of the agent chat for a file that does not fit (AppStore.uploadAttachment).
    static let sizeText = "Choose a file between 1 byte and 4 MB."
    static let limitText = "Add at most \(limit) files to one message."
    /// A file that the fit step cannot make safe to send: a file of several images whose GPS position cannot go, or an
    /// image that the app cannot read. The text is true for both.
    static let cannotSendText = "This file cannot be sent."
    /// The largest file that the app reads to make an image fit. A larger file is not read.
    static let largestRead = 64 * 1024 * 1024

    /// The composer offers files: a team chat or a named chat whose Mac sends the key `files` (TeamChat.offersFiles).
    /// A direct chat takes no file in this version.
    static func offered(in chat: TeamChatKey, _ state: TeamChat) -> Bool {
        !chat.isDirect && state.offersFiles
    }
}

/// Where the bytes of a file of a team chat come from: the paired Mac, by room, message and file. The address names
/// the message, so the disk cache and the route of the Mac never give the bytes of another message for the same file
/// ID.
struct TeamFileAddress: Hashable, Sendable {
    let room: String
    let message: String
    let file: String

    static let scheme = "coconut-team-file"

    init?(room: String, message: String, file: String) {
        guard [room, message, file].allSatisfy(Self.isID) else { return nil }
        self.room = room
        self.message = message
        self.file = file
    }

    init?(_ url: URL) {
        guard url.scheme == Self.scheme, let room = url.host() else { return nil }
        let parts = url.pathComponents.filter { $0 != "/" }
        guard parts.count == 2 else { return nil }
        self.init(room: room, message: parts[0], file: parts[1])
    }

    var url: URL { URL(string: "\(Self.scheme)://\(room)/\(message)/\(file)")! }

    /// The route of the Mac (docs/team-log.md, "Files").
    var path: String { "team/rooms/\(room)/messages/\(message)/files/\(file)" }

    /// The name of the file in the disk cache: all three IDs.
    var cacheName: String { "team-\(room)-\(message)-\(file)" }

    /// An ID of the Mac: lowercase hex, so it is safe in a path and in a file name.
    static func isID(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 64 && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }
}

/// The bytes of a draft on the phone, from the pick until the Mac confirms the send: a message on its way shows its
/// tiles from them. The scheme names the draft ID of the Mac.
enum TeamDraftAddress {
    static let scheme = "coconut-team-draft"

    static func url(_ draft: String) -> URL? {
        TeamFileAddress.isID(draft) ? URL(string: "\(scheme)://\(draft)") : nil
    }

    static func draft(_ url: URL) -> String? {
        guard url.scheme == scheme, let id = url.host(), TeamFileAddress.isID(id) else { return nil }
        return id
    }

    static func cacheName(_ draft: String) -> String { "draft-" + draft }
}

extension TeamChatRow {
    /// The files of the row: of the message of the Mac, or of the message on its way. The bytes of a message on its
    /// way are on the phone, so each of its files is ready.
    var files: [TeamFile] {
        switch content {
        case .message(let message): message.files
        case .pending(let pending): (pending.files ?? []).map { file in var file = file; file.complete = nil; return file }
        }
    }

    /// Where the bytes of a file of this row are: the Mac for a saved message, the phone for a message on its way.
    func address(of file: TeamFile, room: String) -> URL? {
        switch content {
        case .message(let message): TeamFileAddress(room: room, message: message.id, file: file.id)?.url
        case .pending: TeamDraftAddress.url(file.id)
        }
    }

    /// The images of the row as photos of the tile row. The spoken label of a tile is the file name. A file that is not
    /// complete shows the loading state of its tile.
    func photos(room: String) -> [MessagePhoto] {
        files.filter(\.isImage).compactMap { file in
            address(of: file, room: room).map { MessagePhoto(url: $0, caption: "", name: file.name, ready: file.isReady) }
        }
    }

    /// The files of the row that show as cards.
    var documents: [TeamFile] { files.filter { !$0.isImage } }
}

/// What the step before an upload gives: the file as it goes, or why it cannot go.
enum FitOutcome: Equatable, Sendable {
    case fitted(FittedFile)
    /// The file is empty, or larger than the limit, also after an image became smaller.
    case tooLarge
    /// The app cannot make the file fit the rules: it cannot remove the GPS position of a file with more than one
    /// image, or it cannot read the image.
    case cannotSend

    var file: FittedFile? { if case .fitted(let file) = self { file } else { nil } }

    /// The text for the error banner, or nil for a file that goes.
    var text: String? {
        switch self {
        case .fitted: nil
        case .tooLarge: TeamFileRules.sizeText
        case .cannotSend: TeamFileRules.cannotSendText
        }
    }
}

/// The step before an upload: an image fits the limit of a file, and the chat gets its size in pixels.
struct FittedFile: Equatable, Sendable {
    let data: Data
    let name: String
    let type: String?
    let width: Int?
    let height: Int?
}

enum FileFit {
    /// The longest side of an image that the app makes smaller.
    static let longestSide = 2400
    static let quality = 0.8
    /// The largest side that the Mac takes in `width` and `height` (`MAX_SIDE`).
    static let largestSide = 20_000

    /// The file as it goes to the Mac, or nil when it cannot fit. Each piece of a file is one write to the relay and
    /// one download on each device, and the space of a person in a room holds 320 MiB in 90 days. So a photo goes at
    /// the size that a screen shows:
    /// - An image with a side over `longestSide` pixels becomes a JPEG of `longestSide` pixels, whatever its bytes.
    /// - A HEIC or HEIF image always becomes a JPEG, so each Mac and each app can show it.
    /// - Each other image within `longestSide` pixels stays as it is, unless it is over the limit: then it becomes a
    ///   JPEG of the same size.
    /// - A GIF never changes, because a JPEG loses its frames. A GIF over the limit does not fit.
    /// - Each other file must be 1 byte to the limit as it is.
    /// `width` and `height` are the pixels that go to the Mac.
    ///
    /// With `removesPosition` (the team chats), no image leaves the phone or the Mac with a GPS position. A new JPEG
    /// carries no metadata. An image that stays as it is goes as a copy without the GPS data and without a new encode
    /// of its pixels. When that copy fails or is over the limit, the image becomes a JPEG: never the bytes with the
    /// position. An image without a position keeps its bytes. The agent chat sends to the Mac of the person and keeps
    /// the metadata.
    static func fit(_ data: Data, name: String, limit: Int = TeamFileRules.largest,
                    removesPosition: Bool = true) -> FittedFile? {
        outcome(data, name: name, limit: limit, removesPosition: removesPosition).file
    }

    /// The file as it goes to the Mac, or why it cannot go (`fit`).
    ///
    /// A file with more than one image (a TIFF with pages, an animated GIF, PNG, HEIC or WebP) is never encoded
    /// again: a JPEG would keep only the first image, for example page 1 of a scan of three pages. Without a position
    /// it goes as it is when it fits the limit, whatever its pixels. With a position it goes as the copy without it,
    /// which keeps each image, and else it does not go. ImageIO cannot copy a GIF this way, and its copy of a TIFF
    /// keeps the position, so a GIF or a TIFF of several images with a position does not go. `width` and `height` are
    /// the pixels of the first image.
    static func outcome(_ data: Data, name: String, limit: Int = TeamFileRules.largest,
                        removesPosition: Bool = true) -> FitOutcome {
        guard !data.isEmpty else { return .tooLarge }
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let identifier = CGImageSourceGetType(source) as String?, let kind = UTType(identifier),
              kind.conforms(to: .image), CGImageSourceGetCount(source) > 0,
              let size = pixelSize(source) else {
            guard data.count <= limit else { return .tooLarge }
            return .fitted(FittedFile(data: data, name: name, type: mimeType(name), width: nil, height: nil))
        }
        let (width, height) = size
        let longest = max(width, height)
        let position = removesPosition && hasPosition(source)
        if CGImageSourceGetCount(source) > 1 {
            guard data.count <= limit else { return .tooLarge }
            guard position else { return .fitted(kept(data, name: name, kind: kind, width: width, height: height)) }
            guard let copy = withoutPosition(source, kind: kind), copy.count <= limit else { return .cannotSend }
            return .fitted(kept(copy, name: name, kind: kind, width: width, height: height))
        }
        let gif = kind.conforms(to: .gif)
        let heif = kind.conforms(to: .heic) || kind.conforms(to: .heif)
        if gif || (!heif && longest <= longestSide && data.count <= limit) {
            guard data.count <= limit || !gif else { return .tooLarge }
            guard position else { return .fitted(kept(data, name: name, kind: kind, width: width, height: height)) }
            // A GIF has no EXIF block, but it can carry a position in XMP data, and ImageIO reads it. A GIF of one
            // frame goes as a copy without the position, else as a JPEG of its frame.
            if let copy = withoutPosition(source, kind: kind), copy.count <= limit {
                return .fitted(kept(copy, name: name, kind: kind, width: width, height: height))
            }
        }
        guard let image = downsampledImage(data, largestSide: min(longest, longestSide)) else { return .cannotSend }
        for quality in [quality, 0.6, 0.4] {
            if let jpeg = jpegData(image, quality: quality), jpeg.count <= limit {
                return .fitted(FittedFile(data: jpeg, name: jpegName(name), type: "image/jpeg",
                                          width: image.width, height: image.height))
            }
        }
        return .tooLarge
    }

    /// The file has a GPS position: in the container, or in the EXIF data (the GPS dictionary of ImageIO) or the XMP
    /// data of any of its images. A file with more than one image, for example a TIFF with pages, can carry a
    /// position only on a later image.
    static func hasPosition(_ source: CGImageSource) -> Bool {
        let container = CGImageSourceCopyProperties(source, nil) as? [CFString: Any]
        if container?[kCGImagePropertyGPSDictionary] != nil { return true }
        return (0..<CGImageSourceGetCount(source)).contains { index in
            let image = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any]
            if image?[kCGImagePropertyGPSDictionary] != nil { return true }
            guard let metadata = CGImageSourceCopyMetadataAtIndex(source, index, nil) else { return false }
            return [kCGImagePropertyGPSLatitude, kCGImagePropertyGPSLongitude].contains { key in
                CGImageMetadataCopyTagMatchingImageProperty(metadata, kCGImagePropertyGPSDictionary, key) != nil
            }
        }
    }

    /// A copy of the image without its GPS data. ImageIO copies the encoded pixels and leaves out the position. Nil
    /// when the copy fails, or when it still has a position.
    private static func withoutPosition(_ source: CGImageSource, kind: UTType) -> Data? {
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer, kind.identifier as CFString,
                                                                 CGImageSourceGetCount(source), nil),
              CGImageDestinationCopyImageSource(destination, source,
                                                [kCGImageMetadataShouldExcludeGPS: true] as CFDictionary, nil)
        else { return nil }
        let data = buffer as Data
        guard let copy = CGImageSourceCreateWithData(data as CFData, nil), !hasPosition(copy) else { return nil }
        return data
    }

    private static func kept(_ data: Data, name: String, kind: UTType, width: Int, height: Int) -> FittedFile {
        let fits = (1...largestSide).contains(width) && (1...largestSide).contains(height)
        return FittedFile(data: data, name: name, type: kind.preferredMIMEType ?? mimeType(name),
                          width: fits ? width : nil, height: fits ? height : nil)
    }

    /// The size of the first image in pixels, turned upright as the app draws it.
    private static func pixelSize(_ source: CGImageSource) -> (Int, Int)? {
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int, width > 0, height > 0 else { return nil }
        // The orientations 5 to 8 turn the image by a quarter.
        let turned = ((properties[kCGImagePropertyOrientation] as? Int) ?? 1) >= 5
        return turned ? (height, width) : (width, height)
    }

    private static func jpegData(_ image: CGImage, quality: Double) -> Data? {
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer, UTType.jpeg.identifier as CFString, 1, nil)
        else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(destination) ? buffer as Data : nil
    }

    static func jpegName(_ name: String) -> String {
        let base = (name as NSString).deletingPathExtension
        return (base.isEmpty ? "Photo" : base) + ".jpg"
    }

    static func mimeType(_ name: String) -> String? {
        let ext = (name as NSString).pathExtension
        return ext.isEmpty ? nil : UTType(filenameExtension: ext)?.preferredMIMEType
    }

    /// `fit` away from the main thread: a camera photo takes a moment to decode and encode.
    static func fitted(_ file: PickedFile, removesPosition: Bool = true) async -> FitOutcome {
        await Task.detached(priority: .userInitiated) {
            outcome(file.data, name: file.name, removesPosition: removesPosition)
        }.value
    }

    /// The name of a photo from the photo library, which has no file name: "Photo 10.42.05.jpg".
    static func photoName(at date: Date = .now, ext: String) -> String {
        PastedAttachment.timedName("Photo", at: date, ext: ext)
    }
}
