import SwiftUI
import Charts

struct BusinessWorkspaceView: View {
    let function: String
    @State private var tab = "overview"
    private var hasReport: Bool { function == "accounting" || function == "support" }

    var body: some View {
        VStack(spacing: 0) {
            Picker("View", selection: $tab) {
                if hasReport { Text("Overview").tag("overview") }
                Text("Work").tag("work")
                Text("Library").tag("library")
            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 360)
                .padding(.horizontal, BentoPage.padding).padding(.vertical, BoardStyle.Space.m)
            if tab == "library" { BusinessReportLibrary(function: function) }
            else if tab == "work" || !hasReport { BusinessWorkListView(function: function) }
            else { BusinessReportView(key: function) }
        }
        .background(BoardStyle.paper)
        .navigationTitle(BusinessFunction.name(function))
        .onAppear { if !hasReport && tab == "overview" { tab = "work" } }
    }
}

/// The reports as tiles. The Library shows them over the pages of the agents, and a function shows its own.
struct BusinessReportTiles: View {
    var function: String? = nil
    static let details = ["overview": "GMV, money and queues", "accounting": "Revenue, EBITDA, net income", "support": "Queues and resolution times",
                          "risk": "Review queue and chargebacks", "engineering": "Open pull requests"]
    private var keys: [String] {
        BusinessReport.keys.filter { function == nil || $0 == function || $0 == "overview" || (function == "support" && $0 == "risk") }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            Text("Reports").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary).accessibilityAddTraits(.isHeader)
            BentoBoard {
                ForEach(keys, id: \.self) { key in
                    BentoLink(route: .report(key)) {
                        BentoPlace(title: BusinessReport.titles[key] ?? key, detail: Self.details[key], symbol: BusinessReport.symbols[key] ?? "chart.bar")
                    }.accessibilityIdentifier("business-report-" + key).bento(3, 1, compact: (1, 1))
                }
                if function == nil || function == "accounting" {
                    BentoLink(route: .ledger) {
                        BentoPlace(title: "Beancount ledger", detail: "The books, file by file", symbol: "text.document", tint: BentoColor.money)
                    }.accessibilityIdentifier("business-report-ledger").bento(3, 1, compact: (1, 1))
                }
            }
        }
    }
}

struct BusinessReportLibrary: View {
    var function: String? = nil
    var body: some View {
        ScrollView {
            BusinessReportTiles(function: function)
                .padding(BentoPage.padding).frame(maxWidth: BentoPage.width, alignment: .leading).frame(maxWidth: .infinity)
        }.background(BoardStyle.paper)
    }
}

struct BusinessReportView: View {
    @Environment(AppStore.self) private var store
    let key: String
    @State private var report: BusinessReport?
    private var shownReport: BusinessReport? { store.businessReports[key] ?? report }
    @State private var error: String?
    @State private var details = false

    init(key: String, initial: BusinessReport? = nil) { self.key = key; _report = State(initialValue: initial) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
                if let report = shownReport {
                    BusinessReportContent(key: key, report: report, sample: store.isDemo) { details = true }
                } else if error == nil { ProgressView("Loading report").frame(maxWidth: .infinity, minHeight: 360) }
                if let error {
                    ContentUnavailableView {
                        Label("Report unavailable", systemImage: "chart.bar.xaxis")
                    } description: { Text(error) } actions: {
                        Button("Try again") { Task { await load() } }
                        NavigationLink("GitHub connection") { GitHubSettingsView() }
                    }
                }
            }.padding(BentoPage.padding).frame(maxWidth: BentoPage.width, alignment: .leading).frame(maxWidth: .infinity)
        }
        .background(BoardStyle.paper).navigationTitle(BusinessReport.titles[key] ?? key).inlineNavigationTitle()
        .task(id: "\(key)/\(store.connection)/\(store.businessChanges)") { await load() }
        .onChange(of: store.businessGeneration) { _, _ in report = nil }
        .refreshable { await load() }
        .sheet(isPresented: $details) {
            NavigationStack {
                if let report = shownReport { BusinessDataExplorer(value: report.data).navigationTitle("Report data") }
            }.frame(minWidth: 380, minHeight: 420)
        }
    }

    private func load() async {
        do {
            let loaded = try await store.businessReport(key)
            guard !Task.isCancelled else { return }
            report = loaded; error = nil
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}

/// One report without its scroll view: the status line, the grid of its kind and the source.
struct BusinessReportContent: View {
    let key: String
    let report: BusinessReport
    var sample = false
    var explore: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            status
            if report.stale {
                Label(report.generatedAt == nil ? "This snapshot has no timestamp. Its freshness is unknown." : "This snapshot is over an hour old. Check the publisher before acting.", systemImage: "clock.badge.exclamationmark")
                    .font(.footnote).foregroundStyle(BoardStyle.attention)
                    .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s).frame(maxWidth: .infinity, alignment: .leading)
                    .background(BoardStyle.attention.opacity(0.1), in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
            }
            switch key {
            case "support": SupportDashboard(data: report.data)
            case "risk": RiskDashboard(data: report.data)
            case "engineering": EngineeringDashboard(data: report.data)
            default: MoneyDashboard(data: report.data, accounting: key == "accounting")
            }
            HStack {
                if let url = URL(string: report.source.url) {
                }
                Spacer()
                Button("Explore all data", systemImage: "tablecells", action: explore).buttonStyle(.borderless)
            }.font(.caption).foregroundStyle(.secondary)
        }
    }

    /// One quiet line in place of a headline: how fresh the snapshot is and what its numbers are.
    private var status: some View {
        HStack(spacing: 6) {
            Circle().fill(report.stale ? BoardStyle.attention : BoardStyle.positive).frame(width: 7, height: 7).accessibilityHidden(true)
            Text(report.generatedAt?.apiDate.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "Unknown")
            Spacer(minLength: BoardStyle.Space.s)
            if sample {
                Text("Sample data").font(.caption2.weight(.semibold)).padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 3)
                    .background(Color.primary.opacity(0.07), in: Capsule())
            }
        }.font(.caption).foregroundStyle(.secondary).lineLimit(1)
    }
}

