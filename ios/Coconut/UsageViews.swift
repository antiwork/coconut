import Charts
import SwiftUI

/// The usage page of one agent, or of all agents without an ID.
struct UsageRoute: Hashable {
    var botId: String? = nil
}

/// What a usage page shows for a range: the bars and their sum.
struct UsageSummary: Equatable {
    let buckets: [UsageBucket]
    let total: UsageDay
    let costKnown: Bool

    init(agents: [AgentUsage], range: UsageRange, today: String, calendar: Calendar = .current) {
        buckets = UsageMath.buckets(UsageMath.combined(agents), range: range, today: today, calendar: calendar)
        total = UsageMath.sum(buckets.map(\.total))
        costKnown = agents.contains(where: \.costKnown)
    }

    /// "$12.40", or a dash when Hermes has no price for the models.
    var cost: String { costKnown ? UsageMath.cost(total.costUsd) : "\u{2014}" }
    /// The one note of the cost: why the number is a dash.
    var costNote: String? { costKnown ? nil : "No price" }
}

extension AppStore {
    /// The usage of one agent, or of each agent that is not deleted.
    func usageOf(_ botId: String?) -> [AgentUsage] {
        guard let usage else { return [] }
        if let botId { return usage.agents[botId].map { [$0] } ?? [] }
        return bots.compactMap { usage.agents[$0.id] }
    }
}

/// One number of a usage page with its name over it. Data only: a line under the number says why it did not load, and
/// nothing else.
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

/// The bars of a usage page: one number for each day or week, so the chart needs no legend.
struct UsageChart: View {
    let buckets: [UsageBucket]
    let metric: UsageMetric
    let byWeek: Bool
    /// The small chart of the card of a project has its dates and no axis of values.
    var compact = false

    var body: some View {
        Chart {
            ForEach(buckets) { bucket in
                bar(bucket.start, metric.value(bucket.total), metric.title).foregroundStyle(BoardStyle.green)
            }
        }
        .chartLegend(.hidden)
        .chartYAxis(compact ? .hidden : .visible)
        .chartXAxis {
            AxisMarks(values: labelDates) { _ in
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

    /// The bars that get a date: each fifth part of the range from its start. A date has the width of that part, so the
    /// edge of the chart cuts none: a bar near the end has no date.
    private var labelDates: [Date] {
        let step = max(1, (buckets.count + 4) / 5)
        return buckets.indices.filter { $0 % step == 0 && buckets.count - $0 >= step }.map { buckets[$0].start }
    }
}

/// The usage of a project on its page: today, then the last 30 days in four numbers and as small bars of tokens over
/// their dates, so the bars say which days they are. A tap opens the usage page of the project on the same range.
struct AgentUsageCard: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    let botId: String

    var body: some View {
        BoardSection("Usage") {
            Text("Last " + UsageRange.opening.title)
        } rows: {
            if let report = store.usage, let usage = report.agents[botId] {
                let range = UsageSummary(agents: [usage], range: .opening, today: report.today)
                if range.total.isEmpty {
                    BoardEmptyRow(text: "No usage in the last " + UsageRange.opening.title + ".")
                } else {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                        Grid(alignment: .leading, horizontalSpacing: BoardStyle.Space.m, verticalSpacing: BoardStyle.Space.m) {
                            #if os(macOS)
                            GridRow {
                                today(usage, report.today)
                                turns(range)
                                tokens(range)
                                cost(range)
                                time(range)
                            }
                            #else
                            GridRow {
                                today(usage, report.today)
                                cost(range)
                            }
                            GridRow {
                                turns(range)
                                tokens(range)
                            }
                            GridRow {
                                time(range)
                                Color.clear.frame(height: 0)
                            }
                            #endif
                        }
                        HStack(alignment: .center, spacing: BoardStyle.Space.m) {
                            UsageChart(buckets: range.buckets, metric: .tokens, byWeek: false, compact: true).frame(height: 60)
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }
                    }
                    .padding(BoardStyle.Space.m)
                    .contentShape(Rectangle())
                    .boardButton("agent-usage") { pushRoute?(UsageRoute(botId: botId)) }
                }
            } else {
                BoardEmptyRow(text: store.usageIssue ?? "Loading usage")
            }
        }
        .task(id: store.connection == .connected) { await store.loadUsage() }
    }

    /// What the agent cost today.
    private func today(_ usage: AgentUsage, _ today: String) -> UsageStat {
        let day = usage.days.first { $0.day == today } ?? UsageDay(day: today)
        return UsageStat(title: "Today", value: usage.costKnown ? UsageMath.cost(day.costUsd) : "\u{2014}")
    }

    private func turns(_ range: UsageSummary) -> UsageStat {
        UsageStat(title: "Turns", value: UsageMath.count(range.total.turns))
    }

    private func tokens(_ range: UsageSummary) -> UsageStat {
        UsageStat(title: "Tokens", value: UsageMath.count(range.total.tokens))
    }

    private func cost(_ range: UsageSummary) -> UsageStat {
        UsageStat(title: UsageRange.opening.title, value: range.cost, detail: range.costNote)
    }

    private func time(_ range: UsageSummary) -> UsageStat {
        UsageStat(title: "Time working", value: UsageMath.duration(range.total.workingSeconds))
    }
}

/// The usage page: the numbers of a range, a chart by day or by week, each agent, and each model. Data only: the bar
/// has the title, a number has its name, the chart has its axis values, and a note shows only for a number that did
/// not load.
struct UsageView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    var botId: String? = nil
    @State private var range = UsageRange.opening
    @State private var metric = UsageMetric.tokens

