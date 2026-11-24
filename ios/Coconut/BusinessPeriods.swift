import SwiftUI
import Charts

/// The published period report: the same figures as Home, cut by day, month and year.
/// Every share and margin arrives from the generator, so this type only formats what it is given.
struct BusinessPeriods: Sendable {
    struct Day: Identifiable, Sendable {
        let date: String
        let gmv: Double
        var id: String { date }
    }
    struct Month: Identifiable, Sendable {
        let month: String
        let revenue: Double?
        let ebitda: Double?
        let netIncome: Double?
        let margin: Double?
        var id: String { month }
    }
    struct Year: Identifiable, Sendable {
        let year: String
        let months: Int
        let revenue: Double?
        let ebitda: Double?
        let netIncome: Double?
        let margin: Double?
        var partial: Bool { months < 12 }
        var id: String { year }
    }

    let days: [Day]
    let asOf: String?
    let weekday: String?
    let lastDayGmv: Double?
    let averageGmv: Double?
    let vsAverage: Double?
    let vsWeekday: Double?
    let months: [Month]
    let revenue3m: Double?
    let revenuePrev3m: Double?
    let revenue3mPercent: Double?
    let closedThrough: String?
    let years: [Year]

    init(report: BusinessReport) {
        let data = report.data
        let daily = data["daily"], monthly = data["monthly"], yearly = data["yearly"]
        days = daily["days"].array.compactMap { row in
            guard let value = row["gmv"].number else { return nil }
            return Day(date: row["date"].text, gmv: value)
        }
        asOf = daily["as_of"].text.isEmpty ? nil : daily["as_of"].text
        weekday = daily["weekday"].text.isEmpty ? nil : daily["weekday"].text
        lastDayGmv = daily["last_day_gmv"].number
        averageGmv = daily["avg_gmv"].number
        vsAverage = daily["vs_avg_pct"].number
        vsWeekday = daily["vs_weekday_pct"].number
        months = monthly["months"].array.compactMap { row in
            guard !row["month"].text.isEmpty else { return nil }
            return Month(month: row["month"].text, revenue: row["revenue"].number, ebitda: row["ebitda"].number,
                         netIncome: row["net_income"].number, margin: row["margin_pct"].number)
        }
        revenue3m = monthly["revenue_3m"].number
        revenuePrev3m = monthly["revenue_prev_3m"].number
        revenue3mPercent = monthly["revenue_3m_pct"].number
        closedThrough = monthly["closed_through"].text.isEmpty ? nil : monthly["closed_through"].text
        years = yearly["years"].array.compactMap { row in
            let year = row["year"].text
            guard !year.isEmpty else { return nil }
            return Year(year: year, months: Int(row["months"].number ?? 0), revenue: row["revenue"].number,
                        ebitda: row["ebitda"].number, netIncome: row["net_income"].number, margin: row["margin_pct"].number)
        }
    }
}

/// Formatting only: the numbers below the labels are the published ones, untouched.
enum BusinessPeriodFormat {
    static func money(_ value: Double?, compact: Bool = true) -> String { BusinessFormat.money(value, compact: compact) }
    static func percent(_ value: Double?, signed: Bool = false) -> String { BusinessFormat.percent(value, signed: signed) }
    /// "2026-10" as "Oct 2026"; "2026Q4" as "Q4 2026"; "2021" stays as it is.
    static func label(_ raw: String) -> String { BusinessFormat.month(raw) }
    static func day(_ raw: String) -> String { BusinessFormat.day(raw) }
}

struct BusinessReportsView: View {
    @Environment(AppStore.self) private var store
    @State private var report: BusinessReport?
    @State private var error: String?

    private var shownReport: BusinessReport? { store.businessReports["periods"] ?? report }

