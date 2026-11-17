#if os(macOS)
import Foundation

// The share extension of the Mac hands a share to the app, and the app sends it. The app keeps its pairing in the
// login Keychain, which the sandbox of an extension cannot read, so the extension cannot send through the relay
// itself. Both sides read and write one folder of the app: the sandbox of the extension has an exception for it
// (CoconutMacShare.entitlements).

enum ShareFolder {
    /// `~/Library/Application Support/Coconut/Share`. In a sandbox, the home of Foundation is the container, so the
    /// path comes from the user record.
    static var url: URL {
        let home = getpwuid(getuid()).flatMap { $0.pointee.pw_dir }.map { String(cString: $0) } ?? NSHomeDirectory()
        return URL(fileURLWithPath: home, isDirectory: true)
            .appendingPathComponent("Library/Application Support", isDirectory: true)
            .appendingPathComponent(TestIsolation.name("Coconut"), isDirectory: true)
            .appendingPathComponent("Share", isDirectory: true)
    }

    static var inbox: URL { url.appendingPathComponent("inbox", isDirectory: true) }

    /// Only the person reads the folder: a share can hold a private file.
    static func create(_ folder: URL) throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
    }
}

/// One share on its way from the extension to the app: the chat, the text, and the names of its files. The bytes of
/// file `n` are in the file `file-n` next to it.
struct ShareHandoff: Codable, Equatable, Sendable {
    var id: String
    var destination: ShareDestination
    var text: String
    var fileNames: [String]
    var createdAt: Date
}

enum ShareInbox {
    /// The link that starts the app, or brings it to the front, after the extension wrote a share.
    static let wake = URL(string: "coconut://share")!
    /// A share that the app could not send for this long is dropped, so an old share never goes out days later.
    static let keeps: TimeInterval = 24 * 3600

    static func isWake(_ url: URL) -> Bool { url.scheme == "coconut" && url.host() == "share" }

    /// Writes a share as one folder. The folder gets its name only when each file is in it, so the app never reads
    /// half a share.
    @discardableResult
    static func write(_ message: ShareMessage, to destination: ShareDestination, in inbox: URL = ShareFolder.inbox,
                      id: String = UUID().uuidString, now: Date = .now) throws -> ShareHandoff {
        var destination = destination
        destination.face = nil
        let handoff = ShareHandoff(id: id, destination: destination, text: message.text,
                                   fileNames: message.files.map(\.name), createdAt: now)
        try ShareFolder.create(inbox)
        let draft = inbox.appendingPathComponent("." + id, isDirectory: true)
        try? FileManager.default.removeItem(at: draft)
        try ShareFolder.create(draft)
        do {
            for (index, file) in message.files.enumerated() {
                try file.data.write(to: draft.appendingPathComponent("file-\(index)"))
            }
            try encoder.encode(handoff).write(to: draft.appendingPathComponent("handoff.json"))
            try FileManager.default.moveItem(at: draft, to: inbox.appendingPathComponent(id, isDirectory: true))
        } catch {
            try? FileManager.default.removeItem(at: draft)
            throw error
        }
        return handoff
    }

    /// The shares that wait for the app, oldest first. A share that is too old, and a folder that is no share, are
    /// removed. A folder that the extension still writes has a name with a dot and stays.
    static func pending(in inbox: URL = ShareFolder.inbox, now: Date = .now) -> [ShareHandoff] {
        let folders = (try? FileManager.default.contentsOfDirectory(at: inbox, includingPropertiesForKeys: nil)) ?? []
        var found: [ShareHandoff] = []
        for folder in folders where !folder.lastPathComponent.hasPrefix(".") {
            guard let data = try? Data(contentsOf: folder.appendingPathComponent("handoff.json")),
                  let handoff = try? decoder.decode(ShareHandoff.self, from: data),
                  handoff.id == folder.lastPathComponent, now.timeIntervalSince(handoff.createdAt) < keeps else {
                try? FileManager.default.removeItem(at: folder)
                continue
            }
            found.append(handoff)
        }
        return found.sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
    }

    /// The message of a share with the bytes of its files, or nil when a file is gone.
    static func message(of handoff: ShareHandoff, in inbox: URL = ShareFolder.inbox) -> ShareMessage? {
        let folder = inbox.appendingPathComponent(handoff.id, isDirectory: true)
        var files: [SharedFile] = []
        for (index, name) in handoff.fileNames.enumerated() {
            guard let data = try? Data(contentsOf: folder.appendingPathComponent("file-\(index)")) else { return nil }
            files.append(SharedFile(data: data, name: name))
        }
        return ShareMessage(text: handoff.text, files: files)
    }

    static func remove(_ handoff: ShareHandoff, in inbox: URL = ShareFolder.inbox) {
        try? FileManager.default.removeItem(at: inbox.appendingPathComponent(handoff.id, isDirectory: true))
    }

    private static var encoder: JSONEncoder { let value = JSONEncoder(); value.dateEncodingStrategy = .iso8601; return value }
    private static var decoder: JSONDecoder { let value = JSONDecoder(); value.dateDecodingStrategy = .iso8601; return value }
}
#endif
