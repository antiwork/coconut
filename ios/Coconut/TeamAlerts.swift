import Foundation

// Alerts in a chat of a team (docs/team-log.md, "Alerts"). An alert is a message of an agent with a type and a level.
// The Mac sends both with the message, and it counts the alerts of a chat for the filter. A Mac without alerts sends
// neither, and the chat then looks as before.

/// How loud an alert is. The sender chooses it. `notice` is the level of an alert without one.
enum TeamAlertLevel: String, CaseIterable, Sendable {
    case info, notice, critical

    var title: String {
        switch self {
        case .info: "Info"
        case .notice: "Notice"
        case .critical: "Critical"
        }
    }

    /// The symbol of the level. A critical alert has a filled symbol, so its form differs, not only its color.
    var symbol: String {
        switch self {
        case .info: "info.circle"
        case .notice: "bell"
        case .critical: "exclamationmark.triangle.fill"
        }
    }
}

/// The alert of a message: the type that the sender chose, and the level.
struct TeamAlert: Decodable, Equatable, Sendable {
    var type: String
    var level: TeamAlertLevel

    /// An alert that does not fit the rules decodes to an error, and its message then shows as a plain message.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decode(String.self, forKey: .type)
        let level = TeamAlertLevel(rawValue: try container.decode(String.self, forKey: .level))
        guard Self.fits(type), let level else {
            throw DecodingError.dataCorruptedError(forKey: .type, in: container, debugDescription: "No alert.")
        }
        self.type = type
        self.level = level
    }

    private enum CodingKeys: String, CodingKey { case type, level }

    /// The rule of a type: 1 to 32 characters of `a` to `z`, `0` to `9`, `-` and `_`.
    static func fits(_ type: String) -> Bool {
        (1...32).contains(type.utf8.count) && type.utf8.allSatisfy { byte in
            (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte) || (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
                || byte == UInt8(ascii: "-") || byte == UInt8(ascii: "_")
        }
    }

    /// What VoiceOver reads for the label of the alert.
    var spoken: String { "\(level.title) alert, \(type)" }
}

/// The alerts of one chat, from the newest page of the Mac: how many, how many critical, and the count of each type,
/// the type with the most alerts first. Nil from a Mac without alerts.
struct TeamAlertSummary: Decodable, Equatable, Sendable {
    struct TypeCount: Decodable, Equatable, Identifiable, Sendable {
        var type: String
        var count: Int
        var critical: Int

        var id: String { type }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            type = try container.decode(String.self, forKey: .type)
            count = try container.decode(Int.self, forKey: .count)
            critical = (try? container.decodeIfPresent(Int.self, forKey: .critical)) ?? 0
        }

        private enum CodingKeys: String, CodingKey { case type, count, critical }
    }

    var total: Int
    var critical: Int
    var types: [TypeCount]

    /// A type that the app cannot read drops only that type.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        total = try container.decode(Int.self, forKey: .total)
        critical = (try? container.decodeIfPresent(Int.self, forKey: .critical)) ?? 0
        types = ((try? container.decodeIfPresent([LossyTypeCount].self, forKey: .types)) ?? nil)?
            .compactMap(\.value).filter { TeamAlert.fits($0.type) && $0.count > 0 } ?? []
    }

    private enum CodingKeys: String, CodingKey { case total, critical, types }

    private struct LossyTypeCount: Decodable {
        let value: TypeCount?
        init(from decoder: Decoder) throws { value = try? TypeCount(from: decoder) }
    }
}

/// What the person chose in the filter of a chat: one type or each type, and all levels or only critical. It lives in
/// the app only: no Mac and no other device gets it, and it moves no read position.
struct TeamAlertFilter: Hashable, Sendable {
    /// The type to show. Nil shows each type.
    var type: String? = nil
    /// Only the critical alerts.
    var criticalOnly = false

    static let all = TeamAlertFilter()

    /// "All": the chat shows each message, also the ones that are no alert.
    var isAll: Bool { type == nil && !criticalOnly }

    /// The part of the query of a page that names the filter, or nothing for "All".
    var query: String {
        (type.map { "&alert_type=" + $0 } ?? "") + (criticalOnly ? "&alert_level=" + TeamAlertLevel.critical.rawValue : "")
    }

    func matches(_ message: TeamMessage) -> Bool {
        guard !isAll else { return true }
        guard let alert = message.alert else { return false }
        return (type.map { $0 == alert.type } ?? true) && (!criticalOnly || alert.level == .critical)
    }

    /// The filter after a tap on the chip of a type: that type, and a second tap gives each type again.
    func choosing(_ type: String) -> TeamAlertFilter {
        TeamAlertFilter(type: self.type == type ? nil : type, criticalOnly: criticalOnly)
    }