    private var bot: Bot? { store.bots.first { $0.id == botId } }

    var body: some View {
        BoardPage {
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
                }
                .padding(BoardStyle.Space.m)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                if botId == nil { agentRows(report) }
                let models = UsageMath.models(agents)
                if !models.isEmpty {
                    // The Mac has one total of each model for the whole report, so the title names its length, in the
                    // words of the range above ("12 weeks").
                    let length = UsageRange.allCases.first { $0.days == report.days }?.title ?? "\(report.days) days"
                    BoardSection("Models · " + length, count: models.count) {
                        ForEach(models) { model in
                            BoardRow(title: store.modelCatalog?.models.first { $0.id == model.model }?.name ?? model.model,
                                     detail: model.provider.isEmpty ? nil : model.provider,
                                     value: UsageMath.count(model.tokens) + " tokens" + (summary.costKnown ? " · " + UsageMath.cost(model.costUsd) : ""),
                                     chevron: false)
                        }
                    }
                }
            } else if let issue = store.usageIssue {
                BoardLoadNotice(title: "Usage unavailable", message: issue) { Task { await store.loadUsage(force: true) } }
            } else {
                BoardLoading(title: "Loading usage")
            }
        }
        .navigationTitle(bot.map { $0.name + " usage" } ?? "Usage").inlineNavigationTitle()
        .task(id: store.connection == .connected) { await store.loadUsage() }
        .refreshable { await store.loadUsage(force: true) }
    }

    private func stats(_ summary: UsageSummary) -> some View {
        let total = summary.total
        let tiles = [
            UsageStat(title: "Turns", value: UsageMath.count(total.turns)),
            UsageStat(title: "Tokens in", value: UsageMath.count(total.tokensIn)),
            UsageStat(title: "Tokens out", value: UsageMath.count(total.outputTokens)),
            UsageStat(title: "Spend", value: summary.cost, detail: summary.costNote),
            UsageStat(title: "Time working", value: UsageMath.duration(total.workingSeconds)),
            UsageStat(title: "Tool calls", value: UsageMath.count(total.toolCalls)),
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
        // The list of the Projects page, so it has that name.
        BoardSection("Projects", count: rows.count) {
            if rows.isEmpty { BoardEmptyRow(text: "No work in this range.") }
            ForEach(rows, id: \.0.id) { bot, summary in
                HStack(spacing: BoardStyle.Space.m) {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        Text(bot.name).lineLimit(1)
                        // The whole line where it fits. A narrow row leaves the time out, so no number is cut.
                        let counts = "\(UsageMath.count(summary.total.turns)) turns · \(UsageMath.count(summary.total.tokens)) tokens"
                        ViewThatFits(in: .horizontal) {
                            Text(counts + " · " + UsageMath.duration(summary.total.workingSeconds))
                            Text(counts)
                        }
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
}
