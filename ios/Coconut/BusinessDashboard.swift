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
            }.pickerStyle(.segmented).labelsHidden().padding(.horizontal, 24).padding(.vertical, 12)
            if tab == "library" { BusinessReportLibrary(function: function) }
            else if tab == "work" || !hasReport { BusinessWorkListView(function: function) }
            else { BusinessReportView(key: function) }
        }
        .background(BoardStyle.paper)
        .navigationTitle(BusinessFunction.name(function))
        .onAppear { if !hasReport && tab == "overview" { tab = "work" } }
    }
}

struct BusinessReportLibrary: View {
    var function: String? = nil
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Reports").font(.title.bold())
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 16)], spacing: 16) {
                    ForEach(BusinessReport.keys.filter { function == nil || $0 == function || $0 == "overview" || (function == "support" && $0 == "risk") }, id: \.self) { key in
                        NavigationLink(value: BusinessRoute.report(key)) {
                            VStack(alignment: .leading, spacing: 20) {
                                Image(systemName: BusinessReport.symbols[key] ?? "chart.bar").font(.title2).foregroundStyle(BoardStyle.accent)
                                HStack { Text(BusinessReport.titles[key] ?? key).font(.headline); Spacer(); Image(systemName: "arrow.up.right").foregroundStyle(.secondary) }
                            }.dashboardCard()
                        }.buttonStyle(.plain)
                    }
                    if function == nil || function == "accounting" {
                        NavigationLink(value: BusinessRoute.ledger) {
                            VStack(alignment: .leading, spacing: 20) {
                                Image(systemName: "text.document").font(.title2).foregroundStyle(BoardStyle.green)
                                HStack { Text("Beancount ledger").font(.headline); Spacer(); Image(systemName: "arrow.up.right").foregroundStyle(.secondary) }
                            }.dashboardCard()
                        }.buttonStyle(.plain)
                    }
                }
                Text("Published data from GitHub. Agent pages and notes are in the Library tab.").font(.caption).foregroundStyle(.secondary)
            }.padding(24).frame(maxWidth: 1160, alignment: .leading).frame(maxWidth: .infinity)
        }.background(BoardStyle.paper)
    }
}

struct BusinessReportView: View {
    @Environment(AppStore.self) private var store
    let key: String
    @State private var report: BusinessReport?
    @State private var error: String?
    @State private var details = false

    init(key: String, initial: BusinessReport? = nil) { self.key = key; _report = State(initialValue: initial) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if let report {
                    header(report)
                    if report.stale {
                        Label(report.generatedAt == nil ? "This snapshot has no timestamp. Its freshness is unknown." : "This snapshot is over an hour old. Check the publisher before acting.", systemImage: "clock.badge.exclamationmark")
                            .font(.callout).foregroundStyle(BoardStyle.attention).padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(BoardStyle.attention.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                    }
                    switch key {
                    case "support": SupportDashboard(data: report.data)
                    case "risk": RiskDashboard(data: report.data)
                    case "engineering": EngineeringDashboard(data: report.data)
                    default: MoneyDashboard(data: report.data, accounting: key == "accounting")
                    }
                    HStack {
                        if let url = URL(string: report.source.url) { Link("Source on GitHub", destination: url) }
                        Spacer()
                        Button("Explore all data", systemImage: "tablecells") { details = true }
                    }.font(.caption).foregroundStyle(.secondary)
                } else if error == nil { ProgressView("Loading report").frame(maxWidth: .infinity, minHeight: 360) }
                if let error {
                    ContentUnavailableView {
                        Label("Report unavailable", systemImage: "chart.bar.xaxis")
                    } description: { Text(error) } actions: {
                        Button("Try again") { Task { await load() } }
                        NavigationLink("GitHub connection") { GitHubSettingsView() }
                    }
                }
            }.padding(24).frame(maxWidth: 1160, alignment: .leading).frame(maxWidth: .infinity)
        }
        .background(BoardStyle.paper).navigationTitle(BusinessReport.titles[key] ?? key).inlineNavigationTitle()
        .task(id: "\(key)/\(store.connection)/\(store.businessChanges)") { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $details) {
            NavigationStack {
                if let report { BusinessDataExplorer(value: report.data).navigationTitle("Report data") }
            }.frame(minWidth: 380, minHeight: 420)
        }
    }

    private func header(_ report: BusinessReport) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(key == "accounting" ? "BEANCOUNT · COMPANY LEDGER" : "GUMROAD · OPERATIONS")
                    .font(.caption.weight(.semibold)).tracking(1.5).foregroundStyle(BoardStyle.accent)
                Spacer()
                if store.isDemo { Text("SAMPLE DATA").font(.caption2.bold()).foregroundStyle(.secondary) }
            }
            Text(key == "overview" ? "The business, at a glance." : key == "accounting" ? "Every number has a ledger." : key == "support" ? "Keep the queue moving." : BusinessReport.titles[key] ?? key)
                .font(.system(.largeTitle, design: .rounded, weight: .bold)).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Circle().fill(report.stale ? BoardStyle.attention : BoardStyle.green).frame(width: 6, height: 6)
                Text(report.generatedAt?.apiDate.map { "Snapshot " + $0.formatted(date: .abbreviated, time: .shortened) } ?? "Snapshot date unavailable")
                if key == "accounting" { Text("· USD") }
            }.font(.caption).foregroundStyle(.secondary)
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

