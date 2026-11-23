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
                AxisValueLabel { if let number = value.as(Double.self) { Text(metric.axis(number)) } }
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

/// The usage of a project on its page: today, then the last 30 days in four numbers and as small bars of spend over
/// their dates, so the bars say which days they are. The cost comes first and the tokens second; the bars are tokens
/// only when Hermes has no price for the models. A tap opens the usage page of the project on the same range.
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
                                cost(range)
                                tokens(range)
                                turns(range)
                                time(range)
                            }
                            #else
                            GridRow {
                                today(usage, report.today)
                                cost(range)
                            }
                            GridRow {
                                tokens(range)
                                turns(range)
                            }
                            GridRow {
                                time(range)
                                Color.clear.frame(height: 0)
                            }
                            #endif
                        }
                        HStack(alignment: .center, spacing: BoardStyle.Space.m) {
                            UsageChart(buckets: range.buckets, metric: range.costKnown ? .cost : .tokens, byWeek: false,
                                       compact: true).frame(height: 60)
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

/// The usage page of one project: the numbers of a range, a chart by day or by week, and each model. Without an ID it
/// is the leaderboard page (UsageBoard). Data only: the bar has the title, a number has its name, the chart has its
/// axis values, and a note shows only for a number that did not load.
struct UsageView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    var botId: String? = nil
    @State private var range = UsageRange.opening
    /// The chosen chart. Nil shows the spend, or the tokens when Hermes has no price for the models.
    @State private var metric: UsageMetric? = nil

    private var bot: Bot? { store.bots.first { $0.id == botId } }

    var body: some View {
        if botId == nil {
            UsageBoard()
        } else {
            projectPage
        }
    }

    private var projectPage: some View {
        BoardPage {
            if let report = store.usage {
                let agents = store.usageOf(botId)
                let summary = UsageSummary(agents: agents, range: range, today: report.today)
                let shown = metric ?? (summary.costKnown ? .cost : .tokens)
                chips(UsageRange.allCases, selected: range, title: \.title) { range = $0 }
                    .accessibilityIdentifier("usage-range")
                stats(summary)
                VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                    chips(UsageMetric.allCases, selected: shown, title: \.title) { metric = $0 }
                        .accessibilityIdentifier("usage-metric")
                    UsageChart(buckets: summary.buckets, metric: shown, byWeek: range.byWeek).frame(height: 220)
                }
                .padding(BoardStyle.Space.m)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                let models = UsageMath.models(agents)
                if !models.isEmpty {
                    // The Mac has one total of each model for the whole report, so the title names its length, in the
                    // words of the range above ("12 weeks").
                    let length = UsageRange.allCases.first { $0.days == report.days }?.title ?? "\(report.days) days"
                    BoardSection("Models · " + length, count: models.count) {
                        ForEach(models) { model in
                            BoardRow(title: store.modelCatalog?.models.first { $0.id == model.model }?.name ?? model.model,
                                     detail: model.provider.isEmpty ? nil : model.provider,
                                     value: (summary.costKnown ? UsageMath.cost(model.costUsd) + " · " : "") + UsageMath.count(model.tokens) + " tokens",
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
        // The spend first, then the tokens.
        let tiles = [
            UsageStat(title: "Spend", value: summary.cost, detail: summary.costNote),
            UsageStat(title: "Tokens in", value: UsageMath.count(total.tokensIn)),
            UsageStat(title: "Tokens out", value: UsageMath.count(total.outputTokens)),
            UsageStat(title: "Turns", value: UsageMath.count(total.turns)),
            UsageStat(title: "Tool calls", value: UsageMath.count(total.toolCalls)),
            UsageStat(title: "Time working", value: UsageMath.duration(total.workingSeconds)),
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

    /// Each project with its usage in the range, the largest cost first, as places of a leaderboard. A tap opens the
    /// usage page of the project.
    static func projectStandings(_ store: AppStore, _ report: UsageReport, range: UsageRange) -> [UsageStanding] {
        UsageLeaderboard.ranked(store.bots.compactMap { bot -> UsageStanding? in
            guard let usage = report.agents[bot.id] else { return nil }
            let summary = UsageSummary(agents: [usage], range: range, today: report.today)
            return summary.total.isEmpty ? nil : UsageStanding(id: bot.id, name: bot.name, total: summary.total,
                                                               costKnown: usage.costKnown)
        })
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

/// The usage page of all agents: the leaderboard of the people of each team by cost and their spend by day, then
/// where the spend of this person goes: the numbers of the range, the projects and the models. The person asked for a
/// leaderboard by person as the crux of the page, so it comes first, and for cost first and tokens second, so each place
/// leads with its cost and the tokens follow. Without a team the projects of this Mac are the leaderboard. Data only:
/// names, numbers, bars and charts.
struct UsageBoard: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @State private var range = UsageRange.opening

    var body: some View {
        BoardPage(width: .wide, spacing: BoardStyle.Space.l) {
            if let report = store.usage {
                content(report)
            } else if let issue = store.usageIssue {
                BoardLoadNotice(title: "Usage unavailable", message: issue) { Task { await store.loadUsage(force: true) } }
            } else {
                BoardLoading(title: "Loading usage")
            }
        }
        .navigationTitle("Usage").inlineNavigationTitle()
        .task(id: store.connection == .connected) { await store.loadUsage() }
        .refreshable { await store.loadUsage(force: true) }
        .accessibilityIdentifier("usage-page")
    }

    @ViewBuilder private func content(_ report: UsageReport) -> some View {
        // A team of one person ranks nobody, so it shows as no team.
        let teams = (report.teams ?? []).filter { $0.people.count > 1 }
        let summary = UsageSummary(agents: store.usageOf(nil), range: range, today: report.today)
        let projects = UsageView.projectStandings(store, report, range: range)
        Picker("Range", selection: $range) {
            ForEach(UsageRange.allCases) { Text($0.title).tag($0) }
        }
        .pickerStyle(.segmented).labelsHidden().bentoLeadingControl()
        .accessibilityIdentifier("usage-range")
        if teams.isEmpty {
            BentoBoard {
                UsageLeaderboardTile(title: "Projects", rows: projects) { pushRoute?(UsageRoute(botId: $0.id)) }
                    .bento(12, nil, compact: (2, nil))
                UsageStackChart(series: projectSeries(report, projects), metric: UsageLeaderboard.metric(projects),
                                range: range, today: report.today)
                    .bento(12, 2, compact: (2, 2))
                mine(summary, report, modelsWidth: 12)
            }
        } else {
            BentoBoard {
                ForEach(teams) { team in
                    let rows = UsageLeaderboard.people(team, range: range, today: report.today)
                    UsageLeaderboardTile(title: teams.count > 1 ? team.name : "Leaderboard", rows: rows)
                        .bento(12, nil, compact: (2, nil))
                    UsageStackChart(series: personSeries(team, rows), metric: UsageLeaderboard.metric(rows), range: range,
                                    today: report.today)
                        .bento(12, 2, compact: (2, 2))
                }
            }
            // The rest of the page is the person of this Mac, so it has that name over it.
            Text("You").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                .accessibilityAddTraits(.isHeader).padding(.top, BoardStyle.Space.s)
            BentoBoard {
                mine(summary, report)
                UsageLeaderboardTile(title: "Projects", rows: projects) { pushRoute?(UsageRoute(botId: $0.id)) }
                    .bento(6, nil, compact: (2, nil))
            }
        }
    }

    /// The numbers of this person in the range, then the models. The spend leads with its bars, and the tokens follow;
    /// without a price for the models the tokens lead. The Mac has one total of each model for the whole report, so its
    /// title names that length. On a Mac the models share a row with the projects, or take the row alone when the
    /// projects are the leaderboard above.
    @ViewBuilder private func mine(_ summary: UsageSummary, _ report: UsageReport, modelsWidth: Int = 6) -> some View {
        let total = summary.total
        if summary.costKnown {
            BentoStat(title: "Spend", value: summary.cost, trend: summary.buckets.map(\.total.costUsd), trendBars: true)
                .bento(4, 1, compact: (2, 1))
            BentoStat(title: "Tokens", value: UsageMath.count(total.tokens), size: .medium).bento(2, 1, compact: (1, 1))
        } else {
            BentoStat(title: "Tokens", value: UsageMath.count(total.tokens),
                      trend: summary.buckets.map { Double($0.total.tokens) }, trendBars: true)
                .bento(4, 1, compact: (2, 1))
            BentoStat(title: "Spend", value: summary.cost, size: .medium).bento(2, 1, compact: (1, 1))
        }
        BentoStat(title: "Turns", value: UsageMath.count(total.turns), size: .medium).bento(2, 1, compact: (1, 1))
        BentoStat(title: "Tool calls", value: UsageMath.count(total.toolCalls), size: .medium).bento(2, 1, compact: (1, 1))
        BentoStat(title: "Time working", value: UsageMath.duration(total.workingSeconds), size: .medium)
            .bento(2, 1, compact: (1, 1))
        let models = UsageMath.models(store.usageOf(nil))
        if !models.isEmpty {
            let length = UsageRange.allCases.first { $0.days == report.days }?.title ?? "\(report.days) days"
            modelsTile(models, title: "Models · " + length, costKnown: summary.costKnown)
                .bento(modelsWidth, nil, compact: (2, nil))
        }
    }

    /// Each model with its cost, and its tokens after it. A list without a price has its tokens alone.
    private func modelsTile(_ models: [UsageModel], title: String, costKnown: Bool) -> some View {
        BentoTile(padding: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                BentoLabel(title).padding(.horizontal, BoardStyle.Space.xs).padding(.top, BoardStyle.Space.xs)
                ForEach(models) { model in
                    HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                            Text(store.modelCatalog?.models.first { $0.id == model.model }?.name ?? model.model)
                                .font(.subheadline.weight(.medium)).lineLimit(1).truncationMode(.middle)
                            if !model.provider.isEmpty {
                                Text(model.provider).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }
                        Spacer(minLength: BoardStyle.Space.s)
                        if costKnown {
                            Text(UsageMath.cost(model.costUsd)).font(.subheadline.monospacedDigit())
                            Text(UsageMath.count(model.tokens)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                                .frame(minWidth: 44, alignment: .trailing)
                        } else {
                            Text(UsageMath.count(model.tokens)).font(.subheadline.monospacedDigit())
                        }
                    }
                    .padding(.horizontal, BoardStyle.Space.xs)
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .accessibilityIdentifier("usage-models")
    }

    /// The parts of the chart of a team: each person with usage, in the order of the leaderboard.
    private func personSeries(_ team: UsageTeam, _ rows: [UsageStanding]) -> [UsageSeries] {
        let days = Dictionary(team.people.map { ($0.member, $0.days) }) { first, _ in first }
        return rows.compactMap { row in
            guard let list = days[row.id] ?? nil else { return nil }
            return UsageSeries(id: row.id, name: row.name, days: list)
        }
    }

    /// The parts of the chart without a team: each project, in the order of the Projects list.
    private func projectSeries(_ report: UsageReport, _ rows: [UsageStanding]) -> [UsageSeries] {
        rows.compactMap { row in report.agents[row.id].map { UsageSeries(id: row.id, name: row.name, days: $0.days) } }
    }
}

/// A leaderboard: each place with its number, the face of a person, its name, its cost, a bar against the first place,
/// and its tokens and turns. Cost comes first and tokens second; a leaderboard where no place has a price ranks and
/// shows tokens. The person of this Mac has a soft wash of green. A person whose Mac shares no usage yet, or a place
/// without a price on a leaderboard of cost, has a dash and no bar. The cost and the tokens of all places are over the
/// list.
struct UsageLeaderboardTile: View {
    let title: String
    let rows: [UsageStanding]
    /// A tap on a place, for a project: its usage page. People open nothing.
    var open: ((UsageStanding) -> Void)? = nil

    var body: some View {
        let metric = UsageLeaderboard.metric(rows)
        let top = rows.compactMap(\.score).max() ?? 0
        BentoTile(padding: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                    BentoLabel(title)
                    Spacer(minLength: BoardStyle.Space.s)
                    if let line = totalLine(metric) {
                        Text(line).font(.caption.weight(.semibold).monospacedDigit())
                            .foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .padding(.horizontal, BoardStyle.Space.s).padding(.top, BoardStyle.Space.xs).padding(.bottom, BoardStyle.Space.xs)
                if rows.isEmpty {
                    Text("\u{2014}").font(.title3).foregroundStyle(.tertiary).padding(.horizontal, BoardStyle.Space.s)
                }
                ForEach(rows) { row in
                    if let open {
                        Button { open(row) } label: { place(row, metric: metric, top: top) }
                            .buttonStyle(HomePressStyle())
                            .accessibilityIdentifier("usage-place-" + row.id)
                    } else {
                        place(row, metric: metric, top: top).accessibilityIdentifier("usage-place-" + row.id)
                    }
                }
            }
        }
        .accessibilityIdentifier("usage-leaderboard")
    }

    /// "$2,383 · 9.9B tokens": the cost of the places with a price, then the tokens of all. Nil without usage.
    private func totalLine(_ metric: UsageMetric) -> String? {
        let tokens = rows.compactMap(\.tokens).reduce(0, +)
        guard tokens > 0 else { return nil }
        let count = UsageMath.count(tokens) + " tokens"
        guard metric == .cost else { return count }
        return UsageMath.cost(rows.compactMap(\.score).reduce(0, +)) + " · " + count
    }

    private func place(_ row: UsageStanding, metric: UsageMetric, top: Double) -> some View {
        HStack(alignment: .center, spacing: BoardStyle.Space.m) {
            Text(verbatim: row.rank.map { String($0) } ?? "\u{2013}")
                .font(.system(.title3, design: .rounded).weight(.bold)).monospacedDigit()
                .foregroundStyle(row.rank == 1 ? AnyShapeStyle(BoardStyle.green) : AnyShapeStyle(.secondary))
                .frame(minWidth: 22)
            if let member = row.member {
                PersonFace(member: nil, memberId: member, name: row.name, size: 34)
            }
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                    Text(row.name).font(.body.weight(.semibold)).lineLimit(1)
                    if row.isMe {
                        Text("You").font(.caption.weight(.semibold)).foregroundStyle(BoardStyle.green)
                    }
                    Spacer(minLength: BoardStyle.Space.s)
                    Text(row.score.map(metric.text) ?? "\u{2014}")
                        .font(.system(.title3, design: .rounded).weight(.semibold)).monospacedDigit()
                        .foregroundStyle(row.score == nil ? AnyShapeStyle(.tertiary) : AnyShapeStyle(.primary))
                        .lineLimit(1)
                }
                if let total = row.total {
                    if let score = row.score {
                        GeometryReader { proxy in
                            Capsule().fill(Color.primary.opacity(0.08))
                                .overlay(alignment: .leading) {
                                    Capsule().fill(BentoColor.money)
                                        .frame(width: max(3, proxy.size.width * (top > 0 ? score / top : 0)))
                                }
                        }
                        .frame(height: 6)
                        .accessibilityHidden(true)
                    }
                    Text(detail(total, metric: metric)).font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, BoardStyle.Space.s)
        .background {
            if row.isMe {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous).fill(BoardStyle.green.opacity(0.1))
            }
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// "18.2M tokens · 412 turns" under the cost. A leaderboard of tokens has the turns alone.
    private func detail(_ total: UsageDay, metric: UsageMetric) -> String {
        let turns = UsageMath.count(total.turns) + (total.turns == 1 ? " turn" : " turns")
        return metric == .cost ? UsageMath.count(total.tokens) + " tokens · " + turns : turns
    }
}

/// One part of a stacked usage chart: a person of a team, or a project.
struct UsageSeries: Identifiable {
    let id: String
    let name: String
    let days: [UsageDay]
}

/// The spend of a range by day, or by week in the long range, with a part of each bar for each series in its order:
/// the tokens only when no series has a price for its models (UsageLeaderboard.metric). The first four have a color
/// each, and the rest share one part.
struct UsageStackChart: View {
    let series: [UsageSeries]
    let metric: UsageMetric
    let range: UsageRange
    let today: String

    /// "Spend by day", as on the Spend page.
    private var title: String { metric.title + " by day" }

    static let palette: [Color] = SpendView.palette
    static let other = "Others"

    private struct Point: Identifiable {
        let id: String
        let start: Date
        let series: String
        let value: Double
    }

    var body: some View {
        let (points, names, dates) = layout()
        // The fifth color is the gray of the rest: a fifth person alone has it, as "Others" does.
        let colors = names.enumerated().map { index, name in
            name == Self.other || index >= Self.palette.count ? SpendView.rest : Self.palette[index]
        }
        BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                BentoLabel(title)
                Chart(points) { point in
                    BarMark(x: .value(range.byWeek ? "Week" : "Day", point.start, unit: range.byWeek ? .weekOfYear : .day),
                            y: .value(metric.title, point.value), width: .ratio(0.72))
                        .foregroundStyle(by: .value("Series", point.series))
                        .cornerRadius(2)
                }
                .chartForegroundStyleScale(domain: names, range: colors)
                .chartLegend(position: .bottom, alignment: .leading, spacing: BoardStyle.Space.s)
                .chartXAxis {
                    AxisMarks(values: dates) { _ in
                        AxisValueLabel(format: .dateTime.month(.abbreviated).day(), anchor: .top)
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine().foregroundStyle(BoardStyle.line)
                        AxisValueLabel { if let number = value.as(Double.self) { Text(metric.axis(number)) } }
                    }
                }
                .frame(maxHeight: .infinity)
                .accessibilityLabel(metric.title + ", by " + (range.byWeek ? "week" : "day"))
            }
        }
        .accessibilityIdentifier("usage-chart")
    }

    /// The points of the bars in the order of the series, so the first series is at the base of each bar; the names
    /// of the parts; and the dates that get a label: each fifth part of the range from its start, as UsageChart has.
    /// A fifth series alone keeps its name; more share the part "Others".
    private func layout() -> ([Point], [String], [Date]) {
        let shown = series.count == Self.palette.count + 1 ? series[...] : series.prefix(Self.palette.count)
        let rest = series.dropFirst(shown.count)
        var parts: [(name: String, days: [UsageDay])] = []
        var used = Set<String>()
        for item in shown {
            // Two people with one name keep two parts.
            var name = item.name
            var number = 2
            while used.contains(name) { name = item.name + " \(number)"; number += 1 }
            used.insert(name)
            parts.append((name, item.days))
        }
        if !rest.isEmpty { parts.append((Self.other, rest.flatMap(\.days))) }
        var points: [Point] = []
        var starts: [Date] = []
        for part in parts {
            let buckets = UsageMath.buckets(UsageMath.combined([AgentUsage(days: part.days)]), range: range, today: today)
            if starts.isEmpty { starts = buckets.map(\.start) }
            points += buckets.map { Point(id: part.name + "\u{1f}" + $0.total.day, start: $0.start, series: part.name,
                                          value: metric.value($0.total)) }
        }
        let step = max(1, (starts.count + 4) / 5)
        let dates = starts.indices.filter { $0 % step == 0 && starts.count - $0 >= step }.map { starts[$0] }
        return (points, parts.map(\.name), dates)
    }
}
