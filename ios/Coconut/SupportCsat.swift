import SwiftUI
import Charts

/// How Support's customers felt, read from the Mac (backend/coconut/support_csat.py): the score of the newest measured
/// day, its move against yesterday, the week and the month, the fourteen days behind it, and the read that Gumclaw's
/// CSAT lane wrote beside the numbers. The Support dashboard shows the score as a tile, and a tap opens this page: the
/// numbers, the reasons, and the one button that asks Gumclaw to raise it.
struct SupportCsat: Codable, Equatable, Sendable {
    struct Today: Codable, Equatable, Sendable {
        var date: String
        var score: Double
        var happyPct: Double?
        var classified: Int
        /// The conversations of each grade, and the share of each: amazing, great, good, bad.
        var counts: [String: Int]
        var pcts: [String: Double]
        var generatedAt: String?
    }

    /// The move of the score against the day before, the seven days before it, and the thirty.
    struct Moves: Codable, Equatable, Sendable {
        var yesterday: Double?
        var week: Double?
        var month: Double?
    }

    struct Day: Codable, Equatable, Sendable, Identifiable {
        var date: String
        var score: Double
        var id: String { date }
    }

    /// The low and the high of the series, with their days, so a person reads the range instead of trusting a chart.
    struct Range: Codable, Equatable, Sendable {
        var days: Int
        var low: Double?
        var lowDate: String?
        var high: Double?
        var highDate: String?
    }

    /// What the CSAT lane wrote in words (csat_digest_write.py): the daily read of the numbers on this page.
    struct Digest: Codable, Equatable, Sendable {
        struct Driver: Codable, Equatable, Sendable, Identifiable {
            var name: String
            var count: Int
            var id: String { name }
        }

        var date: String?
        var headline: String
        var body: String
        var drivers: [Driver]
        var weekly: String?
        var monthly: String?
        var writtenAt: String?
    }

    /// The newest measured day, nil when the lane has measured none.
    var asOf: String?
    /// The newest day is today or yesterday: a dashboard says so instead of passing an old number as today's.
    var fresh: Bool
    var ageDays: Int?
    var today: Today?
    var deltas: Moves
    var series: [Day]
    var range: Range
    var digest: Digest?
    var readAt: String?
}

/// The numbers of the CSAT page as a person reads them. A score is a 0-100 index, not a share, so it never wears a
/// percent sign; a move is in points of that index.
enum SupportCsatFormat {
    /// `60.5`.
    static func score(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        return value.formatted(.number.precision(.fractionLength(1)))
    }

    /// `2.5 points down`, `1.4 points up`, `no change`, for the move of the score.
    static func move(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "no earlier day" }
        if abs(value) < 0.05 { return "no change" }
        let points = abs(value).formatted(.number.precision(.fractionLength(1)))
        return points + (value < 0 ? " points down" : " points up")
    }

    /// The day of the newest measurement: `Today`, `Yesterday`, `Nov 16`.
    static func day(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "—" }
        return BusinessFormat.day(raw)
    }
}

extension AppStore {
    /// Reads the mood of Support's customers. A failure keeps the block that shows.
    func loadSupportCsat() async {
        if isDemo {
            if supportCsat == nil { supportCsat = .sample }
            return
        }
        guard connection == .connected else { return }
        do {
            let read: SupportCsat = try await businessRequest("support/csat")
            if read != supportCsat { supportCsat = read }
        } catch is CancellationError {
        } catch {}
    }
}

extension SupportCsat {
    /// The page of the demo, so a screenshot of the dashboard shows a score instead of a dash.
    static let sample = SupportCsat(
        asOf: "2026-11-26", fresh: true, ageDays: 0,
        today: Today(date: "2026-11-26", score: 60.5, happyPct: 90.3, classified: 525,
                     counts: ["amazing": 32, "great": 103, "good": 339, "bad": 51],
                     pcts: ["amazing": 6.1, "great": 19.6, "good": 64.6, "bad": 9.7],
                     generatedAt: nil),
        deltas: Moves(yesterday: -2.5, week: -1.9, month: -1.4),
        series: [62.0, 61.1, 62.8, 63.4, 61.9, 62.2, 60.9, 61.7, 62.6, 63.1, 62.4, 60.2, 63.0, 60.5]
            .enumerated().map { index, score in
                Day(date: "2026-11-\(String(format: "%02d", 13 + index))", score: score)
            },
        range: Range(days: 14, low: 60.2, lowDate: "2026-11-13", high: 63.7, highDate: "2026-11-23"),
        digest: Digest(date: "2026-11-26", headline: "Payout holds are still the main gripe.",
                       body: "Payout holds are 77 of this week's 137 genuinely unhappy conversations.",
                       drivers: [Digest.Driver(name: "Payout holds", count: 77),
                                 Digest.Driver(name: "Refunds", count: 14)],
                       weekly: nil, monthly: nil, writtenAt: nil),
        readAt: nil)
}