private struct MoneyDashboard: View {
    let data: BusinessValue
    let accounting: Bool
    private var ledger: BusinessValue { data["ledger"] }
    private var gmv: BusinessValue { data["gmv"] }
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
    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            if accounting {
                LazyVGrid(columns: metricColumns, spacing: 14) {
                    MetricCard("Revenue", value: ledger["ltm_revenue"].number, money: true, note: "Last 12 closed months", tint: .blue)
                    MetricCard("EBITDA", value: ledger["ltm_ebitda"].number, money: true, note: "Last 12 closed months", tint: BoardStyle.green)
                    MetricCard("Net income", value: ledger["ltm_net_income"].number, money: true, note: "Last 12 closed months", tint: .purple)
                }
            } else {
                LazyVGrid(columns: metricColumns, spacing: 14) {
                    MetricCard("GMV · 30 days", value: gmv["gmv_30d"].number, money: true, note: growth, tint: BoardStyle.green)
                    MetricCard("Revenue · LTM", value: ledger["ltm_revenue"].number, money: true, note: "Beancount · closed months", tint: .blue)
                    MetricCard("EBITDA · LTM", value: ledger["ltm_ebitda"].number, money: true, note: "Beancount · closed months", tint: .purple)
                }
            }
            if !accounting {
                ReportChart(title: "Creator sales", subtitle: "Daily GMV · last 30 settled days · USD", points: volume, money: true)
                LazyVGrid(columns: metricColumns, spacing: 14) {
                    QueueCard("Support", value: sum(data["support"]["gumroad"], data["support"]["flexile"]), overdue: sum(data["support"]["gumroad_past_sla"], data["support"]["flexile_past_sla"]), route: .function("support"), symbol: "lifepreserver")
                    QueueCard("Product issues", value: data["issues"]["total"].number, overdue: data["issues"]["past_sla"].number, route: .report("support"), symbol: "wrench.and.screwdriver")
                    QueueCard("Risk review", value: data["risk"]["total"].number, overdue: data["risk"]["past_sla"].number, route: .report("risk"), symbol: "shield.lefthalf.filled")
                }
            }
            ReportChart(title: "Revenue & profit", subtitle: "Monthly · Beancount · USD", points: financials, money: true, bars: true)
            if !ledger.isNull {
                HStack(spacing: 12) {
                    Image(systemName: "checkmark.seal").font(.title2).foregroundStyle(BoardStyle.green)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Books closed through \(ledger["closed_through"].text)").font(.subheadline.weight(.semibold))
                        Text("Company totals from Accounting. GMV measures creator sales separately.").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    NavigationLink(value: BusinessRoute.ledger) { Image(systemName: "arrow.up.right").font(.headline) }.accessibilityLabel("Open Beancount ledger")
                }.dashboardCard()
            }
        }
    }
    private var growth: String {
        guard let current = gmv["gmv_30d"].number, let previous = gmv["gmv_prev_30d"].number, previous > 0 else { return "Previous period unavailable" }
        return ((current / previous - 1) * 100).formatted(.number.precision(.fractionLength(1)).sign(strategy: .always())) + "% vs previous 30 days"
    }
    private func sum(_ a: BusinessValue, _ b: BusinessValue) -> Double? { guard let a = a.number, let b = b.number else { return nil }; return a + b }
}

