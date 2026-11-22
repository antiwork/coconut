import Foundation

/// The usage of one day, or the sum of some days. The Mac reads it from the session store of Hermes (`GET /v1/usage`).
struct UsageDay: Codable, Equatable, Sendable, Identifiable {
    /// The day in the time of the Mac, as "2026-11-16". Empty for a sum.
    var day = ""
    var turns = 0
    var modelCalls = 0
    var toolCalls = 0
    var workingSeconds = 0
    var inputTokens = 0
    var outputTokens = 0
    var cacheReadTokens = 0
    var cacheWriteTokens = 0
    var reasoningTokens = 0
    var costUsd = 0.0

    var id: String { day }
    /// All that the model read: the new input and the input that the provider had in its cache.
    var tokensIn: Int { inputTokens + cacheReadTokens + cacheWriteTokens }
    var tokens: Int { tokensIn + outputTokens }
    var isEmpty: Bool { turns == 0 && modelCalls == 0 && tokens == 0 && costUsd == 0 }

    static func + (left: UsageDay, right: UsageDay) -> UsageDay {
        UsageDay(day: left.day, turns: left.turns + right.turns, modelCalls: left.modelCalls + right.modelCalls,
                 toolCalls: left.toolCalls + right.toolCalls, workingSeconds: left.workingSeconds + right.workingSeconds,
                 inputTokens: left.inputTokens + right.inputTokens, outputTokens: left.outputTokens + right.outputTokens,
                 cacheReadTokens: left.cacheReadTokens + right.cacheReadTokens,
                 cacheWriteTokens: left.cacheWriteTokens + right.cacheWriteTokens,
                 reasoningTokens: left.reasoningTokens + right.reasoningTokens, costUsd: left.costUsd + right.costUsd)
    }
}

/// The tokens and the cost of one model.
struct UsageModel: Codable, Equatable, Sendable, Identifiable {
    var model: String
    var provider: String
    var inputTokens = 0
    var outputTokens = 0
    var cacheReadTokens = 0
    var cacheWriteTokens = 0
    var reasoningTokens = 0
    var costUsd = 0.0

    var id: String { provider + "\u{1f}" + model }
    var tokens: Int { inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens }
}

/// The cost of one chat with one model on one day. The Mac sends these rows for each agent, so a page can total the
/// spend by agent, by model and by chat for any period. A chat is the ID of its bot. An empty chat is a run that
/// no chat started, such as a routine.
struct UsageRow: Codable, Equatable, Sendable {
    var chat: String
    var model: String
    var day: String
    var costUsd = 0.0
    var tokens = 0
}

struct AgentUsage: Codable, Equatable, Sendable {
    /// Each day of the report, the oldest first.
    var days: [UsageDay]
    var models: [UsageModel] = []
    /// Hermes has a price for the models of this agent.
    var costKnown = false
    /// The cost comes from the price list of Hermes, not from a bill of the provider.
    var costEstimated = false
    /// The cost by chat, model and day. A Mac before this field sends none.
    var rows: [UsageRow]? = nil
}

struct UsageReport: Codable, Equatable, Sendable {
    var days: Int
    /// The current day of the Mac.
    var today: String
    var agents: [String: AgentUsage]
    /// The people of each team of the Mac with their usage, for the leaderboard. A Mac before this field sends none,
    /// and a Mac without a team an empty list.
    var teams: [UsageTeam]? = nil
}

/// The people of one team with the usage that each shares (`teams` of GET /v1/usage, backend/coconut/team_usage.py).
/// A Mac for a team only is no person, so it is not in the list.
struct UsageTeam: Codable, Equatable, Sendable, Identifiable {
    var room: String
    var name: String
    var people: [UsagePerson]

    var id: String { room }
}

/// One person of a team: the agents of their Mac summed by day. Each Mac shares its days in the team log at most every
/// 30 minutes; the person of this Mac has the numbers of now.
struct UsagePerson: Codable, Equatable, Sendable, Identifiable {
    var member: String
    var name: String
    var avatar: String? = nil
    var color: String? = nil
    var thisMac: Bool? = nil
    /// Nil until the Mac of the person shares its usage: an older Coconut.
    var days: [UsageDay]? = nil
    var costKnown: Bool? = nil
    var updatedAt: String? = nil

    var id: String { member }
    var isMe: Bool { thisMac == true }
}

