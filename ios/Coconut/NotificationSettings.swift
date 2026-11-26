import Foundation

/// The notification settings of the person, the same on each device of the Mac (backend/coconut/notification_settings.py,
/// docs/team-log.md "Notifications"). The Mac follows them for the pushes of each iPhone, and the Mac app follows them
/// here for its banners (MessageBanners, AgentBanners).
struct NotificationSettings: Equatable, Sendable {
    /// What of the team notifies.
    enum People: String, Codable, CaseIterable, Sendable {
        case all, mentions, nothing

        var title: String {
            switch self {
            case .all: "All new messages"
            case .mentions: "Direct messages, mentions and keywords"
            case .nothing: "Nothing"
            }
        }
    }

    /// What of the agents notifies. A card that needs attention now always does.
    enum Agents: String, Codable, CaseIterable, Sendable {
        case all, cards, urgent

        var title: String {
            switch self {
            case .all: "Cards and replies"
            case .cards: "Cards"
            case .urgent: "Urgent cards only"
            }
        }
    }

    /// The choices of the menu that pauses notifications.
    enum Pause: CaseIterable, Sendable {
        case thirtyMinutes, hour, twoHours, tomorrow, nextWeek

        var title: String {
            switch self {
            case .thirtyMinutes: "30 minutes"
            case .hour: "1 hour"
            case .twoHours: "2 hours"
            case .tomorrow: "Until tomorrow"
            case .nextWeek: "Until next week"
            }
        }

        /// The end of the pause. Tomorrow and next week start at 9:00 here, next week on Monday.
        func end(from now: Date, calendar: Calendar = .current) -> Date {
            let tomorrow = calendar.startOfDay(for: calendar.date(byAdding: .day, value: 1, to: now) ?? now)
            switch self {
            case .thirtyMinutes: return now.addingTimeInterval(30 * 60)
            case .hour: return now.addingTimeInterval(60 * 60)
            case .twoHours: return now.addingTimeInterval(2 * 60 * 60)
            case .tomorrow: return calendar.date(bySettingHour: 9, minute: 0, second: 0, of: tomorrow) ?? tomorrow
            case .nextWeek:
                return calendar.nextDate(after: tomorrow, matching: DateComponents(hour: 9, minute: 0, weekday: 2),
                                         matchingPolicy: .nextTime) ?? tomorrow
            }
        }
    }

    /// A time in UTC until which nothing notifies but an urgent card, or nil.
    var pausedUntil: String? = nil
    var people = People.all
    var agents = Agents.all
    /// Words that notify in a team chat as a mention does: whole words, in any case.
    var keywords: [String] = []
    /// The minutes without input at a Mac after which the iPhone notifies, or nil for at once.
    var mobileDelay: Int? = nil

    static let standard = NotificationSettings()
    static let demo = NotificationSettings(keywords: ["launch", "outage"], mobileDelay: 5)
    /// The choices of Notify my iPhone. The Mac takes 0 to 60 minutes (notification_settings.DELAY_MINUTES).
    static let delays: [Int?] = [nil, 1, 5, 15]

    var pauseEnd: Date? { pausedUntil?.apiDate }

    func isPaused(at now: Date = Date()) -> Bool { pauseEnd.map { $0 > now } ?? false }

    func paused(_ choice: Pause, now: Date = Date()) -> NotificationSettings {
        var settings = self
        settings.pausedUntil = ISO8601DateFormatter().string(from: choice.end(from: now))
        return settings
    }

    /// Whether a new message of a chat of a team notifies, as push.PushDelivery.team_notifies decides for the iPhone.
    /// `mention` is a mention of the person or one of the keywords, `plain` a message that the switches of the team
    /// and the chat let notify.
    func notifiesMessage(direct: Bool, mention: Bool, plain: Bool, at now: Date = Date()) -> Bool {
        guard !isPaused(at: now), people != .nothing else { return false }
        return direct ? plain : mention || (people == .all && plain)
    }

    func notifiesCard(urgent: Bool, at now: Date = Date()) -> Bool { urgent || (!isPaused(at: now) && agents != .urgent) }

    func notifiesReply(at now: Date = Date()) -> Bool { !isPaused(at: now) && agents == .all }

    /// True when the text has one of the keywords as a whole word, in any case (notification_settings.keyword_match).
    func matches(_ text: String?) -> Bool {
        guard let text, !text.isEmpty, !keywords.isEmpty else { return false }
        let words = keywords.map(NSRegularExpression.escapedPattern(for:)).joined(separator: "|")
        guard let pattern = try? NSRegularExpression(pattern: "(?<!\\w)(?:" + words + ")(?!\\w)", options: .caseInsensitive)
        else { return false }
        return pattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// The keywords of the field: separated by commas, trimmed, each one once in any case. The Mac keeps 30 of at most
    /// 40 characters (notification_settings.KEYWORDS), so the field keeps the first 30 and cuts a longer word.
    static func keywords(from text: String) -> [String] {
        var seen = Set<String>()
        let words = text.split(separator: ",").map { word in
            let scalars = word.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.prefix(keywordLength)
            return String(String.UnicodeScalarView(scalars)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return Array(words.filter { !$0.isEmpty && seen.insert($0.lowercased()).inserted }.prefix(keywordCount))
    }

    static let keywordCount = 30
    static let keywordLength = 40

    /// "Paused until 14:30", with the day when the pause ends on another day.
    static func pauseText(_ end: Date, now: Date = Date()) -> String {
        let calendar = Calendar.current
        let day = calendar.isDate(end, inSameDayAs: now) ? ""
            : end.formatted(.dateTime.weekday(.wide)) + " "
        return "Paused until " + day + end.formatted(date: .omitted, time: .shortened)
    }

    static func delayTitle(_ minutes: Int?) -> String {
        guard let minutes else { return "Even while I'm at my Mac" }
        return minutes <= 1 ? "When I'm away for 1 minute" : "When I'm away for \(minutes) minutes"
    }
}

extension NotificationSettings: Codable {
    private enum CodingKeys: String, CodingKey { case pausedUntil, people, agents, keywords, mobileDelay }

    /// A missing or unknown value takes its default, so a newer Mac with another choice keeps the settings readable.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init()
        pausedUntil = (try? container.decodeIfPresent(String.self, forKey: .pausedUntil)) ?? nil
        people = (try? container.decodeIfPresent(People.self, forKey: .people)) ?? .all
        agents = (try? container.decodeIfPresent(Agents.self, forKey: .agents)) ?? .all
        keywords = (try? container.decodeIfPresent([String].self, forKey: .keywords)) ?? []
        mobileDelay = (try? container.decodeIfPresent(Int.self, forKey: .mobileDelay)) ?? nil
    }

    /// Each key, an empty one as null: the Mac takes a null pause as resume and a null delay as at once.
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(pausedUntil, forKey: .pausedUntil)
        try container.encode(people, forKey: .people)
        try container.encode(agents, forKey: .agents)
        try container.encode(keywords, forKey: .keywords)
        try container.encode(mobileDelay, forKey: .mobileDelay)
    }
}