    /// The filter after a tap on "Critical".
    var togglingCritical: TeamAlertFilter { TeamAlertFilter(type: type, criticalOnly: !criticalOnly) }
}

/// One choice of the filter bar.
struct TeamAlertChip: Identifiable, Equatable, Sendable {
    enum Kind: Equatable, Sendable { case all, type(String), critical }

    let kind: Kind
    let title: String
    /// The count next to the title. Nil for "All": the Mac counts alerts, not messages.
    let count: Int?
    let selected: Bool
    /// The filter that a tap on the chip chooses.
    let filter: TeamAlertFilter

    var id: String {
        switch kind {
        case .all: "all"
        case .type(let type): "type-" + type
        case .critical: "critical"
        }
    }

    /// The count as the chip shows it.
    var countText: String? { count.map { $0 > 999 ? "999+" : String($0) } }

    var spoken: String {
        switch kind {
        case .all: "All messages"
        case .type(let type): "\(type), \(count ?? 0) " + ((count ?? 0) == 1 ? "alert" : "alerts")
        case .critical: "Critical only, \(count ?? 0) " + ((count ?? 0) == 1 ? "alert" : "alerts")
        }
    }
}

/// The rules of the filter of a chat.
enum TeamAlertFilters {
    /// The bar shows only in a chat with at least one alert. A chat without one, and a Mac without alerts, show none.
    static func shows(_ summary: TeamAlertSummary?) -> Bool { (summary?.total ?? 0) > 0 && summary?.types.isEmpty == false }

    /// The filter that the chat uses: the choice of the person while the bar offers it, else "All". A type that the
    /// chat no longer has, and "Critical" in a chat without a critical alert, go from the choice.
    static func effective(_ chosen: TeamAlertFilter, summary: TeamAlertSummary?) -> TeamAlertFilter {
        guard shows(summary), let summary else { return .all }
        return TeamAlertFilter(type: chosen.type.flatMap { type in summary.types.contains { $0.type == type } ? type : nil },
                               criticalOnly: chosen.criticalOnly && summary.critical > 0)
    }

    /// The chips of the bar: "All", "Critical" when the chat has a critical alert, and one for each type with its
    /// count. "Critical" stands next to "All", so it stays in view when the types need a scroll. With "Critical" on,
    /// the count of a type is the count of its critical alerts, and a type without a critical alert has no chip,
    /// unless it is the chosen type.
    static func chips(_ summary: TeamAlertSummary?, filter: TeamAlertFilter) -> [TeamAlertChip] {
        guard shows(summary), let summary else { return [] }
        var chips = [TeamAlertChip(kind: .all, title: "All", count: nil, selected: filter.isAll, filter: .all)]
        if summary.critical > 0 {
            let count = filter.type.flatMap { type in summary.types.first { $0.type == type }?.critical } ?? summary.critical
            chips.append(TeamAlertChip(kind: .critical, title: TeamAlertLevel.critical.title, count: count,
                                       selected: filter.criticalOnly, filter: filter.togglingCritical))
        }
        chips += summary.types.compactMap { item in
            let count = filter.criticalOnly ? item.critical : item.count
            guard count > 0 || filter.type == item.type else { return nil }
            return TeamAlertChip(kind: .type(item.type), title: item.type, count: count,
                                 selected: filter.type == item.type, filter: filter.choosing(item.type))
        }
        return chips
    }

    /// The chat of a new choice before its first page arrives: the alerts of the choice that the app already has, so
    /// the chat shows them at once. `loaded` stays false until the Mac answers.
    static func start(from chat: TeamChat?, filter: TeamAlertFilter) -> TeamChat {
        TeamChat(messages: (chat?.messages ?? []).filter(filter.matches), before: nil)
    }

    /// The chat that the list shows for a choice: the alerts of the choice, with the changes of this person that are
    /// on their way in the whole chat (an edit, a removal, a reaction). The messages on their way are no alerts, so
    /// they do not show.
    static func listed(_ filtered: TeamChat, over chat: TeamChat, filter: TeamAlertFilter) -> TeamChat {
        var listed = filtered
        listed.messages = filtered.messages.filter { filter.matches($0) && !chat.removed.contains($0.id) }
        listed.pending = []
        listed.editing = chat.editing
        listed.deleting = chat.deleting
        listed.reacting = chat.reacting
        listed.sentRows = [:]
        return listed
    }

    /// The identity of the list of the chat. "All" keeps the identity of the chat without a filter, and each other
    /// choice has its own, so the list of a choice starts at its bottom.
    static func listIdentity(_ window: String?, filter: TeamAlertFilter) -> String? {
        filter.isAll ? window : "alerts|\(filter.type ?? "")|\(filter.criticalOnly)|\(window ?? "")"
    }

    /// The line of a choice that has no alert.
    static let empty = "No alerts match this filter."
}