/// One place of a leaderboard: a person of a team, or a project of this Mac, with its sum for a range.
struct UsageStanding: Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    /// The member of a person, for the face. A project has none: an agent has no face.
    var member: String? = nil
    var isMe = false
    /// The sum of the range. Nil while the person shares no usage.
    let total: UsageDay?
    var costKnown = false
    /// 1 for the most tokens. Equal tokens share a place. Nil without usage.
    var rank: Int? = nil

    var tokens: Int? { total?.tokens }
}

/// The leaderboard of the Usage page: the people of a team ranked by tokens, the measure that the team optimizes.
enum UsageLeaderboard {
    /// The places of the people of a team in a range, the most tokens first.
    static func people(_ team: UsageTeam, range: UsageRange, today: String, calendar: Calendar = .current) -> [UsageStanding] {
        ranked(team.people.map { person in
            UsageStanding(id: person.member, name: person.name, member: person.member, isMe: person.isMe,
                          total: person.days.map { UsageMath.sum(UsageMath.window($0, range: range, today: today, calendar: calendar)) },
                          costKnown: person.costKnown == true)
        })
    }

    /// The most tokens first, then by name. A place without usage comes last and has no number. Equal tokens share the
    /// place of the first of them, so the next place skips: 1, 1, 3.
    static func ranked(_ rows: [UsageStanding]) -> [UsageStanding] {
        let sorted = rows.sorted { first, second in
            switch (first.tokens, second.tokens) {
            case let (one?, two?) where one != two: return one > two
            case (nil, .some): return false
            case (.some, nil): return true
            default: return first.name.localizedStandardCompare(second.name) == .orderedAscending
            }
        }
        var result: [UsageStanding] = []
        for (index, row) in sorted.enumerated() {
            var row = row
            if let tokens = row.tokens {
                row.rank = result.last.flatMap { $0.tokens == tokens ? $0.rank : nil } ?? index + 1
            }
            result.append(row)
        }
        return result
    }

    /// The place of the person of this Mac in its first team for a range, and how many people have one there. Nil
    /// without a team, or while the Mac shares nothing.
    static func myPlace(_ report: UsageReport, range: UsageRange = .opening) -> (rank: Int, of: Int)? {
        guard let team = report.teams?.first(where: { $0.people.count > 1 }) else { return nil }
        let rows = people(team, range: range, today: report.today)
        guard let rank = rows.first(where: \.isMe)?.rank else { return nil }
        return (rank, rows.filter { $0.rank != nil }.count)
    }
}

/// The days that a usage page shows, and the size of one bar.
enum UsageRange: String, CaseIterable, Identifiable, Sendable {
    case week, month, quarter

    /// The range that the usage page opens on. The usage card of a project shows the same one, so a tap on the card
    /// leads to the same numbers.
    static let opening = UsageRange.month

    var id: String { rawValue }
    var days: Int {
        switch self {
        case .week: 7
        case .month: 30
        case .quarter: 84
        }
    }
    var title: String {
        switch self {
        case .week: "7 days"
        case .month: "30 days"
        case .quarter: "12 weeks"
        }
    }
    /// A bar is a week in the long range, else a day.
    var byWeek: Bool { self == .quarter }
}

enum UsageMetric: String, CaseIterable, Identifiable, Sendable {
    case tokens, cost, turns, tools, time

    var id: String { rawValue }
    var title: String {
        switch self {
        case .tokens: "Tokens"
        case .cost: "Spend"
        case .turns: "Turns"
        case .tools: "Tool calls"
        case .time: "Time working"
        }
    }

    func value(_ day: UsageDay) -> Double {
        switch self {
        case .tokens: Double(day.tokens)
        case .cost: day.costUsd
        case .turns: Double(day.turns)
        case .tools: Double(day.toolCalls)
        case .time: Double(day.workingSeconds)
        }
    }

    func text(_ value: Double) -> String {
        switch self {
        case .tokens: UsageMath.count(Int(value.rounded()))
        case .cost: UsageMath.cost(value)
        case .turns, .tools: UsageMath.count(Int(value.rounded()))
        case .time: UsageMath.duration(Int(value.rounded()))
        }
    }
}

/// One bar of a usage chart: a day, or a week from its first day.
struct UsageBucket: Identifiable, Equatable, Sendable {
    let start: Date
    var total: UsageDay