/// The score of the newest day as a tile of the Support dashboard: the number, its move from yesterday and the
/// fourteen days that led to it. A tap opens the page of the score (SupportCsatView).
struct SupportCsatTile: View {
    let csat: SupportCsat?
    @Environment(\.bentoCompact) private var compact

    private var today: SupportCsat.Today? { csat?.today }

    /// `down from yesterday`, `up from yesterday`, in the words a person uses for a number that moved.
    private var note: String? {
        guard let move = csat?.deltas.yesterday else { return nil }
        return SupportCsatFormat.move(move) + " from yesterday"
    }

    var body: some View {
        BentoTile {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: BoardStyle.Space.xs) {
                    BentoLabel("Customer satisfaction")
                    Spacer(minLength: 0)
                    if let csat, !csat.fresh, let age = csat.ageDays {
                        Text(age == 1 ? "Yesterday" : "\(age) days old").font(.caption).foregroundStyle(BoardStyle.attention)
                    } else if today != nil {
                        BentoChevron()
                    }
                }
                Spacer(minLength: BoardStyle.Space.xs)
                HStack(alignment: .bottom, spacing: BoardStyle.Space.m) {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        BentoNumber(SupportCsatFormat.score(today?.score), size: compact ? .medium : .large)
                        if let note {
                            Text(note).font(.caption).foregroundStyle(noteTint).lineLimit(1)
                        } else if today == nil {
                            Text("Not measured yet").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer(minLength: 0)
                    if let series = csat?.series, series.count > 1 {
                        BentoSparkline(values: series.map(\.score), tint: BoardStyle.accent).frame(maxWidth: 140, maxHeight: 40)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("support-csat")
    }

    private var noteTint: Color {
        guard let move = csat?.deltas.yesterday else { return .secondary }
        return move < -0.05 ? BoardStyle.attention : .secondary
    }
}

/// The page that the score tile opens: today's number and its moves, the split of the conversations, the fourteen
/// days, and the read that Gumclaw wrote, with the button that asks Gumclaw to raise it.
struct SupportCsatView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var shown: SupportCsat?
    @State private var error: String?

    private var csat: SupportCsat? { store.supportCsat ?? shown }

    var body: some View {
        BoardPage(width: .wide, spacing: BoardStyle.Space.l) {
            if let csat {
                head(csat)
                split(csat)
                trend(csat)
                read(csat)
                ask(csat)
            } else if error == nil {
                BoardLoading(title: "Loading customer satisfaction", height: 320)
            }
            if let error { BoardLoadNotice(title: "Customer satisfaction unavailable", message: error) { Task { await load() } } }
        }
        .navigationTitle("Customer satisfaction")
        .task { await load() }
        .refreshable { await load() }
    }

    /// The score of the newest day, the day it is, and the three moves.
    private func head(_ csat: SupportCsat) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) {
                BentoNumber(SupportCsatFormat.score(csat.today?.score), size: .hero)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(csat.today.map { SupportCsatFormat.day($0.date) } ?? "Not measured yet")
                        .font(.subheadline.weight(.medium))
                    if let today = csat.today {
                        Text("\(today.classified.formatted()) closed conversations scored")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 0)
            }
            if !csat.fresh, let age = csat.ageDays {
                Label(age == 1 ? "This is yesterday's measurement." : "This measurement is \(age) days old.",
                      systemImage: "clock.badge.exclamationmark")
                    .font(.footnote).foregroundStyle(BoardStyle.attention)
            }
            HStack(spacing: BoardStyle.Space.l) {
                move("Yesterday", csat.deltas.yesterday)
                move("7-day average", csat.deltas.week)
                move("30-day average", csat.deltas.month)
            }
            if let today = csat.today, let happy = today.happyPct {
                Text("Happy share " + BusinessFormat.percent(happy))
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func move(_ label: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(SupportCsatFormat.move(value))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle((value ?? 0) < -0.05 ? BoardStyle.attention : .primary)
        }
    }

    /// The four grades of the day, in the language of the score.
    private func split(_ csat: SupportCsat) -> some View {
        BoardSection("The conversations") {
            ForEach(["amazing", "great", "good", "bad"], id: \.self) { grade in
                BoardRow(title: grade.capitalized,
                         detail: csat.today.map { "\($0.counts[grade] ?? 0) conversations" },
                         value: csat.today.flatMap { $0.pcts[grade] }.map { BusinessFormat.percent($0) } ?? "—",
                         chevron: false)
            }
        }
    }

    /// The fourteen days, with the range a person quotes.
    @ViewBuilder private func trend(_ csat: SupportCsat) -> some View {
        if csat.series.count > 1 {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                let low = (csat.range.low ?? 0) - 1
                let high = (csat.range.high ?? 1) + 1
                Chart(csat.series) { day in
                    LineMark(x: .value("Day", day.id), y: .value("Score", day.score))
                        .foregroundStyle(BoardStyle.accent)
                    PointMark(x: .value("Day", day.id), y: .value("Score", day.score))
                        .foregroundStyle(BoardStyle.accent)
                        .symbolSize(18)
                }
                .chartYScale(domain: low...max(high, low + 1))
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 5)) { value in
                    if let raw = value.as(String.self) {
                        AxisValueLabel { Text(BusinessFormat.shortMonth(String(raw.prefix(7))) + " " + String(raw.suffix(2))) }
                    }
                } }
                .frame(height: 180)
                Text(range(csat)).font(.caption).foregroundStyle(.secondary)
            }
            .padding(BoardStyle.Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        }
    }

    private func range(_ csat: SupportCsat) -> String {
        let days = csat.range.days
        guard let low = csat.range.low, let high = csat.range.high else { return "The last \(days) days." }
        let lowDay = csat.range.lowDate.map { " on " + SupportCsatFormat.day($0) } ?? ""
        let highDay = csat.range.highDate.map { " on " + SupportCsatFormat.day($0) } ?? ""
        return "Low \(SupportCsatFormat.score(low))\(lowDay) · high \(SupportCsatFormat.score(high))\(highDay) in the last \(days) days."
    }

    /// What Gumclaw read in the conversations, when the CSAT lane wrote it.
    @ViewBuilder private func read(_ csat: SupportCsat) -> some View {
        if let digest = csat.digest {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    BentoLabel("What Gumclaw read" + (digest.date.map { " · " + SupportCsatFormat.day($0) } ?? ""))
                    Text(digest.headline).font(.title3.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                }
                if !digest.body.isEmpty {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                        ForEach(Array(digest.body.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                            Text(paragraph).font(.callout).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                if !digest.drivers.isEmpty {
                    BoardSection("This week's reasons") {
                        ForEach(digest.drivers) { driver in
                            BoardRow(title: driver.name, value: driver.count.formatted(), chevron: false)
                        }
                    }
                }
                ForEach([digest.weekly, digest.monthly].compactMap { $0 }, id: \.self) { note in
                    VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                        ForEach(Array(note.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                            Text(paragraph).font(.callout).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(BoardStyle.Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        }
    }

    /// The one action of the page: the Support message box, with the request in it, to the agent of the business.
    private func ask(_ csat: SupportCsat) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Button {
                store.newChatRequest = NewChatRequest(text: SupportCsatAsk.text(csat), place: .support)
                dismiss()
            } label: {
                Label("Ask Gumclaw to improve this", systemImage: "wand.and.stars").labelStyle(.titleAndIcon)
            }
            .buttonStyle(CoconutButtonStyle())
            .accessibilityIdentifier("support-csat-ask")
            Text("The request goes to the message box of Support, with these numbers in it, and you send it.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func load() async {
        await store.loadSupportCsat()
        shown = store.supportCsat
        if shown == nil { error = "This Mac has not published customer satisfaction yet." }
        else { error = nil }
    }
}

/// The request that the CSAT page puts in the message box of Support: the same numbers the page shows, so the person's
/// words to Gumclaw name the day they just read, and the reasons the lane found.
enum SupportCsatAsk {
    static func text(_ csat: SupportCsat) -> String {
        var lines: [String] = []
        if let today = csat.today {
            var first = "Customer satisfaction is " + SupportCsatFormat.score(today.score)
            if let date = csat.digest?.date ?? csat.asOf { first += " on " + SupportCsatFormat.day(date) }
            var moves: [String] = []
            if let value = csat.deltas.yesterday, abs(value) >= 0.05 { moves.append(SupportCsatFormat.move(value) + " from yesterday") }
            if let value = csat.deltas.week, abs(value) >= 0.05 { moves.append(SupportCsatFormat.move(value) + " against the week") }
            if !moves.isEmpty { first += " (" + moves.joined(separator: ", ") + ")" }
            lines.append(first + ".")
        }
        if let digest = csat.digest {
            if !digest.headline.isEmpty { lines.append(digest.headline) }
            if !digest.drivers.isEmpty {
                lines.append("The reasons this week: "
                             + digest.drivers.map { "\($0.name) \($0.count)" }.joined(separator: ", ") + ".")
            }
        }
        lines.append("Work on raising it, starting with the biggest reason, and tell me what you changed.")
        return lines.joined(separator: "\n\n")
    }
}
