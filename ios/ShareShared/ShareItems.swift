import Foundation
import UniformTypeIdentifiers

// What a share sheet gives to Coconut, and the message that it becomes. The share extensions of the iPhone and the
// Mac compile this file with the apps, so it uses Foundation only.

/// One file of a share: its bytes and its name.
struct SharedFile: Equatable, Sendable {
    let data: Data
    let name: String
}

/// One thing that the share sheet gave, in the order of the share.
enum SharedItem: Equatable, Sendable {
    case text(String)
    case link(URL)
    case file(SharedFile)
}

/// The message of a share: its text, and its files in the order of the share.
struct ShareMessage: Equatable, Sendable {
    var text: String
    var files: [SharedFile]
}

/// Why a share cannot become a message. The text is for the person.
enum ShareProblem: Error, Equatable, Sendable {
    case empty
    case tooManyFiles
    case textTooLong
    case takesNoFiles

    var text: String {
        switch self {
        case .empty: "There is nothing to send."
        case .tooManyFiles: "A message takes at most \(ShareComposer.fileLimit) files."
        case .textTooLong: "This text is too long for one message."
        case .takesNoFiles: "A direct chat takes text and links only."
        }
    }
}

/// Why a share did not go. The text is for the person.
struct ShareFailure: Error, Equatable, Sendable {
    let text: String

    init(text: String) { self.text = text }
}

enum ShareComposer {
    /// The most files of one message (`MAX_FILES` in backend/coconut/team_store.py). An agent chat gets the same
    /// limit, so a share has one rule.
    static let fileLimit = 4
    /// The longest text of a message of a team chat (`MAX_TEXT`), in Unicode scalars, as the service counts.
    static let textLimit = 16_000

    /// The message of a share. The note of the person comes first, then an empty line, then each shared text and
    /// link on its own lines, in the order of the share. A browser gives the address of a page as a link and often
    /// as a text too, so a text that only repeats a link or an earlier text goes once. `takesFiles` is false for a
    /// direct chat, which takes text only.
    static func message(note: String, items: [SharedItem], takesFiles: Bool = true) -> Result<ShareMessage, ShareProblem> {
        var lines: [String] = []
        var files: [SharedFile] = []
        for item in distinct(items) {
            switch item {
            case .file(let file): files.append(file)
            case .link(let url): lines.append(url.absoluteString)
            case .text(let text): lines.append(text)
            }
        }
        let note = note.trimmingCharacters(in: .whitespacesAndNewlines)
        let shared = lines.joined(separator: "\n")
        let text = [note, shared].filter { !$0.isEmpty }.joined(separator: "\n\n")
        guard !text.isEmpty || !files.isEmpty else { return .failure(.empty) }
        guard files.isEmpty || takesFiles else { return .failure(.takesNoFiles) }
        guard files.count <= fileLimit else { return .failure(.tooManyFiles) }
        guard text.unicodeScalars.count <= textLimit else { return .failure(.textTooLong) }
        return .success(ShareMessage(text: text, files: files))
    }

    /// The items that go, in the order of the share: each file, each link once, and each text once, without the
    /// white space around it. An empty text and a text that only repeats a link of the share are left out. The sheet
    /// lists these items, so it shows what the message will hold.
    static func distinct(_ items: [SharedItem]) -> [SharedItem] {
        let links = Set(items.compactMap { item -> String? in
            if case .link(let url) = item { return url.absoluteString }
            return nil
        })
        var seen: Set<String> = []
        return items.compactMap { item in
            switch item {
            case .file: return item
            case .link(let url): return seen.insert(url.absoluteString).inserted ? item : nil
            case .text(let text):
                let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty, !links.contains(text), seen.insert(text).inserted else { return nil }
                return .text(text)
            }
        }
    }
}

/// How the extension reads one item of the share sheet. An item names several types for the same thing, for example
/// a photo as a file and as an image.
enum ShareRepresentation: Equatable, Sendable {
    /// A file on the device. Its bytes go with its own name.
    case fileURL
    /// Bytes of this type, for example a photo of the library or a PDF of a print dialog.
    case data(String)
    case link
    case text

    /// The representation that keeps the most of an item with these type identifiers, or nil for an item that
    /// Coconut cannot send. A file wins, because it has a name. Then an image and a PDF as bytes, then a link, then
    /// text, then any other bytes.
    static func choose(from identifiers: [String]) -> ShareRepresentation? {
        let types = identifiers.compactMap { id in UTType(id).map { (id, $0) } }
        if types.contains(where: { $0.1.conforms(to: .fileURL) }) { return .fileURL }
        for kind in [UTType.image, .pdf] {
            if let match = types.first(where: { $0.1.conforms(to: kind) }) { return .data(match.0) }
        }
        if types.contains(where: { $0.1.conforms(to: .url) }) { return .link }
        if types.contains(where: { $0.1.conforms(to: .text) }) { return .text }
        if let match = types.first(where: { $0.1.conforms(to: .data) }) { return .data(match.0) }
        return nil
    }
}

enum ShareFileName {
    /// The longest file name that the Mac takes (`MAX_FILE_NAME`).
    static let limit = 160

    /// The name of bytes without a file: the name that the sending app suggests, with the extension of the type.
    /// Without a suggestion, an image is "Image" and each other file is "File". A second file of the same share gets
    /// its number, so two photos have two names.
    static func name(suggested: String?, typeIdentifier: String, index: Int = 0) -> String {
        let type = UTType(typeIdentifier)
        let ext = type?.preferredFilenameExtension
        var base = clean(suggested ?? "")
        if let ext, base.lowercased().hasSuffix("." + ext.lowercased()) { base = (base as NSString).deletingPathExtension }
        if base.isEmpty {
            base = type?.conforms(to: .image) == true ? "Image" : "File"
            if index > 0 { base += " \(index + 1)" }
        }
        return clean(ext.map { base + "." + $0 } ?? base)
    }

    /// A name that is safe as one path component on the Mac, within its length. The Mac refuses a name with a
    /// folder in it.
    static func clean(_ name: String) -> String {
        var name = name.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
            .components(separatedBy: .controlCharacters).joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if name == "." || name == ".." { name = "" }
        guard name.count > limit else { return name }
        let ext = (name as NSString).pathExtension
        let stem = (name as NSString).deletingPathExtension
        guard !ext.isEmpty, ext.count < 16 else { return String(name.prefix(limit)) }
        return String(stem.prefix(limit - ext.count - 1)) + "." + ext
    }
}
