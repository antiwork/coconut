import Foundation
import Security

// What Coconut puts on the Home Screen and the Lock Screen (CoconutWidgets): the cards that wait for the person, the
// agents that work with the step that each one does now, the chats with unread messages, and the to-dos of the three
// lists. The person asked to "bring the most important thing to the Home Screen and lock screen. Saving taps and more".
// The app writes it at each change (HomeScreenPublisher), and the widgets read it, so they show at once and ask the Mac
// nothing.

/// The state of Coconut that the widgets and the Live Activity show.
struct CoconutGlance: Codable, Hashable, Sendable {
    /// A card that waits for the person, in the chat that holds it.
    struct Card: Codable, Hashable, Identifiable, Sendable {
        var id: String
        var title: String
        /// The chat of the card, and its name in a list.
        var chat: String
        var name: String
        var createdAt: Date

        var link: HomeScreenLink { .chat(chat, card: id) }
    }

    /// An agent that works, with the step that it does now.
    struct Work: Codable, Hashable, Identifiable, Sendable {
        /// The chat.
        var id: String
        var name: String
        /// The step as the row of the chat names it ("Running a command"), and its command, file or query.
        var step: String
        var detail: String? = nil
        /// When the step started. The time next to it counts from there, as in the app (StepTimeLabel): a web search
        /// that began 23 seconds ago shows 23 seconds, however long the turn has run.
        var startedAt: Date? = nil

        var link: HomeScreenLink { .chat(id, card: nil) }
    }

    /// A chat with unread messages: an agent chat, or a chat of a team.
    struct Unread: Codable, Hashable, Identifiable, Sendable {
        var id: String
        var name: String
        var preview: String
        var count: Int
        var date: Date
        var link: HomeScreenLink
    }

    /// The open to-dos of one list, as the badge of its tab counts them (TodoCounts).
    struct Todos: Codable, Hashable, Identifiable, Sendable {
        /// "finance", "support" or "product", the name of the tab.
        var id: String
        var title: String
        var count: Int
        /// How many of them are past their service level and red in the list (TodoCounts.late). The widgets show the
        /// count of such a list in red too.
        var late = 0

        var link: HomeScreenLink { .tab(id) }
    }

    var cards: [Card] = []
    var working: [Work] = []
    var unread: [Unread] = []
    /// Every unread message of every chat, also of the chats that `unread` leaves out (the badge of Chats).
    var unreadCount = 0
    var todos: [Todos] = []
    /// When the app wrote it. A widget says when it is old, because the app writes only while it runs.
    var updatedAt: Date
    /// True for the glance that the Mac sent after the person left the app (MacGlance), which a widget saved.
    var fromMac: Bool? = nil

    /// The rows of a list, the most important first: the cards that wait, the agents that work, then the unread chats.
    /// A chat shows once, in its most important row.
    var rows: [Row] {
        var seen = Set<String>()
        var rows: [Row] = []
        for card in cards where seen.insert(card.chat).inserted { rows.append(.card(card)) }
        for work in working where seen.insert(work.id).inserted { rows.append(.work(work)) }
        for chat in unread where seen.insert(chat.id).inserted { rows.append(.unread(chat)) }
        return rows
    }

    /// True when nothing waits for the person and nothing works.
    var isQuiet: Bool { cards.isEmpty && working.isEmpty }

    /// One row of a list of the widgets.
    enum Row: Hashable, Identifiable, Sendable {
        case card(Card)
        case work(Work)
        case unread(Unread)

        var id: String {
            switch self {
            case .card(let card): "card-" + card.id
            case .work(let work): "work-" + work.id
            case .unread(let chat): "unread-" + chat.id
            }
        }

        var link: HomeScreenLink {
            switch self {
            case .card(let card): card.link
            case .work(let work): work.link
            case .unread(let chat): chat.link
            }
        }
    }

    /// The most cards, agents and chats that the glance keeps: more than the large widget shows.
    static let limit = 6
    /// The kind of the widget that shows it: NowPushWidget on iOS 26, which the Mac can reload with a push, and NowWidget
    /// before iOS 26, which has a kind of its own. The app asks WidgetKit to read both again at each change.
    static let widgetKind = "coconut.now"
    static let widgetKindBefore26 = "coconut.now.ios18"
    static let widgetKinds = [widgetKind, widgetKindBefore26]
}

extension CoconutGlance.Todos {
    /// A glance that an older app wrote has no `late`, and none of its to-dos is late.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        title = try values.decode(String.self, forKey: .title)
        count = try values.decode(Int.self, forKey: .count)
        late = try values.decodeIfPresent(Int.self, forKey: .late) ?? 0
    }
}

/// A place of the app that a widget, the Live Activity or a control opens: `coconut://open/...`. The app opens it as a
/// tap on its row in the app would (CoconutHome.openHomeScreenLink).
enum HomeScreenLink: Codable, Hashable, Sendable {
    /// An agent chat, at its card when it has one.
    case chat(String, card: String?)
    /// A chat of a team: the Team chat, a named chat or a direct chat (TeamChatKey).
    case team(room: String, member: String?, chat: String?, bot: String?)
    /// Chats with its Working filter on: every chat that works.
    case working
    /// The message box of Chats, with the keyboard up.
    case newAgent
    /// A tab: "finance", "support", "product" or "chats".
    case tab(String)