    var body: some View {
        BoardPage(width: .wide, spacing: BoardStyle.Space.xl) {
            // The three cuts one under the other, with no control between them: the person found that nobody taps
            // such tabs (2026-11-24). The long list of the months comes last, so it hides no other cut.
            if let shownReport {
                let periods = BusinessPeriods(report: shownReport)
                daily(periods)
                yearly(periods)
                monthly(periods)
            } else if error == nil {
                BoardLoading(title: "Loading reports", height: 280)
            }

            if let error {
                BoardLoadNotice(message: error) { Task { await load() } }
            }
        }
        .navigationTitle("History")
        .task(id: "\(store.connection)/\(store.businessChanges)") { await load() }
        .refreshable { await load() }
        .onChange(of: store.businessGeneration) { _, _ in report = nil; error = nil }
    }

    /// The values of the money axis in the one form of the figures, "$250.0K" and not "250,000", at the trailing edge
    /// as on each other chart.
    private static var moneyAxis: some AxisContent {
        AxisMarks(values: .automatic(desiredCount: 3)) { value in
            AxisGridLine().foregroundStyle(BoardStyle.line)
            AxisValueLabel { if let number = value.as(Double.self) { Text(BusinessFormat.money(number)) } }
        }
    }

    private func load() async {
        do {
            let loaded = try await store.businessReport("periods")
            guard !Task.isCancelled else { return }
            report = loaded; error = nil
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    // MARK: - Daily

    private func daily(_ periods: BusinessPeriods) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            BusinessReportHeadline(title: periods.asOf.map(BusinessPeriodFormat.day) ?? "Last day",
                                   value: BusinessPeriodFormat.money(periods.lastDayGmv, compact: false),
                                   note: periods.weekday)
            HStack(spacing: BoardStyle.Space.m) {
                BusinessReportDelta(value: periods.vsAverage, caption: "vs 30 days")
                BusinessReportDelta(value: periods.vsWeekday, caption: "vs weekday")
            }
            if periods.days.count > 1 {
                Chart(periods.days) { day in
                    BarMark(x: .value("Day", day.date), y: .value("GMV", day.gmv))
                        .foregroundStyle(day.id == periods.days.last?.id ? BoardStyle.accent : Color.secondary.opacity(0.35))
                }
                .frame(height: 150)
                .chartXAxis {
                    // A date for each seventh day, counted from the fourth last. A date for each of the thirty bars
                    // ran into the next one, and a date under the last bar is cut at the edge of the chart.
                    let days = periods.days
                    AxisMarks(values: days.indices.filter { (days.count - 1 - $0) % 7 == 3 }.map { days[$0].date }) { value in
                        AxisValueLabel {
                            if let raw = value.as(String.self) { Text(BusinessFormat.axisDay(raw)).font(.caption2) }
                        }
                    }
                }
                .chartYAxis { Self.moneyAxis }
                .accessibilityIdentifier("business-reports-daily-chart")
                // Data only, in the words of the tile of Business: the name and the number.
                Text("Daily average " + BusinessFormat.money(periods.averageGmv)).font(.callout).monospacedDigit()
            }
        }
    }

    // MARK: - Monthly