private struct SupportDashboard: View {
    let data: BusinessValue
    @State private var queue = "support"
    @State private var status = "open"
    @State private var search = ""
    private var selected: BusinessValue { data[queue] }
    private var agents: [BusinessValue] {
        let value = selected["closedCount"]["byAgent"]
        return value.array.isEmpty ? value.object.sorted(by: { $0.key < $1.key }).map(\.value) : value.array
    }
    private var items: [BusinessValue] {
        selected[status].array.filter { search.isEmpty || $0["subject"].text.localizedCaseInsensitiveContains(search) }
    }
    private var resolution: SupportResolution {
        SupportResolution(closed: selected["closed"].array, reference: data["generatedAt"].text.apiDate)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Picker("Queue", selection: $queue) { Text("Customer support").tag("support"); Text("Product issues").tag("issues") }.pickerStyle(.segmented).labelsHidden()
            LazyVGrid(columns: metricColumns, spacing: 14) {
                MetricCard("Open", value: selected["counts"]["all"].number, note: "In this queue", tint: .blue)
                MetricCard("Unassigned", value: selected["counts"]["unassigned"].number, note: "Waiting for an owner", tint: BoardStyle.accent)
                MetricCard("Closed · 24 hours", value: selected["closedCount"]["total"].number, note: "Closure events in this queue", tint: BoardStyle.green)
            }
            ReportChart(title: "Incoming volume", subtitle: "New conversations per day", points: selected["dailyVolume"].array.compactMap { row in row["count"].number.map { ReportPoint(date: row["date"].text, value: $0, series: "Conversations") } }, bars: true)
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Time to resolution").font(.title3.bold()); Spacer()
                    Text("\(resolution.minutes.count) resolved").font(.caption).foregroundStyle(.secondary)
                }
                Text("Customer's last message to close · all assignees · 24 hours before snapshot").font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 32) {
                    resolutionStat("Median", minutes: resolution.median)
                    resolutionStat("90th percentile", minutes: resolution.p90)
                }
                Chart(Array(resolution.buckets.enumerated()), id: \.offset) { index, count in
                    BarMark(x: .value("Time", SupportResolution.labels[index]), y: .value("Resolved", count))
                        .foregroundStyle(BoardStyle.accent.gradient).cornerRadius(4)
                }.frame(height: 160)
            }.dashboardCard()
            if !agents.isEmpty {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Team activity").font(.title3.bold())
                    Chart(Array(agents.enumerated()), id: \.offset) { _, agent in
                        if let count = agent["count"].number {
                            BarMark(x: .value("Closed", count), y: .value("Agent", agent["displayName"].text.isEmpty ? "Unknown closer" : agent["displayName"].text))
                                .foregroundStyle(BoardStyle.green.gradient).cornerRadius(4)
                        }
                    }.frame(height: max(100, CGFloat(agents.count) * 36)).chartXAxis(.hidden)
                }.dashboardCard()
            }
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Conversations").font(.title3.bold())
                    Spacer()
                    Picker("Status", selection: $status) { Text("Open").tag("open"); Text("Closed").tag("closed") }.fixedSize()
                }
                TextField("Search conversations", text: $search).textFieldStyle(.roundedBorder)
                if items.isEmpty { Text("No conversations in this snapshot.").foregroundStyle(.secondary) }
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .top, spacing: 12) {
                        Circle().fill(status == "open" ? BoardStyle.attention : BoardStyle.green).frame(width: 7, height: 7).padding(.top, 6)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item["subject"].text).font(.subheadline.weight(.medium)).textSelection(.enabled)
                            Text([item["queue"].text, item["createdAt"].text.apiDate?.formatted(date: .abbreviated, time: .omitted) ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if queue == "issues", let number = Int(item["slug"].text), let url = URL(string: "https://github.com/antiwork/gumroad-private/issues/\(number)") {
                            Link(destination: url) { Image(systemName: "arrow.up.right") }.accessibilityLabel("Open issue")
                        }
                    }
                    Divider().overlay(BoardStyle.line)
                }
            }.dashboardCard()
        }
    }
    private func resolutionStat(_ title: String, minutes: Double?) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(minutes.map { $0 >= 60 ? ($0 / 60).formatted(.number.precision(.fractionLength(1))) + "h" : Int($0).formatted() + "m" } ?? "—")
                .font(.system(size: 28, weight: .semibold, design: .rounded)).monospacedDigit()
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct RiskDashboard: View {
    let data: BusinessValue
    @State private var queue = "notReviewed"
    private let queues = [("notReviewed", "Unreviewed"), ("flagged", "Flagged"), ("probated", "Probated"), ("softSuspended", "Suspended"), ("watching", "Watching")]
    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            LazyVGrid(columns: metricColumns, spacing: 14) {
                MetricCard("Review queue", value: data["stats"]["queueSize"].number, note: "Accounts requiring review", tint: BoardStyle.accent)
                MetricCard("Disputes · 28 days", value: data["chargebacks"]["last28d"]["count"].number, note: "All payment platforms", tint: .purple)
                MetricCard("Disputed amount", value: data["chargebacks"]["last28d"]["amount_cents"].number.map { $0 / 100 }, money: true, note: "Last 28 days · USD", tint: .blue)
            }
            ReportChart(title: "Chargebacks", subtitle: "Daily disputes · last 28 days", points: data["chargebacks"]["daily"].array.compactMap { row in row["count"].number.map { ReportPoint(date: row["day"].text, value: $0, series: "Disputes") } }, bars: true)
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Risk queue").font(.title3.bold()); Spacer()
                    Picker("Queue", selection: $queue) { ForEach(queues, id: \.0) { Text($0.1).tag($0.0) } }
                }
                Text("\(data[queue]["count"].number.map { Int($0).formatted() } ?? "—") accounts · showing \(data[queue]["users"].array.count) published records").font(.caption).foregroundStyle(.secondary)
                ForEach(Array(data[queue]["users"].array.enumerated()), id: \.offset) { _, user in
                    HStack {
                        VStack(alignment: .leading, spacing: 4) { Text(user["name"].text.isEmpty ? user["username"].text : user["name"].text).font(.subheadline.weight(.medium)); Text(user["email"].text).font(.caption).foregroundStyle(.secondary) }
                        Spacer()
                        Text(reportNumber(user["balance_cents"].number.map { $0 / 100 }, money: true)).monospacedDigit()
                    }.textSelection(.enabled)
                    Divider()
                }
            }.dashboardCard()
        }
    }
}

