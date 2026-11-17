import Charts
import SwiftUI

/// The Spend page.
struct SpendRoute: Hashable {}

/// What the agents cost: today, the last 7 days and the last 30 days, then where it went by agent, model and chat.
/// Data only: numbers, charts and rows.
struct SpendView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openChat) private var openChat
    @State private var period = SpendPeriod.week

    /// The colors of the series of the chart and of the rows: the green, the accent and two faces of Coconut.
    static let palette: [Color] = [BentoColor.money, BoardStyle.accent, BentoColor.second, BentoColor.third]
    static let rest = Color.secondary.opacity(0.45)

    private func modelName(_ id: String) -> String {
        store.modelCatalog?.models.first { $0.id == id }?.name ?? id
    }

    var body: some View {
        ScrollView {
            if let report = store.usage {
                content(report)
                    .padding(BentoPage.padding)
                    .frame(maxWidth: BentoPage.width, alignment: .leading).frame(maxWidth: .infinity)
            } else if let issue = store.usageIssue {
                QuietEmptyState(symbol: "dollarsign.circle", title: "No spend yet", message: issue)
                    .padding(BentoPage.padding)
            } else {
                ProgressView().frame(maxWidth: .infinity).padding(.vertical, 90)
            }
        }
        .background(BoardStyle.paper)
        .navigationTitle("Spend").inlineNavigationTitle()
        .task(id: store.connection == .connected) { await store.loadUsage() }
        .refreshable { await store.loadUsage(force: true) }
        .accessibilityIdentifier("spend-page")
    }

    private func content(_ report: UsageReport) -> some View {
        let selected = SpendSummary(report: report, bots: store.bots, period: period, modelName: modelName)
        let daily = SpendSummary.daily(report: report, bots: store.bots)
        return VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            BentoBoard {
                periodTile(report, .today).bento(4, 1, compact: (2, 1))
                periodTile(report, .week).bento(4, 1, compact: (2, 1))
                periodTile(report, .month).bento(4, 1, compact: (2, 1))
                dailyTile(daily).bento(8, 3, compact: (2, 3))
                modelsTile(selected).bento(4, 3, compact: (2, 3))
            }
            Picker("Period", selection: $period) {
                ForEach(SpendPeriod.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented).labelsHidden().bentoLeadingControl()
            .accessibilityIdentifier("spend-period")
            BentoBoard {
                ranking("Agents", selected.agents, summary: selected, symbol: "square.stack.3d.up").bento(6, nil, compact: (2, nil))
                ranking("Chats", Array(selected.chats.prefix(SpendSummary.chatLimit)), summary: selected, symbol: "bubble.left")
                    .bento(6, nil, compact: (2, nil))
            }
        }
    }

    // MARK: Periods

    private func periodTile(_ report: UsageReport, _ period: SpendPeriod) -> some View {
        let summary = SpendSummary(report: report, bots: store.bots, period: period, modelName: modelName)
        let days = SpendSummary.daily(report: report, bots: store.bots, days: period.days)
        var byDay: [Date: Double] = [:]
        for point in days { byDay[point.date, default: 0] += point.cost }
        let trend = byDay.sorted { $0.key < $1.key }.map(\.value)
        return BentoStat(title: period.title, value: summary.costKnown ? UsageMath.cost(summary.cost) : "\u{2014}",
                         note: UsageMath.count(summary.tokens) + " tokens", trend: period == .today ? [] : trend, trendBars: true)
            .accessibilityIdentifier("spend-" + period.rawValue)
    }

    // MARK: Daily chart

    private func dailyTile(_ points: [SpendDaily]) -> some View {
        let series = SpendSummary.series(points)
        let colors = series.enumerated().map { index, name in
            name == SpendSummary.other ? Self.rest : Self.palette[index % Self.palette.count]
        }
        return BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                BentoLabel("Cost by day")
                Chart(points) { point in
                    BarMark(x: .value("Day", point.date, unit: .day), y: .value("Cost", point.cost), width: .ratio(0.72))
                        .foregroundStyle(by: .value("Agent", point.series))
                        .cornerRadius(2)
                }
                .chartForegroundStyleScale(domain: series, range: colors)
                .chartLegend(position: .bottom, alignment: .leading, spacing: BoardStyle.Space.s)
                .chartXAxis {
                    AxisMarks(values: .stride(by: .day, count: 7)) { _ in
                        AxisValueLabel(format: .dateTime.month(.abbreviated).day(), anchor: .top)
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine().foregroundStyle(BoardStyle.line)
                        AxisValueLabel { if let cost = value.as(Double.self) { Text(UsageMath.cost(cost)) } }
                    }
                }
                .frame(maxHeight: .infinity)
                .accessibilityLabel("Cost by day, by agent")
            }
        }
        .accessibilityIdentifier("spend-daily")
    }

    // MARK: Models

    private func modelsTile(_ summary: SpendSummary) -> some View {
        let models = Array(summary.models.prefix(5))
        return BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                BentoLabel("Models")
                if models.isEmpty {
                    Spacer(minLength: 0)
                } else {
                    Chart(models) { model in
                        SectorMark(angle: .value("Cost", max(model.cost, 0.0001)), innerRadius: .ratio(0.64), angularInset: 1.5)
                            .cornerRadius(4)
                            .foregroundStyle(by: .value("Model", model.title))
                    }
                    .chartForegroundStyleScale(domain: models.map(\.title), range: models.indices.map { Self.palette[$0 % Self.palette.count] })
                    .chartLegend(.hidden)
                    .chartBackground { _ in
                        BentoNumber(summary.costKnown ? UsageMath.cost(summary.cost) : UsageMath.count(summary.tokens), size: .small)
                    }
                    .frame(maxWidth: .infinity, minHeight: 90, maxHeight: .infinity)
                    .accessibilityLabel("Cost by model")
                    VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                        ForEach(Array(models.enumerated()), id: \.element.id) { index, model in
                            HStack(spacing: BoardStyle.Space.s) {
                                Circle().fill(Self.palette[index % Self.palette.count]).frame(width: 8, height: 8)
                                Text(model.title).lineLimit(1).truncationMode(.middle)
                                Spacer(minLength: BoardStyle.Space.xs)
                                Text(summary.value(model)).foregroundStyle(.secondary).monospacedDigit()
                            }
                            .font(.caption)
                        }
                    }
                }
            }
        }
        .accessibilityIdentifier("spend-models")
    }

    // MARK: Rankings

    private func ranking(_ title: String, _ slices: [SpendSlice], summary: SpendSummary, symbol: String) -> some View {
        let top = slices.map(\.cost).max() ?? 0
        return BentoTile(padding: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                BentoLabel(title).padding(.horizontal, BoardStyle.Space.xs).padding(.top, BoardStyle.Space.xs)
                if slices.isEmpty {
                    Text("\u{2014}").font(.title3).foregroundStyle(.tertiary).padding(BoardStyle.Space.xs)
                }
                ForEach(slices) { slice in
                    row(slice, top: top, summary: summary)
                }
            }
        }
        .accessibilityIdentifier("spend-" + title.lowercased())
    }

    private func row(_ slice: SpendSlice, top: Double, summary: SpendSummary) -> some View {
        let bot = slice.botId.flatMap { id in store.bots.first { $0.id == id } }
        let share = top > 0 ? slice.cost / top : 0
        let content = HStack(spacing: BoardStyle.Space.m) {
            if let bot { LiveBotAvatar(bot: bot, size: 28) }
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                HStack(alignment: .firstTextBaseline) {
                    Text(slice.title).font(.subheadline.weight(.medium)).lineLimit(1)
                    Spacer(minLength: BoardStyle.Space.s)
                    Text(summary.value(slice)).font(.subheadline.monospacedDigit())
                    Text(UsageMath.count(slice.tokens)).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                        .frame(minWidth: 40, alignment: .trailing)
                }
                GeometryReader { proxy in
                    Capsule().fill(Color.primary.opacity(0.08))
                        .overlay(alignment: .leading) {
                            Capsule().fill(BentoColor.money).frame(width: max(3, proxy.size.width * share))
                        }
                }
                .frame(height: 5)
            }
        }
        .padding(.horizontal, BoardStyle.Space.xs).padding(.vertical, BoardStyle.Space.s)
        .contentShape(Rectangle())
        return Group {
            if let chat = slice.chatId {
                Button {
                    if let openChat { openChat(chat) } else { pushRoute?(ConversationRoute(botId: chat)) }
                } label: { content }
                    .buttonStyle(HomePressStyle())
            } else {
                content
            }
        }
        .accessibilityElement(children: .combine)
    }
}
