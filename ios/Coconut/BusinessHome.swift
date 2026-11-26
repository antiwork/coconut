import SwiftUI
import Charts

/// Presentation only: totals, comparisons and margins must come from the publisher.
/// An absent health map is compatible with old snapshots, not an implicit failed source.
struct BusinessHomePresentation {
    let report: BusinessReport
    var now: Date = .now
    var refreshFailed = false
    var data: BusinessValue { report.data }
    var age: TimeInterval? { report.generatedAt?.apiDate.map { now.timeIntervalSince($0) } }
    var freshnessWarning: Bool { age.map { $0 > 45 * 60 || $0 < -60 } ?? true }
    var currentStatusUnknown: Bool { refreshFailed || (age.map { $0 > 3600 || $0 < -60 } ?? true) }
    var freshness: String {
        guard let age, age >= -60 else { return "Freshness unknown" }
        return BusinessFormat.time(now.addingTimeInterval(-max(0, age)), now: now) + (age > 3600 ? " · Stale" : "")
    }

    func sourceNotice(_ key: String) -> String? {
        let section = data["sections"][key]
        if section["ok"] == .bool(false) { return "Unavailable" }
        if let date = section["as_of"].text.apiDate, now.timeIntervalSince(date) > 3600 {
            return "Stale source · current status unknown"
        }
        return nil
    }

    struct AttentionRow: Identifiable {
        let id: String
        /// The name of the row. A breach has one name atop the to-dos of its tab and on the Mac's Home ("Support past
        /// SLA"), and a row without a current source says so ("Support · Unknown").
        let title: String
        let detail: String?
        /// The symbol of the row atop a list of to-dos and in Needs attention (AttentionRules).
        let symbol: String
        let route: BusinessRoute
        let unavailable: Bool
        /// The published count of an informational row, when its source is current.
        var value: String? = nil
        /// The count of a row that breaches.
        var count = 0
    }
    /// The breaches of the overview that AttentionRules reads. Support past SLA counts on Support and the risk reviews
    /// past SLA on Finance, each as the first row of the to-dos of its tab (TodoCounts), as the person asked for the
    /// needs attention of the business to show as to-dos. The issues past SLA and of high priority are open issues of
    /// gumroad-private, so they are to-dos of their own lists already: only the Mac's Home shows them as breaches. Each
    /// row opens the detail of what breaches: the open conversations of its queue, or the report of Risk.
    var attention: [AttentionRow] {
        [attentionRow("support", source: "support", name: "Support", count: data["support"]["gumroad_past_sla"].number,
                      breach: "Support past SLA", detail: supportDetail, symbol: "lifepreserver", route: .conversations("support")),
         // The hammer of the Product tab.
         attentionRow("issues", source: "issues", name: "Product issues", count: data["issues"]["past_sla"].number,
                      breach: "Product issues past SLA", detail: nil, symbol: "hammer", route: .conversations("issues")),
         attentionRow("high-priority", source: "issues", name: "High priority", count: data["high_priority"]["count"].number,
                      breach: "High-priority issues", detail: highPriorityDetail, symbol: "exclamationmark.circle",
                      route: .conversations("issues")),
         attentionRow("risk", source: "risk", name: "Risk review", count: data["risk"]["past_sla"].number,
                      breach: "Risk review past SLA", detail: nil, symbol: "shield.lefthalf.filled",
                      route: .report("risk"))].compactMap { $0 }
    }
    private var supportDetail: String? {
        let first = data["support"]["top"][0]
        return nonempty([nonempty(first["subject"].text), first["hours"].number.map { Self.decimal($0) + "h" }]
            .compactMap { $0 }.joined(separator: " · "))
    }
    /// The snapshot publishes the titles by issue number. An older snapshot published a list.
    private var highPriorityDetail: String? {
        let high = data["high_priority"]
        if let title = nonempty(high["top"][0]["title"].text) { return title }
        guard let number = high["numbers"][0].number else { return nil }
        return nonempty(high["titles"][String(Int(number))].text)
    }
    /// `name` is the source of the row, and `breach` what the row is named while its count is over zero.
    private func attentionRow(_ id: String, source: String, name: String, count: Double?, breach: String,
                              detail: String?, symbol: String, route: BusinessRoute) -> AttentionRow? {
        if let notice = sourceNotice(source) {
            return AttentionRow(id: id, title: name + " · " + notice, detail: nil, symbol: symbol, route: route, unavailable: true)
        }
        if currentStatusUnknown || count == nil {
            return AttentionRow(id: id, title: name + " · Unknown", detail: currentStatusUnknown ? "Current status unknown" : "Not published",
                                symbol: symbol, route: route, unavailable: true)
        }
        guard let count, count > 0 else { return nil }
        return AttentionRow(id: id, title: breach, detail: detail, symbol: symbol, route: route, unavailable: false,
                            count: Int(count.rounded(.up)))
    }
    /// The queues that Finance shows as tiles: the risk reviews. The open pull requests are the state of the product,
    /// so Product lists them (ProductWork) and Finance does not.
    var informational: [AttentionRow] {
        [("risk", "Risk review", "shield.lefthalf.filled", BusinessRoute.report("risk"))].map { key, title, symbol, route in
            let notice = sourceNotice(key) ?? (currentStatusUnknown ? "Current status unknown" : nil)
            let count = data[key]["total"].number
            return AttentionRow(id: key, title: title, detail: notice ?? count.map { Self.decimal($0) + " open" } ?? "Not published",
                                symbol: symbol, route: route, unavailable: notice != nil || count == nil,
                                value: notice == nil ? count.map { Self.decimal($0) } : nil)
        }
    }