private struct EngineeringDashboard: View {
    let data: BusinessValue
    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            ForEach(data["data"].object.keys.sorted(), id: \.self) { key in
                let repo = data["data"][key]
                VStack(alignment: .leading, spacing: 16) {
                    HStack { Text(repo["nameWithOwner"].text).font(.title3.bold()); Spacer(); Text(reportNumber(repo["pullRequests"]["totalCount"].number)).font(.title.bold()).foregroundStyle(BoardStyle.accent) }
                    ForEach(Array(repo["pullRequests"]["nodes"].array.enumerated()), id: \.offset) { _, pr in
                        if let url = URL(string: pr["url"].text) {
                            Link(destination: url) {
                                HStack { Text(pr["title"].text).multilineTextAlignment(.leading); Spacer(); Text("+" + reportNumber(pr["additions"].number)).foregroundStyle(BoardStyle.green); Image(systemName: "arrow.up.right") }
                            }.font(.subheadline)
                            Divider()
                        }
                    }
                }.dashboardCard()
            }
        }
    }
}

private let metricColumns = Array(repeating: GridItem(.flexible(minimum: 0), spacing: 14), count: 3)

private struct MetricCard: View {
    let title: String; let value: Double?; var money = false; let note: String; let tint: Color
    init(_ title: String, value: Double?, money: Bool = false, note: String, tint: Color) { self.title = title; self.value = value; self.money = money; self.note = note; self.tint = tint }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text(title).font(.caption.weight(.medium)).lineLimit(2).frame(minHeight: 28, alignment: .topLeading); Spacer(minLength: 0); Circle().fill(tint).frame(width: 6, height: 6) }
            Text(reportNumber(value, money: money)).font(.system(size: 34, weight: .semibold, design: .rounded)).monospacedDigit().minimumScaleFactor(0.6).lineLimit(1)
            Text(value == nil ? "Data unavailable" : note).font(.caption2).foregroundStyle(.secondary).lineLimit(2).frame(minHeight: 28, alignment: .topLeading)
        }.dashboardCard(tint: tint, padding: 14)
        .accessibilityElement(children: .combine)
    }
}