private struct MoneyDashboard: View {
    let data: BusinessValue
    let accounting: Bool
    private var ledger: BusinessValue { data["ledger"] }
    private var money: BusinessValue { data["money"] }
    private var gmv: BusinessValue { data["gmv"] }
    private func monthly(_ series: String) -> [Double] {
        ledger["months"].array.compactMap { ledger["per_month"][$0.text][series].number }
    }
    private var financials: [ReportPoint] {
        ledger["months"].array.flatMap { month in
            ["revenue", "ebitda", "net_income"].compactMap { series in
                ledger["per_month"][month.text][series].number.map { ReportPoint(date: month.text + "-01", value: $0, series: series == "revenue" ? "Revenue" : series == "ebitda" ? "EBITDA" : "Net income") }
            }
        }
    }
    private var volume: [ReportPoint] {
        gmv["series"].array.compactMap { row in row[1].number.map { ReportPoint(date: row[0].text, value: $0, series: "GMV") } }
    }
    private var closed: String { ledger["closed_through"].text.isEmpty ? "—" : BusinessHomePresentation.month(ledger["closed_through"].text) }

    var body: some View {
        BentoBoard {
            if accounting {
                ledgerStat("Revenue", "revenue", symbol: "arrow.down.left.circle", note: revenueNote, tall: true).bento(4, 2, compact: (2, 2))
                ledgerStat("EBITDA", "ebitda", symbol: "chart.bar", tall: true).bento(4, 2, compact: (1, 2))
                ledgerStat("Net income", "net_income", symbol: "equal.circle", tall: true).bento(4, 2, compact: (1, 2))
                profit.bento(8, 3, compact: (2, 3))
                BentoStat(title: "EBITDA margin", symbol: "percent", value: percent(money["ebitda_margin_pct"]), size: .medium).bento(2, 1, compact: (1, 1))
                BentoStat(title: "Net margin", symbol: "percent", value: percent(money["net_margin_pct"]), size: .medium).bento(2, 1, compact: (1, 1))
                BentoStat(title: "Write-offs", symbol: "minus.circle", value: money["impairment_total"].number.map { BusinessHomePresentation.writeOff($0) } ?? "—", size: .medium).bento(4, 1, compact: (2, 1))
                books.bento(4, 1, compact: (2, 1))
            } else {
                sales.bento(8, 3, compact: (2, 3))
                BentoLink(route: .report("accounting")) { ledgerStat("Revenue", "revenue", symbol: "arrow.down.left.circle", chevron: true) }
                    .bento(4, 1, compact: (2, 1))
                BentoLink(route: .report("accounting")) { ledgerStat("EBITDA", "ebitda", symbol: "chart.bar", chevron: true) }
                    .bento(4, 1, compact: (2, 1))
                BentoLink(route: .report("accounting")) { ledgerStat("Net income", "net_income", symbol: "equal.circle", chevron: true) }
                    .bento(4, 1, compact: (2, 1))
                queue("Support", value: sum(data["support"]["gumroad"], data["support"]["flexile"]), overdue: sum(data["support"]["gumroad_past_sla"], data["support"]["flexile_past_sla"]), route: .function("support"), symbol: "lifepreserver")
                    .bento(4, 1, compact: (2, 1))
                queue("Product issues", value: data["issues"]["total"].number, overdue: data["issues"]["past_sla"].number, route: .report("support"), symbol: "wrench.and.screwdriver")
                    .bento(4, 1, compact: (1, 1))
                queue("Risk review", value: data["risk"]["total"].number, overdue: data["risk"]["past_sla"].number, route: .report("risk"), symbol: "shield.lefthalf.filled")
                    .bento(4, 1, compact: (1, 1))
                profit.bento(8, 3, compact: (2, 3))
                BentoStat(title: "Run rate", symbol: "calendar", value: reportNumber(gmv["annualized_gmv"].number, money: true), size: .medium)
                    .bento(4, 1, compact: (1, 1))
                BentoStat(title: "Daily average", symbol: "sum", value: reportNumber(gmv["trailing_avg_gmv"].number, money: true), size: .medium)
                    .bento(4, 1, compact: (1, 1))
                books.bento(4, 1, compact: (2, 1))
            }
        }
    }