    private func monthly(_ periods: BusinessPeriods) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            if !periods.months.isEmpty {
                // A month with no published net income draws no bar, so a gap never reads as a loss of zero.
                Chart(periods.months.filter { $0.netIncome != nil }) { month in
                    BarMark(x: .value("Month", month.month), y: .value("Net income", month.netIncome ?? 0))
                        .foregroundStyle(month.id == periods.months.last?.id ? BoardStyle.accent : Color.secondary.opacity(0.35))
                }
                .frame(height: 160)
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 4)) { value in
                        AxisValueLabel {
                            // The year is in the list below and in the section title, so an axis tick names only its month.
                            if let raw = value.as(String.self) { Text(BusinessFormat.shortMonth(raw)).font(.caption2) }
                        }
                    }
                }
                .chartYAxis { Self.moneyAxis }
                .accessibilityIdentifier("business-reports-monthly-chart")
            }
            if let revenue3m = periods.revenue3m, let previous = periods.revenuePrev3m {
                let now: String = BusinessPeriodFormat.money(revenue3m)
                let before: String = BusinessPeriodFormat.money(previous)
                let change: String = BusinessPeriodFormat.percent(periods.revenue3mPercent, signed: true)
                // Data only: the name, the two quarters and the change.
                Text("Revenue · 3 mo \(now) vs \(before) · \(change)").font(.callout).monospacedDigit()
            }
            ForEach(periods.months.reversed()) { month in
                BusinessReportRow(title: BusinessPeriodFormat.label(month.month),
                                  value: BusinessPeriodFormat.money(month.netIncome),
                                  detail: "Net income · " + BusinessPeriodFormat.percent(month.margin) + " margin",
                                  trailing: BusinessPeriodFormat.money(month.revenue) + " revenue")
            }
        }
    }

    // MARK: - Yearly

    private func yearly(_ periods: BusinessPeriods) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            if !periods.years.isEmpty {
                Chart(periods.years.filter { $0.netIncome != nil }) { year in
                    BarMark(x: .value("Year", year.year), y: .value("Net income", year.netIncome ?? 0))
                        .foregroundStyle(year.id == periods.years.last?.id ? BoardStyle.accent : Color.secondary.opacity(0.35))
                }
                .frame(height: 160)
                // The axis of the two other cuts: the name of each bar, and no line between the bars.
                .chartXAxis {
                    AxisMarks { value in
                        AxisValueLabel { if let year = value.as(String.self) { Text(year).font(.caption2) } }
                    }
                }
                .chartYAxis { Self.moneyAxis }
                .accessibilityIdentifier("business-reports-yearly-chart")
            }
            ForEach(periods.years.reversed()) { year in
                BusinessReportRow(title: year.year + (year.partial ? " · through \(periods.closedThrough.map(BusinessPeriodFormat.month) ?? "today")" : ""),
                                  value: BusinessPeriodFormat.money(year.netIncome),
                                  detail: "Net income · " + BusinessPeriodFormat.percent(year.margin) + " margin",
                                  trailing: BusinessPeriodFormat.money(year.revenue) + " revenue")
            }
        }
    }
}

struct BusinessReportHeadline: View {
    let title: String
    let value: String
    var note: String?
    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(title).font(.subheadline).foregroundStyle(.secondary)
            Text(value).font(.system(.largeTitle, design: .rounded, weight: .semibold)).monospacedDigit()
            if let note { Text(note).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct BusinessReportDelta: View {
    let value: Double?
    let caption: String
    /// A figure that did not arrive has no direction: it reads as a dash in the colour of a caption, not as a rise.
    private var known: Bool { value?.isFinite == true }
    private var up: Bool { (value ?? 0) >= 0 }
    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            HStack(spacing: BoardStyle.Space.xs) {
                if known {
                    Image(systemName: up ? "arrow.up.right" : "arrow.down.right").font(.caption.weight(.semibold))
                }
                Text(BusinessPeriodFormat.percent(value, signed: true)).font(.subheadline.weight(.semibold)).monospacedDigit()
            }
            .foregroundStyle(known ? (up ? BoardStyle.positive : BoardStyle.attention) : Color.secondary)
            Text(caption).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct BusinessReportRow: View {
    let title: String
    let value: String
    let detail: String
    var trailing: String?
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(title)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: BoardStyle.Space.s)
            VStack(alignment: .trailing, spacing: BoardStyle.Space.line) {
                Text(value).monospacedDigit()
                if let trailing { Text(trailing).font(.caption).foregroundStyle(.secondary).monospacedDigit() }
            }
        }
    }
}

extension BusinessPeriodFormat {
    /// "2026-10" as "October" for the partial-year note.
    static func month(_ raw: String) -> String {
        let parts = raw.split(separator: "-")
        guard parts.count == 2, let month = Int(parts[1]), (1...12).contains(month) else { return raw }
        let names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
        return names[month - 1]
    }
}
