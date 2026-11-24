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
    /// past SLA on Business, each as the first row of the to-dos of its tab (TodoCounts), as the person asked for the
    /// needs attention of the business to show as to-dos. The issues past SLA and of high priority are open issues of
    /// gumroad-private, so they are to-dos of their own lists already: only the Mac's Home shows them as breaches.
    var attention: [AttentionRow] {
        [attentionRow("support", source: "support", name: "Support", count: data["support"]["gumroad_past_sla"].number,
                      breach: "Support past SLA", detail: supportDetail, symbol: "lifepreserver", route: .function("support")),
         // The hammer of the Product tab.
         attentionRow("issues", source: "issues", name: "Product issues", count: data["issues"]["past_sla"].number,
                      breach: "Product issues past SLA", detail: nil, symbol: "hammer", route: .report("support")),
         attentionRow("high-priority", source: "issues", name: "High priority", count: data["high_priority"]["count"].number,
                      breach: "High-priority issues", detail: highPriorityDetail, symbol: "exclamationmark.circle",
                      route: .report("support")),
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
    /// The queues that Business shows as tiles: the risk reviews. The open pull requests are the state of the product,
    /// so Product lists them (ProductWork) and Business does not.
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
        var label: String { BusinessHomePresentation.day(date) + " · " + reportNumber(value, money: true) }
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
    var latest: String? {
        guard sourceNotice("gmv") == nil, let value = data["gmv"]["last_day_gmv"].number,
              let day = nonempty(data["gmv"]["last_day"].text) else { return nil }
        return Self.day(day) + " · " + reportNumber(value, money: true)
    }
    var weekdayComparison: String? {
        guard sourceNotice("gmv") == nil else { return nil }
        return data["gmv"]["same_weekday_pct"].number.map { Self.percent($0, signed: true) + " vs weekday" }
    }
    func selectedPoint(at position: Double?) -> Point? {
        guard let position else { return nil }
        return volume.min { abs(Double($0.id) - position) < abs(Double($1.id) - position) }
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

struct BusinessHomeContent: View {
    let model: BusinessHomePresentation
    @State private var selected: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            // Fresh numbers need no line. An old snapshot, or one that a refresh could not replace, says its age.
            if model.freshnessWarning || model.currentStatusUnknown {
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(model.freshness)
                        .foregroundStyle(model.freshnessWarning ? AnyShapeStyle(BoardStyle.attention) : AnyShapeStyle(.secondary))
                        .accessibilityIdentifier("business-home-freshness")
                    if model.currentStatusUnknown {
                        Text("Unknown")
                            .foregroundStyle(.secondary).accessibilityIdentifier("business-home-stale")
                    }
                }.font(.caption)
            }
            BentoBoard {
                // The breaches of the business are the first rows of the to-dos above (TodoCounts), so the page has
                // no tile of them.
                gmv.bento(8, 3, compact: (2, 3))
                if let notice = model.sourceNotice("ledger") {
                    BentoStat(title: "Money", value: notice, size: .small)
                        .accessibilityIdentifier("business-home-money-unavailable").bento(12, 1, compact: (2, 1))
                } else {
                    ForEach(model.moneyRows) { row in
                        moneyTile(row).bento(4, 2, compact: (row.id == "revenue" ? 2 : 1, 2))
                    }
                }
                ForEach(model.informational) { row in infoTile(row).bento(4, 1, compact: (2, 1)) }
                BentoStat(title: "Run rate", value: reportNumber(model.runRate, money: true), size: .medium)
                    .accessibilityIdentifier("business-home-run-rate").bento(4, 1, compact: (1, 1))
                BentoStat(title: "Daily average", value: reportNumber(model.dailyAverage, money: true), size: .medium)
                    .accessibilityIdentifier("business-home-daily-average").bento(4, 1, compact: (1, 1))
                places.bento(12, nil, compact: (2, nil))
            }
        }.foregroundStyle(BoardStyle.ink)
    }

    /// The places that Business opens, as the rows of one tile: a name needs no tile of its own. The repositories that
    /// Coconut follows are a setting, so You has them.
    private var places: some View {
        let rows = Self.functions.map { (title: $0.name, route: BusinessRoute.function($0.key), id: "function-" + $0.key) }
            + [(title: "History", route: .reports, id: "history")]
        return BentoTile(padding: 0) {
            VStack(spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                    if index > 0 { Divider().padding(.leading, BoardStyle.Space.l) }
                    NavigationLink(value: row.route) {
                        HStack(spacing: BoardStyle.Space.s) {
                            Text(row.title)
                            Spacer(minLength: 0)
                            BentoChevron()
                        }
                        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.m)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(HomePressStyle())
                    .accessibilityIdentifier("business-home-" + row.id)
                }
            }
        }
    }

    /// Support is a place of its own, a tab of the iPhone and a row of the Mac sidebar, so Business opens the other
    /// functions.
    private static let functions = BusinessFunction.builtIns.filter { $0.key != "support" }

    private func infoTile(_ row: BusinessHomePresentation.AttentionRow) -> some View {
        BentoLink(route: row.route) {
            if let value = row.value {
                BentoStat(title: row.title, value: value, unit: "open", size: .medium, chevron: true)
            } else {
                BentoStat(title: row.title, value: "—", note: row.detail, size: .medium, chevron: true)
            }
        }.accessibilityIdentifier("business-home-info-" + row.id)
    }

    private var gmv: some View {
        BentoTile(wash: model.gmvNotice == nil ? BentoColor.money : nil) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack(spacing: BoardStyle.Space.s) {
                    BentoLabel("GMV · 30 days")
                    Spacer(minLength: 0)
                    if let day = model.asOf("gmv") { Text(day).font(.caption).foregroundStyle(.tertiary).lineLimit(1) }
                    NavigationLink(value: BusinessRoute.report("overview")) { BentoChevron().padding(BoardStyle.Space.xs).contentShape(Rectangle()) }
                        .buttonStyle(.plain).accessibilityLabel("Open GMV report")
                }
                if let notice = model.gmvNotice {
                    Text(notice).font(.system(.title2, design: .rounded, weight: .semibold)).foregroundStyle(.secondary)
                        .accessibilityIdentifier("business-home-gmv-unavailable")
                    Spacer(minLength: 0)
                } else {
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) { total; delta }
                        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) { total; delta }
                    }
                    volumeChart
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: BoardStyle.Space.l) { Spacer(minLength: 0); reading }
                        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) { reading }
                    }
                }
            }
        }.accessibilityIdentifier("business-home-gmv")
    }
    @ViewBuilder private var total: some View {
        if let total = model.total { BentoNumber(reportNumber(total, money: true), size: .hero).accessibilityIdentifier("business-home-gmv-total") }
    }
    @ViewBuilder private var delta: some View {
        if let change = model.change {
            BentoDelta(percent: change, label: model.changeLabel).accessibilityIdentifier("business-home-gmv-delta")
        }
    }
    /// The day under the pointer, or the last published day with its published comparison.
    @ViewBuilder private var reading: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            if let point = model.selectedPoint(at: selected) {
                Text(point.label).font(.subheadline.weight(.semibold)).monospacedDigit().accessibilityIdentifier("business-home-gmv-selection")
            } else {
                if let latest = model.latest {
                    Text(latest).font(.subheadline.weight(.semibold)).monospacedDigit().accessibilityIdentifier("business-home-gmv-latest")
                }
                if let comparison = model.weekdayComparison {
                    Text(comparison).font(.caption).foregroundStyle(.secondary).accessibilityIdentifier("business-home-gmv-weekday")
                }
            }
        }.lineLimit(1)
    }
    private var volumeChart: some View {
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
            if let point = model.selectedPoint(at: selected) {
                RuleMark(x: .value("Selected day", point.id)).foregroundStyle(BoardStyle.ink.opacity(0.2)).lineStyle(StrokeStyle(lineWidth: 1))
                PointMark(x: .value("Day", point.id), y: .value("GMV", point.value)).foregroundStyle(BentoColor.money).symbolSize(70)
            }
        }
        .chartXSelection(value: $selected)
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
        .frame(minHeight: 96, maxHeight: .infinity).accessibilityIdentifier("business-home-gmv-chart")
    }

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
                    BentoNumber(row.formattedValue, size: row.value == nil ? .small : .large).padding(.top, BoardStyle.Space.s)
                    if let subtitle = row.subtitle {
                        Text(subtitle).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("business-home-money-" + row.id + "-subtitle")
                    }
                    if row.months.isEmpty { Spacer(minLength: 0) } else {
                        Chart(row.months) { month in
                            BarMark(x: .value("Month", month.date), y: .value(row.title, month.value), width: .ratio(0.72))
                                .foregroundStyle(month.value < 0 ? BoardStyle.attention : BentoColor.money.opacity(month.id == row.months.last?.id ? 1 : 0.55))
                                .cornerRadius(3)
                                .accessibilityLabel(BusinessHomePresentation.month(month.date) + ", " + row.title)
                                .accessibilityValue(reportNumber(month.value, money: true))
                        }.chartXAxis(.hidden).chartYAxis(.hidden).frame(minHeight: 40, maxHeight: .infinity).padding(.top, BoardStyle.Space.m)
                            .accessibilityIdentifier("business-home-money-" + row.id + "-chart")
                    }
                }
            }
        }.accessibilityIdentifier("business-home-money-" + row.id.replacingOccurrences(of: "_", with: "-"))
    }
}