    private var sales: some View {
        ReportChart(title: "GMV · 30 days", symbol: "chart.line.uptrend.xyaxis", points: volume, money: true,
                    headline: reportNumber(gmv["gmv_30d"].number, money: true), change: gmv["change_pct"].number, changeLabel: gmv["change_label"].text, wash: BentoColor.money)
    }
    private var profit: some View {
        ReportChart(title: "Revenue and profit", symbol: "chart.bar.xaxis", points: financials, money: true, bars: true)
    }
    private func ledgerStat(_ title: String, _ series: String, symbol: String, note: String? = nil, tall: Bool = false, chevron: Bool = false) -> some View {
        let value = ledger["ltm_" + series].number
        return BentoStat(title: title, symbol: symbol, tint: BentoColor.money, value: reportNumber(value, money: true), note: value == nil ? "Not published" : note,
                         wash: tall && series == "revenue" ? BentoColor.money : nil, chevron: chevron, trend: monthly(series), trendBars: true, trendPlace: tall ? .bottom : .trailing)
    }
    /// The published change of the last three months, when the snapshot has one.
    private var revenueNote: String {
        guard let change = money["revenue_3m_pct"].number else { return "" }
        return BusinessHomePresentation.percent(change, signed: true) + " 3 mo"
    }
    private var books: some View {
        BentoLink(route: .ledger) {
            BentoStat(title: "Books closed through", symbol: "checkmark.seal", tint: BentoColor.money, value: closed, size: .medium, chevron: true)
        }.accessibilityLabel("Open Beancount ledger")
    }
    private func queue(_ title: String, value: Double?, overdue: Double?, route: BusinessRoute, symbol: String) -> some View {
        let late = (overdue ?? 0) > 0
        return BentoLink(route: route) {
            BentoStat(title: title, symbol: symbol, tint: late ? BoardStyle.attention : .secondary, value: reportNumber(value), unit: "open",
                      note: overdue.map { $0 > 0 ? "\(Int($0)) past SLA" : "Within SLA" } ?? "SLA unavailable",
                      noteTint: late ? BoardStyle.attention : overdue == nil ? .secondary : BoardStyle.positive, wash: late ? BoardStyle.attention : nil, chevron: true)
        }
    }
    private func percent(_ value: BusinessValue) -> String { value.number.map { BusinessHomePresentation.percent($0) } ?? "—" }
    private func sum(_ a: BusinessValue, _ b: BusinessValue) -> Double? { guard let a = a.number, let b = b.number else { return nil }; return a + b }
}

private struct SupportDashboard: View {
    let data: BusinessValue
    @State private var queue = "support"
    @State private var status = "open"
    @State private var search = ""
    @State private var shown = 25
    private var selected: BusinessValue { data[queue] }
    private var agents: [(name: String, count: Double)] {
        let value = selected["closedCount"]["byAgent"]
        let rows = value.array.isEmpty ? value.object.sorted(by: { $0.key < $1.key }).map(\.value) : value.array
        return rows.compactMap { row in row["count"].number.map { (row["displayName"].text.isEmpty ? "Unknown closer" : row["displayName"].text, $0) } }
            .sorted { $0.count > $1.count }
    }
    private var items: [BusinessValue] {
        selected[status].array.filter { search.isEmpty || $0["subject"].text.localizedCaseInsensitiveContains(search) }
    }
    private var resolution: SupportResolution {
        SupportResolution(closed: selected["closed"].array, reference: data["generatedAt"].text.apiDate)
    }
    private var volume: [ReportPoint] {
        selected["dailyVolume"].array.compactMap { row in row["count"].number.map { ReportPoint(date: row["date"].text, value: $0, series: "Conversations") } }
    }

