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

    /// The tile row shows the file: a type that a chat shows as one picture. Each other file shows as a card, also a
    /// TIFF, which can hold several pages: "Preview file" opens it in Quick Look with each page, while a tile, the
    /// viewer and Copy would show only the first one.
    var isImage: Bool { type.map { Self.tileTypes.contains($0.lowercased()) } ?? false }

    /// The types of a tile, in lowercase.
    static let tileTypes: Set<String> = ["image/jpeg", "image/png", "image/gif", "image/heic", "image/heif",
                                         "image/webp"]

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
///
/// "Photo" stands for each type that starts with `image/`, also a TIFF that the chat shows as a card
/// (TeamFile.isImage). The two rules differ on purpose: this one must give the same text as the chat list and the
/// notifications, which come from the Mac.
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
    static let largest = FileRules.largest
    /// The text of the agent chat for a file that does not fit (AppStore.uploadAttachment).
    static let sizeText = FileRules.sizeText
    static let limitText = "Add at most \(limit) files to one message."
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

extension FileFit {
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