    struct Point: Identifiable {
        let id: Int
        let date: String
        let value: Double
    }
    private func points(_ field: String) -> [Point] {
        guard sourceNotice("gmv") == nil else { return [] }
        return data["gmv"][field].array.enumerated().compactMap { index, row in
            guard let value = row[1].number, !row[0].text.isEmpty else { return nil }
            return Point(id: index, date: row[0].text, value: value)
        }
    }
    var volume: [Point] { points("series") }
    var previousVolume: [Point] { points("series_prev") }
    var gmvNotice: String? { sourceNotice("gmv") ?? (volume.isEmpty ? "Not published" : nil) }
    var change: Double? { sourceNotice("gmv") == nil ? data["gmv"]["change_pct"].number : nil }
    var changeLabel: String? { nonempty(data["gmv"]["change_label"].text) }
    /// Published totals of the GMV section. A failed or stale source shows none of them.
    private func gmvNumber(_ field: String) -> Double? { sourceNotice("gmv") == nil ? data["gmv"][field].number : nil }
    var total: Double? { gmvNumber("gmv_30d") }
    var runRate: Double? { gmvNumber("annualized_gmv") }
    var dailyAverage: Double? { gmvNumber("trailing_avg_gmv") }
    /// The day of a source, when the snapshot publishes one: "as of Nov 15".
    func asOf(_ key: String) -> String? {
        let text = data["sections"][key]["as_of"].text
        guard text.count == 10, ReportPoint(date: text, value: 0, series: "").day != .distantPast else { return nil }
        return Self.day(text)
    }

