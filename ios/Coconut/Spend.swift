import Foundation

/// The periods of the Spend page.
enum SpendPeriod: String, CaseIterable, Identifiable, Sendable {
    case today, week, month

    var id: String { rawValue }
    var days: Int {
        switch self {
        case .today: 1
        case .week: 7
        case .month: 30
        }
    }
    var title: String {
        switch self {
        case .today: "Today"
        case .week: "7 days"
        case .month: "30 days"
        }
    }
}

/// One share of the spend: an agent, a model or a chat, with its cost and its tokens.
struct SpendSlice: Identifiable, Equatable, Sendable {
    var id: String
    var title: String
    /// The agent whose face the row shows. Nil for a model.
    var botId: String? = nil
    /// The chat that a tap opens. Nil for an agent and a model.
    var chatId: String? = nil
    var cost = 0.0
    var tokens = 0
}

/// One bar of the chart of the daily cost: a day and the cost of one agent on it.
struct SpendDaily: Identifiable, Equatable, Sendable {
    var date: Date
    var series: String
    var cost: Double
    var id: String { UsageMath.day(date) + series }
}

/// The spend of the agents in a period, by agent, model and chat. It reads only its arguments.
struct SpendSummary: Equatable, Sendable {
    /// The most agents that the chart names. The others share one series.
    static let seriesLimit = 4
    static let other = "Other"
    /// The most rows of the chat list.
    static let chatLimit = 8

    var cost = 0.0
    var tokens = 0
    var costKnown = false
    var agents: [SpendSlice] = []
    var models: [SpendSlice] = []
    var chats: [SpendSlice] = []

    /// The days of a period, the current day of the Mac last.
    static func days(_ period: SpendPeriod, today: String, calendar: Calendar = .current) -> Set<String> {
        guard let end = UsageMath.date(today, calendar: calendar) else { return [] }
        return Set((0..<period.days).compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: end).map { UsageMath.day($0, calendar: calendar) }
        })
    }

    init() {}

    init(report: UsageReport, bots: [Bot], period: SpendPeriod, modelName: (String) -> String = { $0 },
         calendar: Calendar = .current) {
        let window = Self.days(period, today: report.today, calendar: calendar)
        var models: [String: SpendSlice] = [:]
        var chats: [String: SpendSlice] = [:]
        for (agentId, usage) in report.agents {
            let days = usage.days.filter { window.contains($0.day) }
            let sum = UsageMath.sum(days)
            costKnown = costKnown || usage.costKnown
            if sum.costUsd > 0 || sum.tokens > 0 {
                agents.append(SpendSlice(id: agentId, title: bots.name(of: agentId, fallback: "Deleted project"), botId: agentId,
                                         cost: sum.costUsd, tokens: sum.tokens))
                cost += sum.costUsd
                tokens += sum.tokens
            }
            for row in usage.rows ?? [] where window.contains(row.day) {
                var model = models[row.model] ?? SpendSlice(id: row.model, title: modelName(row.model))
                model.cost += row.costUsd
                model.tokens += row.tokens
                models[row.model] = model
                var chat = chats[row.chat] ?? Self.chat(row.chat, agent: agentId, bots: bots)
                chat.cost += row.costUsd
                chat.tokens += row.tokens
                chats[row.chat] = chat
            }
        }
        self.agents = Self.ranked(agents)
        self.models = Self.ranked(Array(models.values))
        self.chats = Self.ranked(Array(chats.values))
    }

    private static func chat(_ id: String, agent: String, bots: [Bot]) -> SpendSlice {
        guard !id.isEmpty else { return SpendSlice(id: "", title: "Routines", botId: agent) }
        guard let bot = bots.first(where: { $0.id == id }) else { return SpendSlice(id: id, title: "Deleted agent", botId: agent) }
        return SpendSlice(id: id, title: bot.listName, botId: bot.agentId, chatId: id)
    }

    /// The largest cost first, then the most tokens, then the title.
    static func ranked(_ slices: [SpendSlice]) -> [SpendSlice] {
        slices.sorted { ($0.cost, $0.tokens, $1.title) > ($1.cost, $1.tokens, $0.title) }
    }

    /// The cost of each day for the chart, by agent. The agents with the most cost have a series, the rest share
    /// "Other". The days of the window all have a bar, also a day without cost.
    static func daily(report: UsageReport, bots: [Bot], days count: Int = 30, calendar: Calendar = .current) -> [SpendDaily] {
        guard let end = UsageMath.date(report.today, calendar: calendar) else { return [] }
        let window = (0..<count).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: end)
        }
        let names = Set(window.map { UsageMath.day($0, calendar: calendar) })
        let totals = report.agents.mapValues { usage in
            usage.days.filter { names.contains($0.day) }.reduce(0) { $0 + $1.costUsd }
        }
        let top = totals.filter { $0.value > 0 }.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
            .prefix(seriesLimit).map(\.key)
        var points: [SpendDaily] = []
        for date in window {
            let name = UsageMath.day(date, calendar: calendar)
            var rest = 0.0
            for (agentId, usage) in report.agents.sorted(by: { $0.key < $1.key }) {
                let cost = usage.days.first { $0.day == name }?.costUsd ?? 0
                if top.contains(agentId) {
                    points.append(SpendDaily(date: date, series: bots.name(of: agentId, fallback: "Deleted project"), cost: cost))
                } else {
                    rest += cost
                }
            }
            if report.agents.count > top.count { points.append(SpendDaily(date: date, series: other, cost: rest)) }
        }
        return points
    }

    /// The names of the series of the chart, the largest first, "Other" last.
    static func series(_ points: [SpendDaily]) -> [String] {
        var totals: [String: Double] = [:]
        for point in points { totals[point.series, default: 0] += point.cost }
        let named = totals.filter { $0.key != other }.sorted { ($0.value, $1.key) > ($1.value, $0.key) }.map(\.key)
        return named + (totals[other] == nil ? [] : [other])
    }

    /// "$12.40", or the tokens when Hermes has no price for the models.
    func value(_ slice: SpendSlice) -> String { costKnown ? UsageMath.cost(slice.cost) : UsageMath.count(slice.tokens) }
}

extension Array where Element == Bot {
    func name(of id: String, fallback: String) -> String {
        first { $0.id == id }?.name ?? fallback
    }
}

extension AppStore {
    /// The cost of all agents today, or nil before the Mac reported.
    var spendTodayTotal: String? {
        guard let usage else { return nil }
        let costs = usage.agents.values.compactMap { agent in agent.days.first { $0.day == usage.today }?.costUsd }
        return UsageMath.cost(costs.reduce(0, +))
    }

    /// The cost of an agent today, as the rows of the agents show it. Nil before the Mac reported, and for an agent
    /// that cost nothing today or has no price. A chat shows the cost of its agent.
    func spendToday(agent botId: String) -> String? {
        guard let usage, let agent = usage.agents[AgentChats.agentId(of: botId, in: bots)], agent.costKnown,
              let today = agent.days.first(where: { $0.day == usage.today }), today.costUsd > 0 else { return nil }
        return UsageMath.cost(today.costUsd)
    }
}