    var body: some View {
        let counts = selected["counts"]
        let waiting = counts["unassigned"].number
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            Picker("Queue", selection: $queue) { Text("Customer support").tag("support"); Text("Product issues").tag("issues") }
                .pickerStyle(.segmented).labelsHidden().bentoLeadingControl()
            BentoBoard {
                BentoStat(title: "Open", symbol: "tray.full", value: reportNumber(counts["all"].number),
                          note: counts["assigned"].number.map { "\(Int($0)) assigned" } ?? "In this queue", trend: volume.map(\.value))
                    .bento(4, 1, compact: (2, 1))
                BentoStat(title: "Unassigned", symbol: "person.crop.circle.badge.questionmark", tint: (waiting ?? 0) > 0 ? BoardStyle.attention : .secondary,
                          value: reportNumber(waiting), wash: (waiting ?? 0) > 0 ? BoardStyle.attention : nil)
                    .bento(4, 1, compact: (1, 1))
                BentoStat(title: "Closed · 24 hours", symbol: "checkmark.circle", tint: BoardStyle.positive, value: reportNumber(selected["closedCount"]["total"].number)).bento(4, 1, compact: (1, 1))
                ReportChart(title: "Incoming volume", symbol: "tray.and.arrow.down", points: volume, bars: true)
                    .bento(8, 2, compact: (2, 2))
                BentoStat(title: "Median resolution", symbol: "timer", value: SupportFormat.duration(resolution.median))
                    .bento(4, 1, compact: (1, 1))
                BentoStat(title: "90th percentile", symbol: "gauge.with.dots.needle.67percent", value: SupportFormat.duration(resolution.p90),
                          note: "\(resolution.minutes.count) resolved in 24 hours").bento(4, 1, compact: (1, 1))
                histogram.bento(6, 2, compact: (2, 2))
                team.bento(6, agents.count > 5 ? nil : 2, compact: (2, nil))
                conversations.bento(12, nil, compact: (2, nil))
            }
        }
        .onChange(of: queue) { _, _ in shown = 25 }
        .onChange(of: status) { _, _ in shown = 25 }
    }

    private var histogram: some View {
        BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack {
                    BentoLabel("Time to resolution", symbol: "chart.bar")
                    Spacer(minLength: 0)
                }
                Chart(Array(resolution.buckets.enumerated()), id: \.offset) { index, count in
                    BarMark(x: .value("Time", SupportResolution.labels[index]), y: .value("Resolved", count), width: .ratio(0.7))
                        .foregroundStyle(index >= 6 ? BoardStyle.attention : BentoColor.money).cornerRadius(4)
                        .accessibilityLabel(SupportResolution.labels[index]).accessibilityValue("\(count) resolved")
                        .annotation(position: .top, spacing: 2) {
                            if count > 0 { Text("\(count)").font(.caption2.weight(.semibold)).monospacedDigit().foregroundStyle(.secondary) }
                        }
                }
                .chartYAxis(.hidden)
                // The short name of a bucket is its lower bound, so eight labels fit the width of an iPhone.
                .chartXAxis {
                    AxisMarks { value in
                        AxisValueLabel {
                            if let label = value.as(String.self), let index = SupportResolution.labels.firstIndex(of: label) {
                                Text(SupportResolution.shortLabels[index]).font(.caption2)
                            }
                        }
                    }
                }
                .frame(minHeight: 80, maxHeight: .infinity)
            }
        }
    }

    private var team: some View {
        BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                HStack {
                    BentoLabel("Closed by", symbol: "person.2")
                    Spacer(minLength: 0)
                    Text("24 hours").font(.caption).foregroundStyle(.tertiary)
                }
                if agents.isEmpty { Text("No closures published.").font(.subheadline).foregroundStyle(.secondary) }
                let most = agents.map(\.count).max() ?? 1
                ForEach(Array(agents.enumerated()), id: \.offset) { _, agent in
                    VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                        HStack {
                            Text(agent.name).font(.subheadline).lineLimit(1)
                            Spacer(minLength: BoardStyle.Space.s)
                            Text(reportNumber(agent.count)).font(.subheadline.weight(.semibold)).monospacedDigit()
                        }
                        BentoSplit(parts: [(agent.count, BentoColor.money), (max(most - agent.count, 0), Color.primary.opacity(0.07))])
                    }.accessibilityElement(children: .combine)
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var conversations: some View {
        BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: BoardStyle.Space.m) { conversationsTitle; Spacer(minLength: 0); searchField.frame(width: 240); statusPicker }
                    VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                        HStack { conversationsTitle; Spacer(minLength: 0); statusPicker }
                        searchField
                    }
                }
                if items.isEmpty { Text("No conversations in this snapshot.").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, BoardStyle.Space.s) }
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(items.prefix(shown).enumerated()), id: \.offset) { index, item in
                        if index > 0 { Divider() }
                        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) {
                            Circle().fill(status == "open" ? BoardStyle.attention : BoardStyle.positive).frame(width: 7, height: 7)
                                .alignmentGuide(.firstTextBaseline) { $0[.bottom] + 1 }.accessibilityHidden(true)
                            Text(item["subject"].text).font(.subheadline).lineLimit(2).textSelection(.enabled)
                            Spacer(minLength: BoardStyle.Space.s)
                            Text([item["queue"].text.capitalized, item["createdAt"].text.apiDate?.formatted(date: .abbreviated, time: .omitted) ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            if queue == "issues", let url = SupportFormat.issueURL(item) {
                                Link(destination: url) { Image(systemName: "arrow.up.right").font(.caption.weight(.semibold)) }.accessibilityLabel("Open issue")
                            }
                        }.padding(.vertical, BoardStyle.Space.s)
                    }
                }
                if items.count > shown {
                    Button("Show more · \(items.count - shown) left") { shown += 100 }.buttonStyle(.plain).font(.subheadline.weight(.medium)).foregroundStyle(BoardStyle.accent)
                }
            }
        }
    }
    private var conversationsTitle: some View {
        HStack(spacing: BoardStyle.Space.s) {
            BentoLabel("Conversations", symbol: "bubble.left.and.bubble.right")
            if !items.isEmpty { CountPill(count: items.count) }
        }
    }
    private var statusPicker: some View {
        Picker("Status", selection: $status) { Text("Open").tag("open"); Text("Closed").tag("closed") }.pickerStyle(.segmented).labelsHidden().fixedSize()
    }
    private var searchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
            TextField("Search conversations", text: $search).textFieldStyle(.plain).font(.subheadline)
        }
        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 6)
        .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

}

/// The words and links of the support report.
enum SupportFormat {
    /// The published link of an issue. An older snapshot had only the number of a Gumroad issue.
    static func issueURL(_ item: BusinessValue) -> URL? {
        if let url = URL(string: item["url"].text), url.scheme == "https" { return url }
        return Int(item["slug"].text).flatMap { URL(string: "https://github.com/antiwork/gumroad-private/issues/\($0)") }
    }
    static func duration(_ minutes: Double?) -> String {
        minutes.map { $0 >= 60 ? ($0 / 60).formatted(.number.precision(.fractionLength(1))) + "h" : Int($0).formatted() + "m" } ?? "—"
    }
}