    var id: Date { start }
}

enum UsageMath {
    static func sum(_ days: [UsageDay]) -> UsageDay { days.reduce(UsageDay(), +) }

    /// The days of some agents as one list: each day has the sum of the agents, the oldest day first.
    static func combined(_ agents: [AgentUsage]) -> [UsageDay] {
        var result: [String: UsageDay] = [:]
        for day in agents.flatMap(\.days) {
            result[day.day] = (result[day.day] ?? UsageDay(day: day.day)) + day
        }
        return result.values.sorted { $0.day < $1.day }
    }

    /// The models of some agents: a model of two agents is one row. The largest cost first, then the most tokens.
    static func models(_ agents: [AgentUsage]) -> [UsageModel] {
        var result: [String: UsageModel] = [:]
        for model in agents.flatMap(\.models) {
            var sum = result[model.id] ?? UsageModel(model: model.model, provider: model.provider)
            sum.inputTokens += model.inputTokens
            sum.outputTokens += model.outputTokens
            sum.cacheReadTokens += model.cacheReadTokens
            sum.cacheWriteTokens += model.cacheWriteTokens
            sum.reasoningTokens += model.reasoningTokens
            sum.costUsd += model.costUsd
            result[model.id] = sum
        }
        return result.values.sorted { ($0.costUsd, $0.tokens, $1.id) > ($1.costUsd, $1.tokens, $0.id) }
    }

    static func date(_ day: String, calendar: Calendar = .current) -> Date? {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
    }

    static func day(_ date: Date, calendar: Calendar = .current) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// The last days of the range, ending today. A day that the Mac did not report is empty, so each range has all its days.
    static func window(_ days: [UsageDay], range: UsageRange, today: String, calendar: Calendar = .current) -> [UsageDay] {
        guard let end = date(today, calendar: calendar) else { return [] }
        let known = Dictionary(days.map { ($0.day, $0) }) { _, latest in latest }
        return (0..<range.days).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: end).map { date in
                let name = day(date, calendar: calendar)
                return known[name] ?? UsageDay(day: name)
            }
        }
    }

    /// The bars of the range: a bar for each day, or for each week of the calendar in the long range.
    static func buckets(_ days: [UsageDay], range: UsageRange, today: String, calendar: Calendar = .current) -> [UsageBucket] {
        var result: [UsageBucket] = []
        for day in window(days, range: range, today: today, calendar: calendar) {
            guard let date = date(day.day, calendar: calendar) else { continue }
            let start = range.byWeek ? calendar.dateInterval(of: .weekOfYear, for: date)?.start ?? date : date
            if let last = result.indices.last, result[last].start == start {
                result[last].total = result[last].total + day
            } else {
                var total = day
                total.day = UsageMath.day(start, calendar: calendar)
                result.append(UsageBucket(start: start, total: total))
            }
        }
        return result
    }

    /// "950", "12.4k", "3.2M", "1.1B".
    static func count(_ value: Int) -> String {
        let number = Double(value)
        for (limit, unit) in [(1e9, "B"), (1e6, "M"), (1e3, "k")] where abs(number) >= limit {
            let scaled = number / limit
            return String(format: scaled >= 100 ? "%.0f%@" : "%.1f%@", scaled, unit)
        }
        return "\(value)"
    }

    /// "$0.42", "$12.40", "$1,284".
    static func cost(_ value: Double) -> String {
        if value >= 1000 { return "$" + Int(value.rounded()).formatted(.number.locale(Locale(identifier: "en_US"))) }
        if value > 0, value < 0.01 { return "<$0.01" }
        return String(format: "$%.2f", value)
    }

    /// "45s", "12m", "3h 05m".
    static func duration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m" }
        return String(format: "%dh %02dm", minutes / 60, minutes % 60)
    }
}

