import Charts
import SwiftUI

/// The usage page of one agent, or of all agents without an ID.
struct UsageRoute: Hashable {
    var botId: String? = nil
}

/// What a usage page shows for a range: the bars, their sum, and the words under them.
struct UsageSummary: Equatable {
    let buckets: [UsageBucket]
    let total: UsageDay
    let costKnown: Bool
    let costEstimated: Bool

    init(agents: [AgentUsage], range: UsageRange, today: String, calendar: Calendar = .current) {
        buckets = UsageMath.buckets(UsageMath.combined(agents), range: range, today: today, calendar: calendar)
        total = UsageMath.sum(buckets.map(\.total))
        costKnown = agents.contains(where: \.costKnown)
        costEstimated = agents.contains(where: \.costEstimated)
    }

    /// "$12.40", or a dash when Hermes has no price for the models.
    var cost: String { costKnown ? UsageMath.cost(total.costUsd) : "\u{2014}" }
    var costDetail: String { !costKnown ? "No price known" : costEstimated ? "Estimate at list prices" : "Billed by the provider" }
}

extension AppStore {
    /// The usage of one agent, or of each agent that is not deleted.
    func usageOf(_ botId: String?) -> [AgentUsage] {
        guard let usage else { return [] }
        if let botId { return usage.agents[botId].map { [$0] } ?? [] }
        return bots.compactMap { usage.agents[$0.id] }
    }
}

/// One number of a usage page with its name over it and a quiet line under it.
struct UsageStat: View {
    let title: String
    let value: String
    var detail: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Text(value).font(.title3.weight(.semibold).monospacedDigit()).lineLimit(1).minimumScaleFactor(0.7)
            if let detail { Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.8) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// The bars of a usage page. Tokens show the input and the output as two parts of a bar.
struct UsageChart: View {
    let buckets: [UsageBucket]
    let metric: UsageMetric
    let byWeek: Bool
    /// The small chart of the card of an agent has no axes.
    var compact = false

    var body: some View {
        Chart {
            ForEach(buckets) { bucket in
                if metric == .tokens {
                    bar(bucket.start, Double(bucket.total.tokensIn), "In").foregroundStyle(BoardStyle.ink.opacity(0.7))
                    bar(bucket.start, Double(bucket.total.outputTokens), "Out").foregroundStyle(BoardStyle.accent)
                } else {
                    bar(bucket.start, metric.value(bucket.total), metric.title).foregroundStyle(BoardStyle.ink.opacity(0.7))
                }
            }
        }
        .chartLegend(.hidden)
        .chartXAxis(compact ? .hidden : .visible)
        .chartYAxis(compact ? .hidden : .visible)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 6)) { _ in
                AxisValueLabel(format: .dateTime.month(.abbreviated).day())
            }
        }
        .chartYAxis {
            AxisMarks(values: .automatic(desiredCount: 4)) { value in
                AxisGridLine().foregroundStyle(BoardStyle.line)
                AxisValueLabel { if let number = value.as(Double.self) { Text(metric.text(number)) } }
            }
        }
        .accessibilityLabel(metric.title + (byWeek ? " by week" : " by day"))
    }

    private func bar(_ start: Date, _ value: Double, _ series: String) -> some ChartContent {
        BarMark(x: .value(byWeek ? "Week" : "Day", start, unit: byWeek ? .weekOfYear : .day),
                y: .value(series, value))
            .cornerRadius(compact ? 1.5 : 2.5)
    }
}