    struct MoneyRow: Identifiable {
        let id: String
        let title: String
        let value: Double?
        let subtitle: String?
        let months: [Point]
        var formattedValue: String { value.map { reportNumber($0, money: true) } ?? "Not published" }
    }
    var moneyRows: [MoneyRow] {
        guard sourceNotice("ledger") == nil else { return [] }
        let money = data["money"]
        return [("revenue", "Revenue"), ("ebitda", "EBITDA"), ("net_income", "Net income")].map { key, title in
            let subtitle: String?
            if key == "ebitda" {
                subtitle = money["ebitda_margin_pct"].number.map { Self.percent($0) + " margin" }
            } else if key == "net_income", let impairment = money["impairment_total"].number, impairment != 0 {
                subtitle = "includes " + Self.writeOff(impairment) + " non-cash write-off"
            } else { subtitle = nil }
            let months = money["months"].array.enumerated().compactMap { index, month -> Point? in
                guard let value = month[key].number, !month["month"].text.isEmpty else { return nil }
                return Point(id: index, date: month["month"].text, value: value)
            }
            return MoneyRow(id: key, title: title, value: money["ltm_" + key].number, subtitle: subtitle, months: months)
        }
    }
    private func nonempty(_ text: String) -> String? { text.isEmpty ? nil : text }
    static func decimal(_ value: Double) -> String { BusinessFormat.number(value) }
    static func percent(_ value: Double, signed: Bool = false) -> String { BusinessFormat.percent(value, signed: signed) }
    static func day(_ date: String) -> String { BusinessFormat.day(date) }
    /// "2026-10" as "Oct 2026". Calendar formatting only.
    static func month(_ month: String) -> String { BusinessFormat.month(month) }
    static func writeOff(_ value: Double) -> String { BusinessFormat.writeOff(value) }
}

/// The tiles of Finance on its one page (DashboardPage), which the person reads at once without scrolling: the to-dos,
/// GMV, the money of the ledger and the risk reviews. A tap on a tile opens its detail: GMV its history by day, month and
/// year (History), the money Accounting, which opens the ledger, and the risk reviews the report of Risk. A row of the
/// to-dos opens its issue or its review, and the last line of the tile the whole list. The work of Marketing is the
/// to-dos of Finance (business.WORK_AREAS), so it has no tile of its own.
struct FinanceTiles: View {
    /// Nil while the overview loads, or when it could not load.
    let model: BusinessHomePresentation?
    var error: String? = nil
    var retry: () -> Void = {}
    @Environment(\.bentoCompact) private var compact

    var body: some View {
        TodoTile(area: .finance).bento(5, 3, compact: (2, 2))
        gmv.bento(7, 3, compact: (2, 2))
        ForEach(moneyRows) { row in moneyTile(row).bento(3, 2, compact: (1, 1)) }
        risk.bento(3, 2, compact: (1, 1))
    }

    // MARK: - GMV

    /// The volume of the last 30 days with its trend. A tap opens History, the volume of each day, month and year.
    @ViewBuilder private var gmv: some View {
        if let model, model.gmvNotice == nil {
            BentoLink(route: .reports) { gmvTile(model) }
                .accessibilityIdentifier("business-home-gmv")
        } else {
            BentoTile {
                VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                    gmvHeader(model)
                    if let notice = model?.gmvNotice {
                        Text(notice).font(.system(.title2, design: .rounded, weight: .semibold)).foregroundStyle(.secondary)
                            .accessibilityIdentifier("business-home-gmv-unavailable")
                    } else if let error {
                        BoardLoadNotice(message: error, retry: retry).accessibilityIdentifier("business-home-refresh-error")
                    } else {
                        CoconutLoader(.page).frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    Spacer(minLength: 0)
                }
            }
            .accessibilityIdentifier("business-home-gmv")
        }
    }