    static let scheme = "coconut"
    static let host = "open"

    var url: URL {
        var parts = URLComponents()
        parts.scheme = Self.scheme
        parts.host = Self.host
        var items: [URLQueryItem] = []
        switch self {
        case .chat(let chat, let card):
            parts.path = "/chat/" + chat
            if let card { items.append(URLQueryItem(name: "card", value: card)) }
        case .team(let room, let member, let chat, let bot):
            parts.path = "/team/" + room
            items += [("member", member), ("chat", chat), ("bot", bot)].compactMap { name, value in
                value.map { URLQueryItem(name: name, value: $0) }
            }
        case .working: parts.path = "/working"
        case .newAgent: parts.path = "/new"
        case .tab(let tab): parts.path = "/tab/" + tab
        }
        parts.queryItems = items.isEmpty ? nil : items
        return parts.url ?? URL(string: "coconut://open/tab/chats")!
    }

    /// The place of a link of the widgets, or nil for every other link (a pairing link, an invite of a team).
    init?(url: URL) {
        guard url.scheme?.lowercased() == Self.scheme, url.host?.lowercased() == Self.host else { return nil }
        let path = url.pathComponents.filter { $0 != "/" }
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? { query.first { $0.name == name }?.value.flatMap { $0.isEmpty ? nil : $0 } }
        switch (path.first, path.count) {
        case ("chat", 2): self = .chat(path[1], card: value("card"))
        case ("team", 2): self = .team(room: path[1], member: value("member"), chat: value("chat"), bot: value("bot"))
        case ("working", 1): self = .working
        case ("new", 1): self = .newAgent
        case ("tab", 2): self = .tab(path[1])
        default: return nil
        }
    }
}

/// Where the glance lives between the app and its widgets: one Keychain item in the group that the app shares with its
/// extensions (`CoconutPushKeychainGroup`, as ShareDirectoryStore), so they need no app group. The item opens after the
/// first unlock, because the Lock Screen shows the widgets of a locked iPhone.
enum GlanceStore {
    static func load() -> CoconutGlance? {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        if SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data {
            return try? decoder.decode(CoconutGlance.self, from: data)
        }
        #if targetEnvironment(simulator)
        // A simulator build without a signature has no Keychain group (errSecMissingEntitlement), so the app and the
        // widgets of a simulator share a file of the simulated device instead.
        if let file = simulatorFile, let data = try? Data(contentsOf: file) {
            return try? decoder.decode(CoconutGlance.self, from: data)
        }
        #endif
        return nil
    }

    static func save(_ glance: CoconutGlance?) {
        guard let glance, let data = try? encoder.encode(glance) else {
            SecItemDelete(query as CFDictionary)
            #if targetEnvironment(simulator)
            if let file = simulatorFile { try? FileManager.default.removeItem(at: file) }
            #endif
            return
        }
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
            SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        #if targetEnvironment(simulator)
        if let file = simulatorFile { try? data.write(to: file, options: .atomic) }
        #endif
    }

    #if targetEnvironment(simulator)
    /// A file in the folder of the simulated device, which every process of that device sees.
    private static var simulatorFile: URL? {
        guard let folder = ProcessInfo.processInfo.environment["SIMULATOR_SHARED_RESOURCES_DIRECTORY"] else { return nil }
        return URL(fileURLWithPath: folder).appendingPathComponent(TestIsolation.name("coconut-home-screen") + ".json")
    }
    #endif

    private static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .secondsSince1970
        return encoder
    }

    private static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
        return decoder
    }

    private static var query: [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                     kSecAttrService as String: TestIsolation.name("com.gumroad.coconut.home-screen"),
                                     kSecAttrAccount as String: "glance"]
        if let group = Bundle.main.object(forInfoDictionaryKey: "CoconutPushKeychainGroup") as? String, !group.isEmpty {
            result[kSecAttrAccessGroup as String] = group
        }
        return result
    }
}

extension CoconutGlance {
    /// The glance of the widget gallery and of a widget before the app wrote one: the work of the demo (PreviewState.json).
    static func sample(now: Date = .now) -> CoconutGlance {
        CoconutGlance(
            cards: [Card(id: "sample-card", title: "Save the payout retry fix?", chat: "sample-coconut", name: "Coconut",
                         createdAt: now.addingTimeInterval(-18 * 60))],
            working: [Work(id: "sample-gumroad", name: "Gumroad", step: "Running a command",
                           detail: "bin/rails runner script/affected_sellers.rb --since friday",
                           startedAt: now.addingTimeInterval(-9))],
            unread: [Unread(id: "sample-personal", name: "Personal",
                            preview: "1,204 payouts went out. 7 failed: 5 sellers need to update a bank detail.", count: 1,
                            date: now.addingTimeInterval(-40 * 60), link: .chat("sample-personal", card: nil))],
            unreadCount: 1,
            todos: [Todos(id: "finance", title: "Finance", count: 3, late: 2),
                    Todos(id: "support", title: "Support", count: 5, late: 3),
                    Todos(id: "product", title: "Product", count: 12)],
            updatedAt: now)
    }
}