private struct RiskDashboard: View {
    let data: BusinessValue
    @State private var queue = "notReviewed"
    @State private var shown = 25
    private let queues = [("notReviewed", "Unreviewed"), ("flagged", "Flagged"), ("probated", "Probated"), ("softSuspended", "Suspended"), ("watching", "Watching")]
    private func dollars(_ cents: BusinessValue) -> String { reportNumber(cents.number.map { $0 / 100 }, money: true) }
    private func count(_ value: BusinessValue) -> String { value.number.map { Int($0).formatted() } ?? "—" }

    var body: some View {
        let disputes = data["chargebacks"]
        let last = disputes["last28d"]
        let outcomes = disputes["outcomes90d"]
        let size = data["stats"]["queueSize"].number
        BentoBoard {
            BentoStat(title: "Review queue", symbol: "shield.lefthalf.filled", tint: (size ?? 0) > 0 ? BoardStyle.attention : .secondary, value: reportNumber(size), wash: (size ?? 0) > 0 ? BoardStyle.attention : nil).bento(4, 1, compact: (2, 1))
            BentoStat(title: "Disputes · 28 days", symbol: "creditcard.trianglebadge.exclamationmark", value: reportNumber(last["count"].number),
                      note: disputes["rate"]["transactions"].number.map { "of " + Int($0).formatted() + " transactions" } ?? "All payment platforms",
                      trend: disputes["daily"].array.compactMap { $0["count"].number }).bento(4, 1, compact: (2, 1))
            BentoStat(title: "Disputed amount", symbol: "dollarsign.circle", value: dollars(last["amount_cents"]))
                .bento(4, 1, compact: (2, 1))
            ReportChart(title: "Chargebacks", symbol: "chart.bar",
                        points: disputes["daily"].array.compactMap { row in row["count"].number.map { ReportPoint(date: row["day"].text, value: $0, series: "Disputes") } }, bars: true)
                .bento(8, 2, compact: (2, 2))
            pair("Outcomes · 90 days", symbol: "scalemass",
                 first: ("Won", count(outcomes["won"]), dollars(outcomes["won_cents"]), BoardStyle.positive, outcomes["won"].number),
                 second: ("Lost", count(outcomes["lost"]), dollars(outcomes["lost_cents"]), BoardStyle.attention, outcomes["lost"].number))
                .bento(4, 1, compact: (2, 1))
            pair("By platform · 28 days", symbol: "building.columns",
                 first: ("Merchant of record", count(last["mor_count"]), dollars(last["mor_amount_cents"]), BentoColor.second, last["mor_count"].number),
                 second: ("Connect", count(last["connect_count"]), dollars(last["connect_amount_cents"]), BentoColor.third, last["connect_count"].number))
                .bento(4, 1, compact: (2, 1))
            BentoStat(title: "Suspended for fraud", symbol: "hand.raised", value: reportNumber(data["stats"]["suspendedFraud"].number), size: .medium)
                .bento(3, 1, compact: (1, 1))
            BentoStat(title: "Suspended for terms", symbol: "doc.text", value: reportNumber(data["stats"]["suspendedTos"].number), size: .medium)
                .bento(3, 1, compact: (1, 1))
            BentoStat(title: "Negative balances", symbol: "minus.circle", value: dollars(data["suspensionAllowance"]["total_negative_cents"]),
                      note: data["suspensionAllowance"]["users"].number.map { "\(Int($0)) suspended accounts" } ?? "Suspended accounts", size: .medium)
                .bento(3, 1, compact: (1, 1))
            BentoStat(title: "Daily negative", symbol: "calendar", value: dollars(data["suspensionAllowance"]["avg_daily_negative_cents"]), size: .medium)
                .bento(3, 1, compact: (1, 1))
            accounts.bento(12, nil, compact: (2, nil))
        }
        .onChange(of: queue) { _, _ in shown = 25 }
    }

    /// Two published counts of one whole, with their amounts. The bar shows the share; no rate is computed.
    private func pair(_ title: String, symbol: String, first: (String, String, String, Color, Double?), second: (String, String, String, Color, Double?)) -> some View {
        BentoTile {
            VStack(alignment: .leading, spacing: 0) {
                BentoLabel(title, symbol: symbol)
                Spacer(minLength: BoardStyle.Space.xs)
                HStack(alignment: .top, spacing: BoardStyle.Space.l) {
                    ForEach(Array([first, second].enumerated()), id: \.offset) { _, part in
                        VStack(alignment: .leading, spacing: 0) {
                            HStack(alignment: .firstTextBaseline, spacing: 5) {
                                BentoNumber(part.1, size: .medium)
                                Text(part.2).font(.caption).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
                            }
                            HStack(spacing: BoardStyle.Space.xs) {
                                Circle().fill(part.3).frame(width: 6, height: 6)
                                Text(part.0).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                BentoSplit(parts: [(first.4 ?? 0, first.3), (second.4 ?? 0, second.3)]).padding(.top, BoardStyle.Space.s)
            }
        }.accessibilityElement(children: .combine)
    }

    private var accounts: some View {
        let users = data[queue]["users"].array
        return BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: BoardStyle.Space.m) { accountsTitle; Spacer(minLength: 0); picker.pickerStyle(.segmented).fixedSize() }
                    HStack(spacing: BoardStyle.Space.m) { accountsTitle; Spacer(minLength: 0); picker.pickerStyle(.menu).fixedSize() }
                }
                Text("\(count(data[queue]["count"])) accounts · \(users.count) published records").font(.caption).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(users.prefix(shown).enumerated()), id: \.offset) { index, user in
                        if index > 0 { Divider() }
                        let account = RiskAccount(user)
                        HStack(spacing: BoardStyle.Space.m) {
                            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                                Text(account.title).font(.subheadline.weight(.medium)).lineLimit(1)
                                if let detail = account.detail { Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                            }
                            Spacer(minLength: BoardStyle.Space.s)
                            VStack(alignment: .trailing, spacing: BoardStyle.Space.line) {
                                Text(account.amount).font(.subheadline.weight(.medium)).monospacedDigit()
                                Text(account.amountLabel).font(.caption2).foregroundStyle(.tertiary)
                            }
                        }.padding(.vertical, BoardStyle.Space.s).textSelection(.enabled)
                    }
                }
                if users.count > shown {
                    Button("Show more · \(users.count - shown) left") { shown += 100 }.buttonStyle(.plain).font(.subheadline.weight(.medium)).foregroundStyle(BoardStyle.accent)
                }
            }
        }
    }
    private var accountsTitle: some View { BentoLabel("Risk queue", symbol: "person.crop.rectangle.stack") }
    private var picker: some View {
        Picker("Queue", selection: $queue) { ForEach(queues, id: \.0) { Text($0.1).tag($0.0) } }.labelsHidden()
    }
}