extension UsageReport {
    /// The usage of the preview: each agent has work on the weekdays, and the first agent has the most. As on the Mac,
    /// an agent has the usage of its further chats too, and each row names its chat.
    static func preview(bots: [Bot], days: Int = 84, now: Date = Date(), calendar: Calendar = .current) -> UsageReport {
        var agents: [String: AgentUsage] = [:]
        for (index, bot) in bots.filter({ !$0.isChat }).enumerated() {
            let weight = Double(max(1, 4 - index))
            let list = (0..<days).reversed().map { offset -> UsageDay in
                let date = calendar.date(byAdding: .day, value: -offset, to: now) ?? now
                let weekday = calendar.component(.weekday, from: date)
                // A fixed wave, so two runs of the preview show the same bars.
                let wave = 0.55 + 0.45 * abs(sin(Double(offset * (index + 3)) * 0.9))
                let busy = (weekday == 1 || weekday == 7 ? 0.15 : 1) * wave * weight
                let turns = Int((busy * 9).rounded())
                return UsageDay(day: UsageMath.day(date, calendar: calendar), turns: turns, modelCalls: turns * 11,
                                toolCalls: turns * 9, workingSeconds: turns * 410, inputTokens: turns * 5_200,
                                outputTokens: turns * 7_900, cacheReadTokens: turns * 410_000, cacheWriteTokens: turns * 21_000,
                                reasoningTokens: 0, costUsd: Double(turns) * 0.42)
            }
            let total = UsageMath.sum(list)
            let models = [UsageModel(model: "claude-opus-5-5", provider: "Anthropic", inputTokens: total.inputTokens,
                                     outputTokens: total.outputTokens, cacheReadTokens: total.cacheReadTokens,
                                     cacheWriteTokens: total.cacheWriteTokens, costUsd: total.costUsd * 0.86),
                          UsageModel(model: "claude-haiku-5", provider: "Anthropic", inputTokens: total.inputTokens / 6,
                                     outputTokens: total.outputTokens / 9, costUsd: total.costUsd * 0.14)]
            // The first chat of the agent has three parts of four of each day, and its further chats share the rest.
            let further = bots.filter { $0.chatOf == bot.id }.map(\.id)
            var shares: [(chat: String, part: Double)] = [(chat: bot.id, part: further.isEmpty ? 1 : 0.75)]
            shares += further.map { (chat: $0, part: 0.25 / Double(further.count)) }
            let rows = list.flatMap { day in
                shares.flatMap { share in
                    [UsageRow(chat: share.chat, model: "claude-opus-5-5", day: day.day, costUsd: day.costUsd * 0.86 * share.part,
                              tokens: Int(Double(day.tokens) * 0.8 * share.part)),
                     UsageRow(chat: share.chat, model: "claude-haiku-5", day: day.day, costUsd: day.costUsd * 0.14 * share.part,
                              tokens: Int(Double(day.tokens) * 0.2 * share.part))]
                }
            }.filter { $0.costUsd > 0 }
            agents[bot.id] = AgentUsage(days: list, models: models, costKnown: true, costEstimated: true, rows: rows)
        }
        // The team of the preview (AppStore.applyTeamPreview): Ada is the person of this Mac, with the sum of the agents
        // above, and each other person has the same weeks at another size, so the leaderboard has a clear order.
        let mine = UsageMath.combined(Array(agents.values))
        func scaled(_ factor: Double, weeks: Int) -> [UsageDay] {
            mine.indices.map { index in
                let source = mine[(index + weeks * 7) % mine.count]
                func times(_ value: Int) -> Int { Int((Double(value) * factor).rounded()) }
                return UsageDay(day: mine[index].day, turns: times(source.turns), modelCalls: times(source.modelCalls),
                                toolCalls: times(source.toolCalls), workingSeconds: times(source.workingSeconds),
                                inputTokens: times(source.inputTokens), outputTokens: times(source.outputTokens),
                                cacheReadTokens: times(source.cacheReadTokens), cacheWriteTokens: times(source.cacheWriteTokens),
                                reasoningTokens: 0, costUsd: source.costUsd * factor)
            }
        }
        let people = [
            UsagePerson(member: "m2", name: "Grace", days: scaled(1.38, weeks: 1), costKnown: true),
            UsagePerson(member: "m1", name: "Ada", thisMac: true, days: mine, costKnown: true),
            UsagePerson(member: "m3", name: "Lin", days: scaled(0.64, weeks: 2), costKnown: true),
            UsagePerson(member: "m4", name: "Omar", days: scaled(0.41, weeks: 3), costKnown: true),
            UsagePerson(member: "m5", name: "Priya", days: scaled(0.23, weeks: 4), costKnown: true),
        ]
        return UsageReport(days: days, today: UsageMath.day(now, calendar: calendar), agents: agents,
                           teams: [UsageTeam(room: "gumroad", name: "Gumroad", people: people)])
    }
}