/// The usage of an agent on its page: the last 7 days in four numbers and a small chart of 30 days. A tap opens the
/// usage page of the agent.
struct AgentUsageCard: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    let botId: String

    var body: some View {
        BoardSection("Usage") {
            Text("Last 7 days")
        } rows: {
            if let report = store.usage, let usage = report.agents[botId] {
                let week = UsageSummary(agents: [usage], range: .week, today: report.today)
                let month = UsageMath.buckets(usage.days, range: .month, today: report.today)
                if UsageMath.sum(month.map(\.total)).isEmpty {
                    BoardEmptyRow(text: "No usage in the last 30 days.")
                } else {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                        Grid(alignment: .leading, horizontalSpacing: BoardStyle.Space.m, verticalSpacing: BoardStyle.Space.m) {
                            #if os(macOS)
                            GridRow {
                                today(usage, report.today)
                                turns(week)
                                tokens(week)
                                cost(week)
                                time(week)
                            }
                            #else
                            GridRow {
                                today(usage, report.today)
                                cost(week)
                            }
                            GridRow {
                                turns(week)
                                tokens(week)
                            }
                            GridRow {
                                time(week)
                                Color.clear.frame(height: 0)
                            }
                            #endif
                        }
                        HStack(alignment: .bottom, spacing: BoardStyle.Space.m) {
                            UsageChart(buckets: month, metric: .tokens, byWeek: false, compact: true).frame(height: 40)
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }
                        Text("Tokens by day, last 30 days").font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(BoardStyle.Space.m)
                    .contentShape(Rectangle())
                    .boardButton("agent-usage") { pushRoute?(UsageRoute(botId: botId)) }
                }
            } else {
                BoardEmptyRow(text: store.usageIssue ?? "Loading usage from your Mac.")
            }
        }
        .task(id: store.connection == .connected) { await store.loadUsage() }
    }

    /// What the agent used today: the cost and the tokens.
    private func today(_ usage: AgentUsage, _ today: String) -> UsageStat {
        let day = usage.days.first { $0.day == today } ?? UsageDay(day: today)
        return UsageStat(title: "Today", value: usage.costKnown ? UsageMath.cost(day.costUsd) : "\u{2014}",
                         detail: UsageMath.count(day.tokens) + " tokens")
    }

    private func turns(_ week: UsageSummary) -> UsageStat {
        UsageStat(title: "Turns", value: UsageMath.count(week.total.turns), detail: UsageMath.count(week.total.toolCalls) + " tool calls")
    }

    private func tokens(_ week: UsageSummary) -> UsageStat {
        UsageStat(title: "Tokens", value: UsageMath.count(week.total.tokens),
                  detail: UsageMath.count(week.total.tokensIn) + " in · " + UsageMath.count(week.total.outputTokens) + " out")
    }

    private func cost(_ week: UsageSummary) -> UsageStat {
        UsageStat(title: "7 days", value: week.cost, detail: week.costDetail)
    }

    private func time(_ week: UsageSummary) -> UsageStat {
        UsageStat(title: "Time working", value: UsageMath.duration(week.total.workingSeconds),
                  detail: UsageMath.count(week.total.modelCalls) + " model calls")
    }
}