/// One account of a risk queue. A watched account has no name and no balance; it has an email, its revenue and a note.
struct RiskAccount {
    let title: String
    let detail: String?
    let amount: String
    let amountLabel: String

    init(_ user: BusinessValue) {
        let names = [user["name"].text, user["username"].text, user["email"].text].filter { !$0.isEmpty }
        let name = names.first ?? "Unknown account"
        title = name
        let rest = [names.count > 1 ? user["email"].text : "", user["notes"].text].filter { !$0.isEmpty && $0 != name }
        detail = rest.isEmpty ? nil : rest.joined(separator: " · ")
        if user["balance_cents"].number == nil, let revenue = user["revenue_cents"].number {
            amount = reportNumber(revenue / 100, money: true)
            amountLabel = user["unpaid_balance_cents"].number.map { reportNumber($0 / 100, money: true) + " unpaid" } ?? "Revenue"
        } else {
            amount = reportNumber(user["balance_cents"].number.map { $0 / 100 }, money: true)
            amountLabel = "Balance"
        }
    }
}

/// One pull request of the published snapshot, with the words of its state.
struct PullRequestRow {
    let title: String
    let detail: String
    let url: URL?
    let additions: String
    let deletions: String
    let state: String?
    let checks: String

    init(_ pull: BusinessValue) {
        title = pull["title"].text
        url = URL(string: pull["url"].text)
        detail = [pull["number"].number.map { "#\(Int($0))" }, pull["author"]["login"].text.isEmpty ? nil : pull["author"]["login"].text]
            .compactMap { $0 }.joined(separator: " · ")
        additions = "+" + reportNumber(pull["additions"].number)
        deletions = "−" + reportNumber(pull["deletions"].number)
        let review: String? = switch pull["reviewDecision"].text {
        case "APPROVED": "Approved"
        case "CHANGES_REQUESTED": "Changes requested"
        case "REVIEW_REQUIRED": "Needs review"
        default: nil
        }
        state = pull["isDraft"].flag ? "Draft" : review
        checks = pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"].text
    }
    var checkSymbol: String {
        switch checks {
        case "SUCCESS": "checkmark.circle.fill"
        case "FAILURE", "ERROR": "xmark.circle.fill"
        case "PENDING", "EXPECTED": "clock"
        default: "circle.dotted"
        }
    }
    var checkLabel: String {
        switch checks {
        case "SUCCESS": "Checks pass"
        case "FAILURE", "ERROR": "Checks fail"
        case "PENDING", "EXPECTED": "Checks run"
        default: "No checks"
        }
    }
}

private struct EngineeringDashboard: View {
    let data: BusinessValue
    private var repositories: [(key: String, value: BusinessValue)] {
        data["data"].object.sorted { a, b in
            let x = a.value["pullRequests"]["totalCount"].number ?? 0, y = b.value["pullRequests"]["totalCount"].number ?? 0
            return x == y ? a.key < b.key : x > y
        }
    }
    var body: some View {
        let counts = repositories.compactMap { $0.value["pullRequests"]["totalCount"].number }
        BentoBoard {
            BentoStat(title: "Open pull requests", symbol: "arrow.triangle.pull", value: counts.isEmpty ? "—" : reportNumber(counts.reduce(0, +)),
                      note: "\(repositories.count) repositories", wash: BentoColor.second).bento(4, 1, compact: (2, 1))
            ForEach(repositories, id: \.key) { repository in
                BentoStat(title: Self.name(repository.value, key: repository.key), symbol: "shippingbox", value: reportNumber(repository.value["pullRequests"]["totalCount"].number),
                          unit: "open", size: .medium).bento(2, 1, compact: (1, 1))
            }
            ForEach(repositories.filter { !$0.value["pullRequests"]["nodes"].array.isEmpty }, id: \.key) { repository in
                pulls(repository.value, key: repository.key).bento(12, nil, compact: (2, nil))
            }
        }
    }
    private static func name(_ repository: BusinessValue, key: String) -> String { repository["name"].text.isEmpty ? key : repository["name"].text }

