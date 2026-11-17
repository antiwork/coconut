import Foundation
import Security

// The chats that a share can go to. The app writes the list when its chats change, and the share extension reads it,
// so the extension shows the chats at once and without a request to the Mac.

/// One chat that takes a share: the chat of an agent, a chat of a team, or a direct chat.
struct ShareDestination: Codable, Identifiable, Hashable, Sendable {
    enum Kind: String, Codable, Sendable {
        case agent, team, direct

        /// The title of the section of the kind in the list.
        var section: String {
            switch self {
            case .agent: "Agents"
            case .team: "Teams"
            case .direct: "Direct messages"
            }
        }
    }

    var kind: Kind
    /// The bot ID of an agent chat.
    var bot: String? = nil
    /// The room of a chat of a team or of a direct chat.
    var room: String? = nil
    /// The ID of a named chat of a team. Nil for the Team chat.
    var chat: String? = nil
    /// The other side of a direct chat: the member ID of a person, or the team bot ID of a bot.
    var peer: String? = nil
    /// True when the other side of a direct chat is a bot of the team.
    var peerIsBot: Bool? = nil
    var title: String
    /// The line under the title: the team of a named chat or of a direct chat.
    var detail: String? = nil
    /// The face of the chat as the app draws it, as a PNG. Nil shows the letter of the title.
    var face: Data? = nil

    var id: String {
        switch kind {
        case .agent: "agent-" + (bot ?? "")
        case .team: "team-" + (room ?? "") + (chat.map { "-" + $0 } ?? "")
        case .direct: "direct-" + (room ?? "") + "-" + (peer ?? "")
        }
    }

    /// A direct chat takes no file in this version (TeamFileRules.offered).
    var takesFiles: Bool { kind != .direct }

    /// The route of the Mac that takes a message of the chat, under `/v1/`. Nil for a chat without its IDs, or with
    /// an ID that cannot be a part of a path.
    var messagesPath: String? { chatPath.map { $0 + "/messages" } }

    /// The route of the Mac that keeps a file of the next message of a chat of a team. Nil for each other chat.
    var filesPath: String? { kind == .team ? chatPath.map { $0 + "/files" } : nil }

    private var chatPath: String? {
        func safe(_ id: String?) -> String? {
            guard let id, !id.isEmpty, id.count <= 100,
                  id.unicodeScalars.allSatisfy({ $0.isASCII && (CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_") })
            else { return nil }
            return id
        }
        switch kind {
        case .agent: return safe(bot).map { "bots/" + $0 }
        case .team: return safe(room).map { "team/rooms/" + $0 }
        case .direct:
            guard let room = safe(room), let peer = safe(peer) else { return nil }
            return "team/rooms/\(room)/directs/\(peer)"
        }
    }
}

/// The list of the app for the share extension.
struct ShareDirectory: Codable, Equatable, Sendable {
    /// The relay route of the paired Mac. It holds no secret: the keys of the pairing stay in the Keychain items of
    /// the app. Nil on a Mac, where the app sends the share itself.
    struct Endpoint: Codable, Equatable, Sendable {
        var relay: String
        var route: String
        var macKey: String
    }

    var destinations: [ShareDestination] = []
    /// The chat that a share starts with: the agent that the person wrote to last, else the Coconut chat.
    var defaultId: String? = nil
    var endpoint: Endpoint? = nil
    /// The name of the paired Mac, for the texts of the extension.
    var macName: String? = nil

    /// The chat that the sheet shows first: the chat of the last share, then the choice of the app, then the first
    /// agent, then the first chat. A chat that is gone is skipped. With files, a direct chat is skipped too.
    func initial(lastUsed: String?, needsFiles: Bool) -> ShareDestination? {
        let offered = destinations.filter { !needsFiles || $0.takesFiles }
        for id in [lastUsed, defaultId] {
            if let id, let match = offered.first(where: { $0.id == id }) { return match }
        }
        return offered.first { $0.kind == .agent } ?? offered.first
    }

    /// The chats in the sections of the list: agents, teams, direct messages. A section without a chat is left out.
    /// `query` keeps the chats whose title or team has it.
    func sections(matching query: String = "") -> [(kind: ShareDestination.Kind, items: [ShareDestination])] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return [ShareDestination.Kind.agent, .team, .direct].compactMap { kind in
            let items = destinations.filter { item in
                item.kind == kind && (query.isEmpty || item.title.localizedCaseInsensitiveContains(query)
                                      || (item.detail ?? "").localizedCaseInsensitiveContains(query))
            }
            return items.isEmpty ? nil : (kind, items)
        }
    }
}

/// Where the list lives between the app and its share extension.
///
/// On an iPhone it is one Keychain item in the group that the app shares with its extensions
/// (`CoconutPushKeychainGroup`, as NotificationKeys), so the extension needs no app group. On a Mac it is a file in
/// the folder of the app, which the sandbox of the extension may read (CoconutMacShare.entitlements).
enum ShareDirectoryStore {
    #if os(macOS)
    static func load() -> ShareDirectory? {
        guard let data = try? Data(contentsOf: ShareFolder.url.appendingPathComponent("directory.json")) else { return nil }
        return try? JSONDecoder().decode(ShareDirectory.self, from: data)
    }

    static func save(_ directory: ShareDirectory?) {
        let file = ShareFolder.url.appendingPathComponent("directory.json")
        guard let directory, let data = try? JSONEncoder().encode(directory) else {
            try? FileManager.default.removeItem(at: file)
            return
        }
        try? ShareFolder.create(ShareFolder.url)
        try? data.write(to: file, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
    #else
    static func load() -> ShareDirectory? {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(ShareDirectory.self, from: data)
    }

    static func save(_ directory: ShareDirectory?) {
        guard let directory, let data = try? JSONEncoder().encode(directory) else {
            SecItemDelete(query as CFDictionary)
            return
        }
        // The share sheet opens only on an unlocked iPhone.
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
            SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
    }

    private static var query: [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                     kSecAttrService as String: TestIsolation.name("com.gumroad.coconut.share"),
                                     kSecAttrAccount as String: "directory"]
        if let group = Bundle.main.object(forInfoDictionaryKey: "CoconutPushKeychainGroup") as? String, !group.isEmpty {
            result[kSecAttrAccessGroup as String] = group
        }
        return result
    }
    #endif
}

/// The chat of the last share. The extension keeps it in its own defaults.
enum ShareLastUsed {
    static let key = "shareLastDestination"
    static func read(_ defaults: UserDefaults = .standard) -> String? { defaults.string(forKey: key) }
    static func save(_ id: String, _ defaults: UserDefaults = .standard) { defaults.set(id, forKey: key) }
}