/// The usage page: the numbers of a range, a chart by day or by week, each agent, and each model.
struct UsageView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    var botId: String? = nil
    @State private var range = UsageRange.month
    @State private var metric = UsageMetric.tokens

    private var bot: Bot? { store.bots.first { $0.id == botId } }

    var body: some View {
        BoardPage {
            BoardPageTitle(title: bot.map { $0.name + " usage" } ?? "Usage",
                           detail: botId == nil ? "All agents on your Mac" : "Turns, tokens, cost and time of this agent")
            if let report = store.usage {
                let agents = store.usageOf(botId)
                let summary = UsageSummary(agents: agents, range: range, today: report.today)
                chips(UsageRange.allCases, selected: range, title: \.title) { range = $0 }
                    .accessibilityIdentifier("usage-range")
                stats(summary)
                VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                    chips(UsageMetric.allCases, selected: metric, title: \.title) { metric = $0 }
                        .accessibilityIdentifier("usage-metric")
                    UsageChart(buckets: summary.buckets, metric: metric, byWeek: range.byWeek).frame(height: 220)
                    HStack(spacing: BoardStyle.Space.m) {
                        if metric == .tokens {
                            legend("In", BoardStyle.ink.opacity(0.7))
                            legend("Out", BoardStyle.accent)
                        }
                        Text(range.byWeek ? "By week" : "By day")
                    }
                    .font(.caption).foregroundStyle(.secondary)
                }
                .padding(BoardStyle.Space.m)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                if botId == nil { agentRows(report) }
                let models = UsageMath.models(agents)
                if !models.isEmpty {
                    BoardSection("Models · last \(report.days) days", count: models.count) {
                        ForEach(models) { model in
                            BoardRow(symbol: "cpu", title: store.modelCatalog?.models.first { $0.id == model.model }?.name ?? model.model,
                                     detail: model.provider.isEmpty ? nil : model.provider,
                                     value: UsageMath.count(model.tokens) + " tokens" + (summary.costKnown ? " · " + UsageMath.cost(model.costUsd) : ""),
                                     chevron: false)
                        }
                    }
                }
                Text("Hermes records turns, tool calls and time by message, and tokens and cost by session. A day gets the share of a session that equals its share of the model calls."
                     + (summary.costEstimated ? " Cost is an estimate at list prices, not a bill." : ""))
                    .font(.footnote).foregroundStyle(.secondary)
            } else if let issue = store.usageIssue {
                QuietEmptyState(symbol: "chart.bar", title: "No usage yet", message: issue)
            } else {
                ProgressView().frame(maxWidth: .infinity).padding(.vertical, 70)
            }
        }
        .navigationTitle("Usage").inlineNavigationTitle()
        .task(id: store.connection == .connected) { await store.loadUsage() }
        .refreshable { await store.loadUsage(force: true) }
    }

    private func stats(_ summary: UsageSummary) -> some View {
        let total = summary.total
        let tiles = [
            UsageStat(title: "Turns", value: UsageMath.count(total.turns), detail: UsageMath.count(total.modelCalls) + " model calls"),
            UsageStat(title: "Tokens in", value: UsageMath.count(total.tokensIn), detail: UsageMath.cached(total)),
            UsageStat(title: "Tokens out", value: UsageMath.count(total.outputTokens),
                      detail: total.reasoningTokens > 0 ? UsageMath.count(total.reasoningTokens) + " reasoning" : nil),
            UsageStat(title: "Cost", value: summary.cost, detail: summary.costDetail),
            UsageStat(title: "Time working", value: UsageMath.duration(total.workingSeconds),
                      detail: total.turns > 0 ? UsageMath.duration(total.workingSeconds / total.turns) + " per turn" : nil),
            UsageStat(title: "Tool calls", value: UsageMath.count(total.toolCalls),
                      detail: total.turns > 0 ? String(format: "%.1f per turn", Double(total.toolCalls) / Double(total.turns)) : nil),
        ]
        // Three numbers in a row of a Mac window, two on an iPhone, so each row is full.
        #if os(macOS)
        let columns = 3
        #else
        let columns = 2
        #endif
        return LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: BoardStyle.Space.m, alignment: .topLeading), count: columns),
                         alignment: .leading, spacing: BoardStyle.Space.l) {
            ForEach(tiles.indices, id: \.self) { tiles[$0] }
        }
        .padding(BoardStyle.Space.m)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        .accessibilityIdentifier("usage-stats")
    }

    /// Each agent with its usage in the range, the largest value of the chosen number first.
    @ViewBuilder private func agentRows(_ report: UsageReport) -> some View {
        let rows = store.bots.compactMap { bot -> (Bot, UsageSummary)? in
            guard let usage = report.agents[bot.id] else { return nil }
            let summary = UsageSummary(agents: [usage], range: range, today: report.today)
            return summary.total.isEmpty ? nil : (bot, summary)
        }.sorted { (metric.value($0.1.total), $1.0.name) > (metric.value($1.1.total), $0.0.name) }
        BoardSection("Agents", count: rows.count) {
            if rows.isEmpty { BoardEmptyRow(text: "No agent worked in this range.") }
            ForEach(rows, id: \.0.id) { bot, summary in
                HStack(spacing: BoardStyle.Space.m) {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        Text(bot.name).lineLimit(1)
                        Text("\(UsageMath.count(summary.total.turns)) turns · \(UsageMath.count(summary.total.tokens)) tokens · \(UsageMath.duration(summary.total.workingSeconds))")
                            .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: BoardStyle.Space.s)
                    Text(metric == .cost || metric == .tokens ? summary.cost : metric.text(metric.value(summary.total)))
                        .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
                .contentShape(Rectangle())
                .boardButton("usage-agent-" + bot.id) { pushRoute?(UsageRoute(botId: bot.id)) }
            }
        }
    }

    private func chips<Value: Identifiable & Equatable>(_ values: [Value], selected: Value, title: KeyPath<Value, String>,
                                                        choose: @escaping (Value) -> Void) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: BoardStyle.Space.xs) {
                ForEach(values) { value in
                    ChipButton(title: value[keyPath: title], selected: value == selected) { choose(value) }
                }
            }
        }
    }

    private func legend(_ title: String, _ color: Color) -> some View {
        HStack(spacing: BoardStyle.Space.xs) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(title)
        }
    }
}