    private func pulls(_ repository: BusinessValue, key: String) -> some View {
        let nodes = repository["pullRequests"]["nodes"].array
        let total = repository["pullRequests"]["totalCount"].number.map { Int($0) } ?? nodes.count
        return BentoTile {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack {
                    BentoLabel(repository["nameWithOwner"].text.isEmpty ? key : repository["nameWithOwner"].text, symbol: "arrow.triangle.pull")
                    Spacer(minLength: 0)
                    Text(nodes.count < total ? "\(nodes.count) of \(total)" : "\(total)").font(.caption).monospacedDigit().foregroundStyle(.tertiary)
                }
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(nodes.enumerated()), id: \.offset) { index, node in
                        if index > 0 { Divider() }
                        row(PullRequestRow(node))
                    }
                }
            }
        }
    }
    @ViewBuilder private func row(_ pull: PullRequestRow) -> some View {
        let content = HStack(spacing: BoardStyle.Space.m) {
            Image(systemName: pull.checkSymbol).font(.subheadline)
                .foregroundStyle(pull.checks == "SUCCESS" ? AnyShapeStyle(BoardStyle.positive) : pull.checks == "FAILURE" || pull.checks == "ERROR" ? AnyShapeStyle(BoardStyle.attention) : AnyShapeStyle(.secondary))
                .frame(width: 18).accessibilityLabel(pull.checkLabel)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(pull.title).font(.subheadline.weight(.medium)).foregroundStyle(.primary).lineLimit(2).multilineTextAlignment(.leading)
                Text([pull.detail, pull.state].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: BoardStyle.Space.s)
            HStack(spacing: 6) {
                Text(pull.additions).foregroundStyle(BoardStyle.positive)
                Text(pull.deletions).foregroundStyle(BoardStyle.attention)
            }.font(.caption.weight(.medium)).monospacedDigit().lineLimit(1)
            Image(systemName: "arrow.up.right").font(.caption2.weight(.bold)).foregroundStyle(.tertiary).accessibilityHidden(true)
        }.padding(.vertical, BoardStyle.Space.s).contentShape(Rectangle())
        if let url = pull.url { Link(destination: url) { content }.buttonStyle(.plain) } else { content }
    }
}

struct ReportPoint: Identifiable {
    let date: String; let value: Double; let series: String
    var id: String { date + series }
    var day: Date {
        let parts = date.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return .distantPast }
        return Calendar(identifier: .gregorian).date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12)) ?? .distantPast
    }
}

/// A chart tile: the label, an optional large number with its published change, and a line or bars that fill the
/// tile. The value under the pointer shows in the header.
private struct ReportChart: View {
    let title: String
    var symbol: String? = nil
    var subtitle: String = ""
    let points: [ReportPoint]
    var money = false
    var bars = false
    var headline: String? = nil
    var change: Double? = nil
    var changeLabel = ""
    var wash: Color? = nil
    @State private var selected: Date?
    private var series: [String] { points.reduce(into: [String]()) { if !$0.contains($1.series) { $0.append($1.series) } } }
    private var many: Bool { series.count > 1 }
    private var axisDates: [Date] {
        let days = Array(Set(points.map(\.day))).sorted()
        guard !days.isEmpty else { return [] }
        return Array(Set([0, days.count / 4, days.count / 2, days.count * 3 / 4])).sorted().map { days[$0] }
    }
    private var selectedPoints: [ReportPoint] {
        guard let selected else { return [] }
        guard let closest = points.min(by: { abs($0.day.timeIntervalSince(selected)) < abs($1.day.timeIntervalSince(selected)) }) else { return [] }
        return points.filter { $0.date == closest.date }
    }
    private func label(_ point: ReportPoint) -> String {
        many ? point.day.formatted(.dateTime.month(.abbreviated).year()) : point.day.formatted(.dateTime.month(.abbreviated).day())
    }

