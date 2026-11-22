import Foundation
import Security

// The chats that a share can go to. The app writes the list when its chats change, and the share extension reads it,
// so the extension shows the chats at once and without a request to the Mac.

/// One place that takes a share: a project, where the share starts a new chat, the chat of an agent, a chat of a
/// team, or a direct chat.
struct ShareDestination: Codable, Identifiable, Hashable, Sendable {
    enum Kind: String, Codable, Sendable {
        case project, agent, team, direct

        /// The title of the section of the kind in the list.
        var section: String {
            switch self {
            case .project: "Projects"
            case .agent: "Agents"
            case .team: "Teams"
            case .direct: "Direct messages"
            }
        }
    }

    var kind: Kind
    /// The bot ID of an agent chat, or the ID of a project.
    var bot: String? = nil
    /// The agent of an agent chat: its project, or the agent itself. A share to a chat counts for its project in the
    /// suggestions of the share sheet (ShareDirectory.suggestion).
    var project: String? = nil
    /// The room of a chat of a team or of a direct chat.
    var room: String? = nil
    /// The ID of a named chat of a team. Nil for the Team chat.
    var chat: String? = nil
    /// The other side of a direct chat: the member ID of a person, or the team bot ID of a bot.
    var peer: String? = nil
    /// True when the other side of a direct chat is a bot of the team.
    var peerIsBot: Bool? = nil
    var title: String
    /// The line under the title: "New agent" for a project (AgentChats.untitled), the project of an agent chat, the team
    /// of a named chat or of a direct chat.
    var detail: String? = nil
    /// The face of the chat as the app draws it, as a PNG. Nil shows the letter of the title.
    var face: Data? = nil

    var id: String {
        switch kind {
        case .project: "project-" + (bot ?? "")
        case .agent: "agent-" + (bot ?? "")
        case .team: "team-" + (room ?? "") + (chat.map { "-" + $0 } ?? "")
        case .direct: "direct-" + (room ?? "") + "-" + (peer ?? "")
        }
    }

    /// A direct chat takes no file in this version (TeamFileRules.offered).
    var takesFiles: Bool { kind != .direct }

    /// The requests of a share with `files` files: one for each file and one for the message, and one more for the
    /// new chat of a project.
    func steps(files: Int) -> Int { files + (kind == .project ? 2 : 1) }

    /// The route of the Mac that takes a message of the chat, under `/v1/`. Nil for a project, whose chat comes first
    /// (newChatPath), and for a chat without its IDs, or with an ID that cannot be a part of a path.
    var messagesPath: String? { chatPath.map { $0 + "/messages" } }

    /// The route of the Mac that keeps a file of the next message of a chat of a team. Nil for each other chat.
    var filesPath: String? { kind == .team ? chatPath.map { $0 + "/files" } : nil }

    /// The route of the Mac that makes a new chat of a project, for a share to the project. Nil for each other chat.
    var newChatPath: String? { kind == .project ? Self.safe(bot).map { "bots/" + $0 + "/chats" } : nil }

    /// The agent chat that the Mac made for a share to this project.
    func newChat(_ bot: String) -> ShareDestination {
        ShareDestination(kind: .agent, bot: bot, project: self.bot, title: title)
    }

    private var chatPath: String? {
        switch kind {
        case .project: return nil
        case .agent: return Self.safe(bot).map { "bots/" + $0 }
        case .team: return Self.safe(room).map { "team/rooms/" + $0 }
        case .direct:
            guard let room = Self.safe(room), let peer = Self.safe(peer) else { return nil }
            return "team/rooms/\(room)/directs/\(peer)"
        }
    }

    private static func safe(_ id: String?) -> String? {
        guard let id, !id.isEmpty, id.count <= 100,
              id.unicodeScalars.allSatisfy({ $0.isASCII && (CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_") })
        else { return nil }
        return id
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
    /// The place that a share starts with: a new chat of the project that the person wrote to last, else of the
    /// Coconut project.
    var defaultId: String? = nil
    var endpoint: Endpoint? = nil
    /// The name of the paired Mac, for the texts of the extension.
    var macName: String? = nil

    /// The place that the sheet shows first: the chat that the person tapped in the row of people of the share sheet
    /// (`suggested`), then the place of the last share, then the choice of the app, then the first project, agent or
    /// chat. A place that is gone is skipped. With files, a direct chat is skipped too.
    func initial(suggested: String? = nil, lastUsed: String?, needsFiles: Bool) -> ShareDestination? {
        let offered = destinations.filter { !needsFiles || $0.takesFiles }
        for id in [suggested, lastUsed, defaultId] {
            if let id, let match = offered.first(where: { $0.id == id }) { return match }
        }
        return offered.first { $0.kind == .project } ?? offered.first { $0.kind == .agent } ?? offered.first
    }

    /// The place that a share to `destination` counts for in the row of people of the share sheet: the project of an
    /// agent chat, else the first chat of its agent, else the place itself. A chat of a project goes to Archived after
    /// two days, and its project stays.
    func suggestion(for destination: ShareDestination) -> ShareDestination {
        guard destination.kind == .agent, let agent = destination.project else { return destination }
        return destinations.first { $0.kind == .project && $0.bot == agent }
            ?? destinations.first { $0.kind == .agent && $0.bot == agent } ?? destination
    }

    /// The places in the sections of the list: projects, chats, teams, direct messages. A section without a place is
    /// left out. `query` keeps the places whose title or detail has it.
    func sections(matching query: String = "") -> [(kind: ShareDestination.Kind, items: [ShareDestination])] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return [ShareDestination.Kind.project, .agent, .team, .direct].compactMap { kind in
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

/// The place of the last share. The extension keeps it in its own defaults. The key is new since a share to a project
/// starts a new chat: the old key could name the first chat of a project, and a share would go there again.
enum ShareLastUsed {
    static let key = "shareLastPlace"
    static func read(_ defaults: UserDefaults = .standard) -> String? { defaults.string(forKey: key) }
    static func save(_ id: String, _ defaults: UserDefaults = .standard) { defaults.set(id, forKey: key) }
}
