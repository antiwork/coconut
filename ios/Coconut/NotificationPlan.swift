import Foundation

/// What notifies outside work hours. The Mac applies it; see `push_schedule.py`.
enum OutsideWorkHours: String, CaseIterable, Sendable {
    /// Urgent cards notify at once. Everything else waits for work hours.
    case urgent
    /// Nothing notifies. Urgent cards also wait for work hours.
    case nothing
    /// No quiet time: every hour is like work hours.
    case same

    static let key = "outsideWorkHours"

    var title: String {
        switch self {
        case .urgent: "Only urgent"
        case .nothing: "Nothing"
        case .same: "Same as work hours"
        }
    }

    /// The value for the Mac. "Same as work hours" turns the schedule off, so the Mac keeps "urgent" for it.
    var registrable: String { self == .nothing ? "nothing" : "urgent" }

    /// The saved choice. Before this choice, the "afterHoursImportantOnly" switch turned work hours on or off.
    static func load(from defaults: UserDefaults = .standard) -> OutsideWorkHours {
        if let saved = defaults.string(forKey: key), let value = OutsideWorkHours(rawValue: saved) { return value }
        return defaults.object(forKey: "afterHoursImportantOnly") as? Bool == false ? .same : .urgent
    }
}

/// How many cards of the last week were urgent, and the last urgent cards. A Mac before this read sends none.
struct UrgentSummary: Decodable, Equatable, Sendable {
    struct Example: Decodable, Equatable, Sendable {
        let title: String?
        let botName: String?
        let createdAt: String?
    }
    let weekUrgent: Int
    let weekTotal: Int
    let examples: [Example]

    var weekText: String { "Last week: \(weekUrgent) urgent of \(weekTotal) \(weekTotal == 1 ? "card" : "cards")" }
}

/// The plain sentence at the top of the notification settings. It says when Coconut interrupts the person.
enum NotificationSentence {
    static let urgentExplanation = "A bot marks a card urgent when it needs your answer soon, for example when waiting "
        + "until your next work hours would cause harm or a missed deadline."

    /// "9:00" for 540 minutes.
    static func time(_ minutes: Int) -> String {
        let day = ((minutes % 1440) + 1440) % 1440
        return String(format: "%d:%02d", day / 60, day % 60)
    }

    /// "weekdays" or "Mon, Wed", or nil for every day.
    static func days(_ weekdays: [Int]) -> String? {
        let summary = NotificationSchedule.summary(of: weekdays)
        if summary == "Every day" { return nil }
        return summary == "Weekdays" || summary == "Weekends" ? summary.lowercased() : summary
    }

    /// The row that opens the work hours editor, for example "9:00–18:00, every day".
    static func workHours(_ schedule: NotificationSchedule, currentTimeZone: String = TimeZone.current.identifier) -> String {
        var text = "\(time(NotificationSchedule.minutes(fromClock: schedule.start) ?? 0))–"
            + "\(time(NotificationSchedule.minutes(fromClock: schedule.end) ?? 0)), \(days(schedule.weekdays) ?? "every day")"
        if schedule.timezone != currentTimeZone {
            text += ", " + (schedule.timezone.split(separator: "/").last.map(String.init) ?? schedule.timezone)
                .replacingOccurrences(of: "_", with: " ") + " time"
        }
        return text
    }

    /// `team` is true when a message of at least one team notifies this phone (`TeamRooms.notifiesPhone`).
    static func text(level: NotificationLevel, outside: OutsideWorkHours, replies: Bool, team: Bool = false,
                     schedule: NotificationSchedule) -> String {
        // Only urgent turns bot replies and team messages off on the Mac.
        let replies = replies && level != .urgentOnly
        let team = team && level != .urgentOnly
        if outside == .same || (level == .urgentOnly && outside == .urgent) {
            return during(level, replies: replies, team: team, from: "At any time")
        }
        let start = time(NotificationSchedule.minutes(fromClock: schedule.start) ?? 0)
        let end = time(NotificationSchedule.minutes(fromClock: schedule.end) ?? 0)
        let days = days(schedule.weekdays)
        let window = "From \(start) to \(end)" + (days.map { " on \($0)" } ?? "")
        let until = "until \(start)" + (days == nil ? "" : " on the next work day")
        // A team message does not wait in Inbox.
        let wait = "wait" + (team ? "" : " in Inbox") + " \(until), then one notification tells you about them."
        let rest: String
        switch (outside, level) {
        case (.nothing, .urgentOnly):
            rest = "Outside those hours, nothing notifies you. Urgent cards wait \(until), then one notification tells you about them."
        case (.nothing, _):
            rest = "Outside those hours, nothing notifies you. "
                + list(["Cards", replies ? "bot replies" : nil, team ? "team messages" : nil]) + " " + wait
        default:
            rest = "Outside those hours, only urgent cards notify you. "
                + list(["Other cards", replies ? "bot replies" : nil, team ? "team messages" : nil]) + " " + wait
        }
        return during(level, replies: replies, team: team, from: window) + " " + rest
    }

    /// "a", "a and b", or "a, b and c". The summary of the Mac also has no comma before "and".
    private static func list(_ items: [String?]) -> String {
        let items = items.compactMap { $0 }
        guard let last = items.last, items.count > 1 else { return items.first ?? "" }
        return items.dropLast().joined(separator: ", ") + " and " + last
    }

    private static func during(_ level: NotificationLevel, replies: Bool, team: Bool, from window: String) -> String {
        switch level {
        case .every:
            "\(window), Coconut notifies you about "
                + list(["every card", replies ? "every bot reply" : nil, team ? "every team message" : nil]) + "."
        case .grouped:
            "\(window), Coconut sends one summary at a time about "
                + list(["new cards", replies ? "bot replies" : nil, team ? "team messages" : nil]) + ". "
                + "Urgent cards notify you at once."
        case .urgentOnly:
            "\(window), only urgent cards notify you. Other cards wait in Inbox without a notification."
        }
    }
}