    var body: some View {
        BentoTile(wash: wash) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack(spacing: BoardStyle.Space.s) {
                    BentoLabel(title, symbol: symbol, tint: wash ?? .secondary)
                    Spacer(minLength: 0)
                    if !subtitle.isEmpty { Text(subtitle).font(.caption).foregroundStyle(.tertiary).lineLimit(1) }
                }
                if let headline {
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) { BentoNumber(headline, size: .hero); delta }
                        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) { BentoNumber(headline, size: .hero); delta }
                    }
                }
                if points.isEmpty {
                    Text("No published series").font(.subheadline).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    chart
                    reading
                }
            }
        }
    }
    @ViewBuilder private var delta: some View {
        if let change { BentoDelta(percent: change, label: changeLabel.isEmpty ? nil : changeLabel) }
    }
    /// The legend of the series, or the values of the day under the pointer. A chart of one series reads its last day.
    private var reading: some View {
        let shown = selectedPoints.isEmpty ? (many ? [] : points.suffix(1)) : selectedPoints
        return HStack(spacing: BoardStyle.Space.m) {
            if let first = shown.first { Text(label(first)).foregroundStyle(.secondary) }
            ForEach(Array(series.enumerated()), id: \.offset) { index, name in
                HStack(spacing: BoardStyle.Space.xs) {
                    if many { Circle().fill(BentoColor.series[index % BentoColor.series.count]).frame(width: 7, height: 7) }
                    if many { Text(name).foregroundStyle(.secondary) }
                    if let point = shown.first(where: { $0.series == name }) { Text(reportNumber(point.value, money: money)).fontWeight(.semibold).monospacedDigit() }
                }
            }
        }.font(.caption).lineLimit(1).frame(minHeight: 16)
    }
    private var chart: some View {
        Chart(points) { point in
            if bars {
                BarMark(x: .value("Date", point.day, unit: many ? .month : .day), y: .value("Value", point.value))
                    .foregroundStyle(by: .value("Series", point.series)).position(by: .value("Series", point.series)).cornerRadius(3)
            } else {
                AreaMark(x: .value("Date", point.day), yStart: .value("Base", base), yEnd: .value("Value", point.value))
                    .foregroundStyle(LinearGradient(colors: [BentoColor.money.opacity(0.26), BentoColor.money.opacity(0)], startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("Date", point.day), y: .value("Value", point.value)).foregroundStyle(BentoColor.money)
                    .lineStyle(StrokeStyle(lineWidth: 2.5, lineCap: .round, lineJoin: .round)).interpolationMethod(.monotone)
            }
            if let first = selectedPoints.first, first.date == point.date, point.series == first.series {
                RuleMark(x: .value("Selected", point.day)).foregroundStyle(BoardStyle.ink.opacity(0.2)).lineStyle(StrokeStyle(lineWidth: 1))
            }
        }
        .chartForegroundStyleScale(domain: series, range: Array(BentoColor.series.prefix(max(series.count, 1))))
        .chartXSelection(value: $selected)
        .chartYScale(domain: .automatic(includesZero: bars))
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(BoardStyle.line)
                AxisValueLabel { if let n = value.as(Double.self) { Text(reportNumber(n, money: money)) } }
            }
        }
        .chartXAxis { AxisMarks(values: axisDates) { _ in AxisValueLabel(format: many ? .dateTime.month(.abbreviated) : .dateTime.month(.abbreviated).day()) } }
        .chartLegend(.hidden)
        .padding(.top, BoardStyle.Space.s)
        .frame(minHeight: 72, maxHeight: .infinity)
    }
    /// The fill of a line starts under its lowest day, so a small change of a large volume still shows as a shape.
    private var base: Double {
        let low = points.map(\.value).min() ?? 0, high = points.map(\.value).max() ?? 1
        return max(0, low - (high - low) * 0.35)
    }
}

func reportNumber(_ number: Double?, money: Bool = false) -> String {
    guard let number else { return "—" }
    let magnitude = abs(number)
    let divisor: Double = magnitude >= 1_000_000_000 ? 1_000_000_000 : magnitude >= 1_000_000 ? 1_000_000 : magnitude >= 10_000 ? 1_000 : 1
    let suffix = divisor == 1_000_000_000 ? "B" : divisor == 1_000_000 ? "M" : divisor == 1_000 ? "K" : ""
    return (number < 0 ? "−" : "") + (money ? "$" : "") + (magnitude / divisor).formatted(.number.precision(.fractionLength(divisor == 1 ? 0 : 1))) + suffix
}

struct BusinessDataExplorer: View {
    let value: BusinessValue
    var body: some View { List { BusinessDataRows(value: value) }.textSelection(.enabled) }
}

private struct BusinessDataRows: View {
    let value: BusinessValue
    var body: some View {
        ForEach(value.object.keys.sorted(), id: \.self) { key in row(key, value[key]) }
        ForEach(Array(value.array.enumerated()), id: \.offset) { index, child in row(String(index + 1), child) }
    }
    @ViewBuilder private func row(_ key: String, _ child: BusinessValue) -> some View {
        switch child {
        case .object, .array: DisclosureGroup(key) { BusinessDataRows(value: child) }
        default: LabeledContent(key, value: child.number.map { $0.formatted() } ?? (child.isNull ? "Unavailable" : child.text.isEmpty ? String(describing: child) : child.text))
        }
    }
}

struct BusinessLedgerView: View {
    @Environment(AppStore.self) private var store
    @State private var files: [LedgerIndex.File] = []
    @State private var error: String?
    @State private var search = ""
    var body: some View {
        List {
            Section { Label("Beancount · Accounting is the system of record", systemImage: "books.vertical").font(.subheadline) }
            ForEach(files.filter { search.isEmpty || $0.path.localizedCaseInsensitiveContains(search) }) { file in
                NavigationLink { BusinessLedgerFileView(path: file.path) } label: { Label(file.path, systemImage: "doc.text").font(.subheadline.monospaced()) }
            }
            if let error { Text(error).foregroundStyle(BoardStyle.attention) }
        }.navigationTitle("Beancount").searchable(text: $search)
            .task { do { files = try await store.businessLedger().files } catch { self.error = error.localizedDescription } }
    }
}

private struct BusinessLedgerFileView: View {
    @Environment(AppStore.self) private var store
    let path: String
    @State private var file: LedgerFile?
    @State private var error: String?
    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            if let file { Text(file.content).font(.system(.caption, design: .monospaced)).textSelection(.enabled).padding(24) }
            else if let error { Text(error).padding(24) }
            else { ProgressView().padding(24) }
        }.navigationTitle(path.components(separatedBy: "/").last ?? path)
            .toolbar { if let file, let url = URL(string: file.url) { Link("GitHub", destination: url) } }
            .task { do { file = try await store.businessLedgerFile(path) } catch { self.error = error.localizedDescription } }
    }
}