    private func gmvTile(_ model: BusinessHomePresentation) -> some View {
        BentoTile(wash: BentoColor.money) {
            VStack(alignment: .leading, spacing: compact ? BoardStyle.Space.xs : BoardStyle.Space.s) {
                gmvHeader(model)
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) { total(model); delta(model, label: true) }
                    HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) { total(model); delta(model, label: false) }
                    total(model)
                }
                volumeChart(model)
                figures(model)
            }
        }
    }

    /// The name of the tile, the day of its numbers, and its chevron. An old snapshot, or one that a refresh could not
    /// replace, says its age in the place of the day. Fresh numbers need no word.
    private func gmvHeader(_ model: BusinessHomePresentation?) -> some View {
        HStack(spacing: BoardStyle.Space.s) {
            BentoLabel("GMV · 30 days")
            Spacer(minLength: 0)
            if let model, model.freshnessWarning || model.currentStatusUnknown {
                Text(model.currentStatusUnknown ? model.freshness + " · Unknown" : model.freshness)
                    .font(.caption)
                    .foregroundStyle(model.freshnessWarning ? AnyShapeStyle(BoardStyle.attention) : AnyShapeStyle(.secondary))
                    .lineLimit(1)
                    .accessibilityIdentifier("business-home-freshness")
            } else if let day = model?.asOf("gmv") {
                Text(day).font(.caption).foregroundStyle(.tertiary).lineLimit(1)
            }
            if model?.gmvNotice == nil && model != nil { BentoChevron() }
        }
    }

    @ViewBuilder private func total(_ model: BusinessHomePresentation) -> some View {
        if let total = model.total {
            BentoNumber(reportNumber(total, money: true), size: compact ? .large : .hero).accessibilityIdentifier("business-home-gmv-total")
        }
    }

    @ViewBuilder private func delta(_ model: BusinessHomePresentation, label: Bool) -> some View {
        if let change = model.change {
            BentoDelta(percent: change, label: label ? model.changeLabel : nil).accessibilityIdentifier("business-home-gmv-delta")
        }
    }

    /// The run rate and the daily average of the volume, under its chart.
    private func figures(_ model: BusinessHomePresentation) -> some View {
        HStack(spacing: BoardStyle.Space.l) {
            figure("Run rate", model.runRate).accessibilityIdentifier("business-home-run-rate")
            figure("Daily average", model.dailyAverage).accessibilityIdentifier("business-home-daily-average")
            Spacer(minLength: 0)
        }
        .font(.caption).lineLimit(1)
    }

    private func figure(_ title: String, _ value: Double?) -> some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Text(title).foregroundStyle(.secondary)
            Text(reportNumber(value, money: true)).fontWeight(.semibold).monospacedDigit()
        }
        .accessibilityElement(children: .combine)
    }

    /// The 30 days over the 30 before them, both on one scale. The tile is a link, so the chart takes no touch: History
    /// shows the value of each day.
    private func volumeChart(_ model: BusinessHomePresentation) -> some View {
        let floor = (model.volume + model.previousVolume).map(\.value).min() ?? 0
        let ceiling = (model.volume + model.previousVolume).map(\.value).max() ?? 1
        // The fill starts under the lowest day, so a small change of a large volume still shows as a shape.
        let base = max(0, floor - (ceiling - floor) * 0.35)
        return Chart {
            // Align each period by its published ordinal day; both lines share one y-scale.
            ForEach(model.volume) { point in
                AreaMark(x: .value("Day in period", point.id), yStart: .value("Base", base), yEnd: .value("GMV", point.value))
                    .foregroundStyle(LinearGradient(colors: [BentoColor.money.opacity(0.26), BentoColor.money.opacity(0)], startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone).accessibilityHidden(true)
            }
            ForEach(model.previousVolume) { point in
                LineMark(x: .value("Day in period", point.id), y: .value("GMV", point.value), series: .value("Period", "Previous"))
                    .foregroundStyle(Color.secondary.opacity(0.6)).lineStyle(StrokeStyle(lineWidth: 1.5, lineCap: .round, dash: [3, 4]))
                    .interpolationMethod(.monotone)
                    .accessibilityLabel("Prior period, " + BusinessHomePresentation.day(point.date))
                    .accessibilityValue(reportNumber(point.value, money: true))
            }
            ForEach(model.volume) { point in
                LineMark(x: .value("Day in period", point.id), y: .value("GMV", point.value), series: .value("Period", "Current"))
                    .foregroundStyle(BentoColor.money).lineStyle(StrokeStyle(lineWidth: 2.5, lineCap: .round, lineJoin: .round))
                    .interpolationMethod(.monotone)
                    .accessibilityLabel(BusinessHomePresentation.day(point.date)).accessibilityValue(reportNumber(point.value, money: true))
            }
        }
        .chartYScale(domain: base...(ceiling + (ceiling - floor) * 0.1 + 1))
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 4)) { value in
                AxisValueLabel {
                    if let index = value.as(Int.self), let point = model.volume.first(where: { $0.id == index }) {
                        Text(BusinessFormat.axisDay(point.date))
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(BoardStyle.line)
                AxisValueLabel { if let n = value.as(Double.self) { Text(reportNumber(n, money: true)) } }
            }
        }
        // A low tile of an iPhone has no room for the axes: the line is the shape of the month.
        .chartXAxis(compact ? .hidden : .automatic)
        .chartYAxis(compact ? .hidden : .automatic)
        .frame(minHeight: 24, maxHeight: .infinity).accessibilityIdentifier("business-home-gmv-chart")
    }

    // MARK: - Money

    /// Revenue, EBITDA and net income of the last twelve months. Until the overview loads, or while the ledger is
    /// unavailable, each keeps its tile with a dash, so the page keeps its shape.
    private var moneyRows: [BusinessHomePresentation.MoneyRow] {
        if let rows = model?.moneyRows, !rows.isEmpty { return rows }
        let notice = model?.sourceNotice("ledger")
        return [("revenue", "Revenue"), ("ebitda", "EBITDA"), ("net_income", "Net income")].map { key, title in
            BusinessHomePresentation.MoneyRow(id: key, title: title, value: nil, subtitle: notice, months: [])
        }
    }

    /// A tap opens Accounting: the months of the ledger, the margins and the books.
    private func moneyTile(_ row: BusinessHomePresentation.MoneyRow) -> some View {
        BentoLink(route: .report("accounting")) {
            BentoTile {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: BoardStyle.Space.xs) {
                        BentoLabel(row.title)
                        Text("LTM").font(.caption).foregroundStyle(.tertiary)
                        Spacer(minLength: 0)
                        BentoChevron()
                    }
                    Spacer(minLength: BoardStyle.Space.xs)
                    BentoNumber(row.value == nil ? "—" : row.formattedValue, size: compact ? .medium : .large)
                    // A tile of an iPhone holds the number. A Mac's has room for its margin and its months.
                    if !compact {
                        if let subtitle = row.subtitle {
                            Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                .accessibilityIdentifier("business-home-money-" + row.id + "-subtitle")
                        }
                        if !row.months.isEmpty {
                            Chart(row.months) { month in
                                BarMark(x: .value("Month", month.date), y: .value(row.title, month.value), width: .ratio(0.72))
                                    .foregroundStyle(month.value < 0 ? BoardStyle.attention : BentoColor.money.opacity(month.id == row.months.last?.id ? 1 : 0.55))
                                    .cornerRadius(3)
                                    .accessibilityLabel(BusinessHomePresentation.month(month.date) + ", " + row.title)
                                    .accessibilityValue(reportNumber(month.value, money: true))
                            }
                            .chartXAxis(.hidden).chartYAxis(.hidden).frame(minHeight: 32, maxHeight: .infinity).padding(.top, BoardStyle.Space.m)
                            .accessibilityIdentifier("business-home-money-" + row.id + "-chart")
                        }
                    }
                }
            }
        }
        .accessibilityIdentifier("business-home-money-" + row.id.replacingOccurrences(of: "_", with: "-"))
    }

    // MARK: - Risk

    /// The accounts in the queues of the risk review. The reviews past SLA are rows of the to-dos, so an iPhone shows
    /// the number only, with the wash of the accent while one is late. A tap opens the report of Risk.
    private var risk: some View {
        let row = model?.informational.first
        let late = model.flatMap { $0.sourceNotice("risk") == nil ? $0.data["risk"]["past_sla"].number : nil }
        let note: String? = row?.value == nil ? row?.detail : late.map { $0 > 0 ? "\(Int($0)) past SLA" : "Within SLA" }
        return BentoLink(route: .report("risk")) {
            BentoStat(title: "Risk review", value: row?.value ?? "—", unit: row?.value == nil ? nil : "open",
                      note: compact ? nil : note,
                      noteTint: (late ?? 0) > 0 ? BoardStyle.attention : late == nil ? .secondary : BoardStyle.positive,
                      size: compact ? .medium : .large, wash: (late ?? 0) > 0 ? BoardStyle.attention : nil, chevron: true)
        }
        .accessibilityIdentifier("business-home-info-risk")
    }
}
