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
        let minutes = max(0, Int(age / 60))
        return "Updated \(minutes)m ago" + (age > 3600 ? " · Stale snapshot" : "")
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
        let title: String
        let detail: String?
        let symbol: String
        let route: BusinessRoute
        let unavailable: Bool
    }
    var attention: [AttentionRow] {
        [attentionRow("support", source: "support", title: "Support", count: data["support"]["gumroad_past_sla"].number,
                      suffix: "past SLA", detail: supportDetail, symbol: "lifepreserver", route: .function("support")),
         attentionRow("issues", source: "issues", title: "Product issues", count: data["issues"]["past_sla"].number,
                      suffix: "past SLA", detail: nil, symbol: "wrench.and.screwdriver", route: .report("support")),
         attentionRow("high-priority", source: "issues", title: "High priority", count: data["high_priority"]["count"].number,
                      suffix: "open", detail: nonempty(data["high_priority"]["top"][0]["title"].text),
                      symbol: "exclamationmark.circle", route: .report("support"))].compactMap { $0 }
    }
    private var supportDetail: String? {
        let first = data["support"]["top"][0]
        return nonempty([nonempty(first["subject"].text), first["hours"].number.map { Self.decimal($0) + "h" }]
            .compactMap { $0 }.joined(separator: " · "))
    }
    private func attentionRow(_ id: String, source: String, title: String, count: Double?, suffix: String,
                              detail: String?, symbol: String, route: BusinessRoute) -> AttentionRow? {
        if let notice = sourceNotice(source) {
            return AttentionRow(id: id, title: title + " · " + notice, detail: nil, symbol: symbol, route: route, unavailable: true)
        }
        if currentStatusUnknown || count == nil {
            return AttentionRow(id: id, title: title + " · Unknown", detail: currentStatusUnknown ? "Current status unknown" : "Not published",
                                symbol: symbol, route: route, unavailable: true)
        }
        guard let count, count > 0 else { return nil }
        return AttentionRow(id: id, title: title + " · " + Self.decimal(count) + " " + suffix,
                            detail: detail, symbol: symbol, route: route, unavailable: false)
    }
    var informational: [AttentionRow] {
        [("risk", "Risk review", "shield.lefthalf.filled", BusinessRoute.report("risk")),
         ("prs", "Pull requests", "arrow.triangle.pull", BusinessRoute.report("engineering"))].map { key, title, symbol, route in
            let notice = sourceNotice(key) ?? (currentStatusUnknown ? "Current status unknown" : nil)
            let count = data[key]["total"].number
            return AttentionRow(id: key, title: title, detail: notice ?? count.map { Self.decimal($0) + " open" } ?? "Not published",
                                symbol: symbol, route: route, unavailable: notice != nil || count == nil)
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
    var latest: String? {
        guard sourceNotice("gmv") == nil, let value = data["gmv"]["last_day_gmv"].number,
              let day = nonempty(data["gmv"]["last_day"].text) else { return nil }
        return Self.day(day) + " · " + reportNumber(value, money: true)
    }
    var weekdayComparison: String? {
        guard sourceNotice("gmv") == nil else { return nil }
        return data["gmv"]["same_weekday_pct"].number.map { Self.percent($0, signed: true) + " vs the same weekday" }
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
    var closedThrough: String? { sourceNotice("ledger") == nil ? nonempty(data["money"]["closed_through"].text) : nil }
    private func nonempty(_ text: String) -> String? { text.isEmpty ? nil : text }
    static func decimal(_ value: Double) -> String { value.formatted(.number.precision(.fractionLength(0...1))) }
    static func percent(_ value: Double, signed: Bool = false) -> String {
        value.formatted(.number.precision(.fractionLength(0...1)).sign(strategy: signed ? .always() : .automatic)) + "%"
    }
    static func day(_ date: String) -> String {
        // Calendar formatting only, never a financial comparison.
        let point = ReportPoint(date: date, value: 0, series: "")
        return point.day == .distantPast ? date : point.day.formatted(.dateTime.month(.abbreviated).day())
    }
    static func writeOff(_ value: Double) -> String {
        // Display units, not financial math. Keep the publisher's two-decimal-million precision.
        if abs(value) >= 1_000_000 {
            return "$" + (value / 1_000_000).formatted(.number.precision(.fractionLength(0...2))) + "M"
        }
        return value.formatted(.currency(code: "USD").precision(.fractionLength(0...2)))
    }
}

struct BusinessHomeContent: View {
    let model: BusinessHomePresentation
    @State private var selected: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 8) {
                Label(model.freshness, systemImage: "clock")
                    .foregroundStyle(model.freshnessWarning ? BoardStyle.attention : BoardStyle.ink.opacity(0.65))
                    .accessibilityIdentifier("business-home-freshness")
                if model.currentStatusUnknown {
                    Text("Current status unknown. Historical values below are the last published snapshot.")
                        .accessibilityIdentifier("business-home-stale")
                }
            }.font(.caption)
            attention
            gmv
            money
        }.foregroundStyle(BoardStyle.ink)
    }

    private var attention: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Attention").font(.title2.bold())
            if model.attention.isEmpty {
                Label { Text("All clear") } icon: { Image(systemName: "checkmark.circle").foregroundStyle(BoardStyle.green) }
                    .accessibilityIdentifier("business-home-all-clear")
            }
            ForEach(model.attention) { row in attentionLink(row, urgent: true) }
            Divider()
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 32) { ForEach(model.informational) { row in attentionLink(row, urgent: false) } }
                VStack(alignment: .leading, spacing: 16) { ForEach(model.informational) { row in attentionLink(row, urgent: false) } }
            }
        }.homeCard().accessibilityIdentifier("business-home-attention")
    }
    private func attentionLink(_ row: BusinessHomePresentation.AttentionRow, urgent: Bool) -> some View {
        NavigationLink(value: row.route) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: row.symbol).foregroundStyle(urgent ? BoardStyle.attention : BoardStyle.accent).frame(width: 20)
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.title).font(.subheadline.weight(.semibold))
                    if let detail = row.detail { Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityElement(children: .combine)
            .accessibilityIdentifier("business-home-" + (urgent ? "attention-" : "info-") + row.id)
    }
    private var gmv: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("GMV").font(.title2.bold())
                Spacer()
                NavigationLink(value: BusinessRoute.report("overview")) { Image(systemName: "arrow.up.right") }
                    .accessibilityLabel("Open GMV report")
            }
            if let notice = model.gmvNotice {
                Text(notice).accessibilityIdentifier("business-home-gmv-unavailable")
            } else {
                if let change = model.change {
                    HStack(spacing: 6) {
                        Image(systemName: change > 0 ? "arrow.up.right" : change < 0 ? "arrow.down.right" : "arrow.right")
                            .foregroundStyle(change >= 0 ? BoardStyle.green : BoardStyle.attention)
                        Text(BusinessHomePresentation.percent(change, signed: true)).monospacedDigit()
                        if let label = model.changeLabel { Text(label) }
                    }.font(.caption).padding(8).background(BoardStyle.paper, in: Capsule())
                        .accessibilityElement(children: .combine).accessibilityIdentifier("business-home-gmv-delta")
                }
                volumeChart
                HStack(spacing: 16) {
                    Label("Current period", systemImage: "line.diagonal")
                    if !model.previousVolume.isEmpty { Text("┄ Prior period").foregroundStyle(.secondary) }
                }.font(.caption2)
                if let point = model.selectedPoint(at: selected) {
                    Text(point.label).font(.headline).monospacedDigit().accessibilityIdentifier("business-home-gmv-selection")
                } else if let latest = model.latest {
                    Text(latest).font(.headline).monospacedDigit().accessibilityIdentifier("business-home-gmv-latest")
                }
                if selected == nil, let comparison = model.weekdayComparison {
                    Text(comparison).font(.caption).accessibilityIdentifier("business-home-gmv-weekday")
                }
            }
        }.homeCard().accessibilityIdentifier("business-home-gmv")
    }
    private var volumeChart: some View {
        Chart {
            // Align each period by its published ordinal day; both lines share one y-scale.
            ForEach(model.previousVolume) { point in
                LineMark(x: .value("Day in period", point.id), y: .value("GMV", point.value), series: .value("Period", "Previous"))
                    .foregroundStyle(BoardStyle.ink.opacity(0.3)).lineStyle(StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                    .accessibilityLabel("Prior period, " + BusinessHomePresentation.day(point.date))
                    .accessibilityValue(reportNumber(point.value, money: true))
            }
            ForEach(model.volume) { point in
                LineMark(x: .value("Day in period", point.id), y: .value("GMV", point.value), series: .value("Period", "Current"))
                    .foregroundStyle(BoardStyle.ink).lineStyle(StrokeStyle(lineWidth: 2.5))
                    .accessibilityLabel(BusinessHomePresentation.day(point.date)).accessibilityValue(reportNumber(point.value, money: true))
            }
            if let point = model.selectedPoint(at: selected) {
                RuleMark(x: .value("Selected day", point.id)).foregroundStyle(BoardStyle.ink.opacity(0.25))
                PointMark(x: .value("Day", point.id), y: .value("GMV", point.value)).foregroundStyle(BoardStyle.ink)
            }
        }
        .chartXSelection(value: $selected)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 4)) { value in
                AxisValueLabel {
                    if let index = value.as(Int.self), let point = model.volume.first(where: { $0.id == index }) {
                        Text(BusinessHomePresentation.day(point.date))
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { value in
                AxisGridLine()
                AxisValueLabel { if let n = value.as(Double.self) { Text(reportNumber(n, money: true)) } }
            }
        }
        .frame(height: 220).accessibilityIdentifier("business-home-gmv-chart")
    }
    private var money: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                Text("Money").font(.title2.bold())
                Spacer()
                Text("LTM · USD").font(.caption).foregroundStyle(.secondary)
            }
            if let notice = model.sourceNotice("ledger") {
                Text(notice).accessibilityIdentifier("business-home-money-unavailable")
            } else {
                ForEach(model.moneyRows) { row in
                    NavigationLink(value: BusinessRoute.report("accounting")) {
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 24) {
                                moneyLabel(row).frame(minWidth: 180, maxWidth: .infinity, alignment: .leading)
                                miniBars(row).frame(width: 220)
                                moneyValue(row).frame(minWidth: 130, alignment: .trailing)
                            }
                            VStack(alignment: .leading, spacing: 12) {
                                HStack(alignment: .top) { moneyLabel(row); Spacer(); moneyValue(row) }
                                miniBars(row)
                            }
                        }.padding(.vertical, 10).contentShape(Rectangle())
                    }.buttonStyle(.plain).accessibilityIdentifier("business-home-money-" + row.id.replacingOccurrences(of: "_", with: "-"))
                    if row.id != "net_income" { Divider() }
                }
                if let closed = model.closedThrough { Text("Books closed through " + closed).font(.caption).foregroundStyle(.secondary) }
            }
        }.homeCard().accessibilityIdentifier("business-home-money")
    }
    private func moneyLabel(_ row: BusinessHomePresentation.MoneyRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(row.title).font(.headline)
            if let subtitle = row.subtitle {
                Text(subtitle).font(.caption).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("business-home-money-" + row.id + "-subtitle")
            }
        }
    }
    private func moneyValue(_ row: BusinessHomePresentation.MoneyRow) -> some View {
        Text(row.formattedValue).font(.title3.weight(.semibold)).monospacedDigit().fixedSize()
    }
    @ViewBuilder private func miniBars(_ row: BusinessHomePresentation.MoneyRow) -> some View {
        if !row.months.isEmpty {
            Chart(row.months) { month in
                BarMark(x: .value("Month", month.date), y: .value(row.title, month.value))
                    .foregroundStyle(BoardStyle.ink.opacity(0.65)).cornerRadius(2)
                    .accessibilityLabel(month.date + ", " + row.title).accessibilityValue(reportNumber(month.value, money: true))
            }.chartXAxis(.hidden).chartYAxis(.hidden).frame(height: 44)
                .accessibilityIdentifier("business-home-money-" + row.id + "-chart")
        }
    }
}

private extension View {
    func homeCard() -> some View {
        padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
    }
}