private struct QueueCard: View {
    let title: String; let value: Double?; let overdue: Double?; let route: BusinessRoute; let symbol: String
    init(_ title: String, value: Double?, overdue: Double?, route: BusinessRoute, symbol: String) { self.title = title; self.value = value; self.overdue = overdue; self.route = route; self.symbol = symbol }
    var body: some View {
        NavigationLink(value: route) {
            VStack(alignment: .leading, spacing: 14) {
                HStack { Image(systemName: symbol).foregroundStyle(BoardStyle.accent); Text(title).font(.subheadline.weight(.medium)); Spacer(); Image(systemName: "arrow.up.right").font(.caption).foregroundStyle(.secondary) }
                HStack(alignment: .firstTextBaseline) {
                    Text(reportNumber(value)).font(.system(size: 32, weight: .semibold, design: .rounded)).monospacedDigit()
                    Text("open").font(.caption).foregroundStyle(.secondary)
                }
                Text(overdue.map { $0 > 0 ? "\(Int($0)) past SLA" : "Within SLA" } ?? "SLA unavailable").font(.caption.weight(.medium)).foregroundStyle((overdue ?? 0) > 0 ? BoardStyle.attention : BoardStyle.green)
            }.dashboardCard()
        }.buttonStyle(.plain)
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

private struct ReportChart: View {
    let title: String; let subtitle: String; let points: [ReportPoint]; var money = false; var bars = false
    @State private var selected: Date?
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
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.title3.bold()); Spacer()
                if let last = points.last, Set(points.map(\.series)).count == 1 { Text(reportNumber(last.value, money: money)).font(.title3.weight(.semibold)).monospacedDigit() }
            }
            Text(subtitle).font(.caption).foregroundStyle(.secondary)
            if points.isEmpty { ContentUnavailableView("No published series", systemImage: "chart.xyaxis.line").frame(height: 180) }
            else {
                Chart(points) { point in
                    if bars {
                        BarMark(x: .value("Date", point.day, unit: Set(points.map(\.series)).count > 1 ? .month : .day), y: .value("Value", point.value))
                            .foregroundStyle(by: .value("Series", point.series)).position(by: .value("Series", point.series)).cornerRadius(3)
                    } else {
                        AreaMark(x: .value("Date", point.day), yStart: .value("Zero", 0), yEnd: .value("Value", point.value))
                            .foregroundStyle(BoardStyle.green.opacity(0.10).gradient)
                        LineMark(x: .value("Date", point.day), y: .value("Value", point.value)).foregroundStyle(BoardStyle.green).lineStyle(StrokeStyle(lineWidth: 2.5)).interpolationMethod(.monotone)
                    }
                    if let selected, Calendar.current.isDate(point.day, inSameDayAs: selected) { RuleMark(x: .value("Selected", point.day)).foregroundStyle(.secondary.opacity(0.3)) }
                }
                .chartForegroundStyleScale(range: [BoardStyle.green, Color.blue, Color.purple])
                .chartXSelection(value: $selected)
                .chartXScale(range: .plotDimension(startPadding: 8, endPadding: 24))
                .chartYAxis { AxisMarks(position: .leading, values: .automatic(desiredCount: 4)) { value in AxisGridLine(); AxisValueLabel { if let n = value.as(Double.self) { Text(reportNumber(n, money: money)) } } } }
                .chartXAxis { AxisMarks(values: axisDates) { _ in AxisValueLabel(format: Set(points.map(\.series)).count > 1 ? .dateTime.month(.abbreviated) : .dateTime.month(.abbreviated).day()) } }
                .chartLegend(position: .bottom, alignment: .leading, spacing: 16)
                .frame(height: 230)
                if let first = selectedPoints.first {
                    HStack {
                        Text(first.day.formatted(date: .abbreviated, time: .omitted)).foregroundStyle(.secondary)
                        ForEach(selectedPoints) { point in Text("\(point.series) \(reportNumber(point.value, money: money))") }
                    }.font(.caption).monospacedDigit().frame(minHeight: 16)
                }
            }
        }.dashboardCard()
    }
}

func reportNumber(_ number: Double?, money: Bool = false) -> String {
    guard let number else { return "—" }
    let magnitude = abs(number)
    let divisor: Double = magnitude >= 1_000_000_000 ? 1_000_000_000 : magnitude >= 1_000_000 ? 1_000_000 : magnitude >= 10_000 ? 1_000 : 1
    let suffix = divisor == 1_000_000_000 ? "B" : divisor == 1_000_000 ? "M" : divisor == 1_000 ? "K" : ""
    return (number < 0 ? "−" : "") + (money ? "$" : "") + (magnitude / divisor).formatted(.number.precision(.fractionLength(divisor == 1 ? 0 : 1))) + suffix
}

extension View {
    fileprivate func dashboardCard(tint: Color? = nil, padding: CGFloat = 20) -> some View {
        self.padding(padding).frame(maxWidth: .infinity, alignment: .leading)
            .background((tint ?? BoardStyle.ink).opacity(tint == nil ? 0.025 : 0.065), in: RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(BoardStyle.line.opacity(0.7), lineWidth: 0.5))
    }
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
