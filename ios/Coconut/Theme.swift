import SwiftUI

// Swift's Hasher changes between launches. The fill of a face must not.
func characterSeed(_ value: String) -> UInt64 {
    value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
}

enum BoardStyle {
    static let paper = Color.adaptive { dark, _ in dark ? PlatformColor(white: 0.065, alpha: 1) : .pageBackground }
    static let surface = Color.adaptive { dark, _ in PlatformColor(white: dark ? 0.13 : 0.955, alpha: 1) }
    static let userBubble = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.39, green: 0.26, blue: 0.18, alpha: 1)
             : PlatformColor(red: 0.88, green: 0.77, blue: 0.65, alpha: 1)
    }
    static let userBubbleLink = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.75, green: 0.87, blue: 1, alpha: 1) : PlatformColor(red: 0, green: 0.31, blue: 0.68, alpha: 1)
    }
    static let ink = Color.primary
    static let accent = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.99, green: 0.52, blue: 0.36, alpha: 1) : PlatformColor(red: 0.72, green: 0.27, blue: 0.14, alpha: 1)
    }
    static let green = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.56, green: 0.75, blue: 0.60, alpha: 1) : PlatformColor(red: 0.27, green: 0.43, blue: 0.32, alpha: 1)
    }
    /// The brown of the husk in the app icon, the same in light and dark mode. A swipe action takes it, because its
    /// label is always white.
    static let husk = Color(platformColor: PlatformColor(red: 0.48, green: 0.31, blue: 0.18, alpha: 1))
    /// The coconut brown of the app: the husk in light mode, and a lighter tan in dark mode, so it reads on the dark page.
    static let brown = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.83, green: 0.65, blue: 0.45, alpha: 1) : PlatformColor(red: 0.48, green: 0.31, blue: 0.18, alpha: 1)
    }
    static let line = Color.adaptive { dark, highContrast in
        (dark ? PlatformColor.white : .black).withAlphaComponent(highContrast ? 0.4 : 0.1)
    }
    /// The fill of each button and the tint of the app: the selected tab, the toolbar, links and switches. It is coconut
    /// brown, never black: the person rejects black buttons. The unread badge takes the green of the New line.
    static let control = brown
    static let controlLabel = Color(platformColor: .pageBackground)
    // Semantic tints. Each one names a meaning, not a hue.
    static let positive = green
    static let attention = accent
    static let destructive = Color(platformColor: .systemRed)

    /// The fills of a face with letters, for a team or a person, with the ink of the letters. They read the same in light
    /// and in dark mode. An agent has no face.
    static let faceScale: CGFloat = 1.1
    static let faceColors: [(fill: String, ink: String)] = [
        ("7E99D3", "1F334E"), ("70A779", "FCEDCE"), ("AA79C2", "FCF0DD"),
        ("F2A088", "263329"), ("EAA052", "263329"), ("D76C91", "FFEBD6"), ("B6C493", "263329"),
        ("EBC96C", "263329"),
    ]

    /// The one fade for status text. Reduce Motion turns it off.
    static func fade(reduceMotion: Bool) -> Animation? { reduceMotion ? nil : .easeOut(duration: 0.2) }

    /// Corner radii. A shape uses the radius of its size class, not a value of its own.
    enum Radius {
        static let large: CGFloat = 24
        static let medium: CGFloat = 16
        static let small: CGFloat = 12
    }

    /// The space scale. Each padding and each gap is one of these values. `line` is only for the gap between two
    /// lines of one block of text, for example a name and the line under it.
    enum Space {
        static let line: CGFloat = 2
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
    }

    /// The face at the start of a row of the Chats list. The Mac sidebar has its own smaller face.
    static let rowFace: CGFloat = 46

    #if os(macOS)
    /// The height of the unread badge and of the "@" mark: the height of one line of text under the name of a chat
    /// row. A badge fills the second line of the row, so it ends where that line ends, above and under. The badge
    /// grows with that text.
    static let badge: CGFloat = 14
    #else
    static let badge: CGFloat = 18
    #endif
}

/// The button of Coconut. The primary kind is a coconut brown capsule (BoardStyle.control) with a label in the paper
/// color, as the send button of the composer. The quiet kind is a soft capsule of the same brown with a brown label,
/// for the second action next to it. The same on a Mac and on an iPhone; an iPhone gets the height that a finger needs.
struct CoconutButtonStyle: ButtonStyle {
    enum Kind { case primary, quiet }

    var kind: Kind = .primary
    /// A large button for the one action of a setup page.
    var large = false
    @Environment(\.isEnabled) private var enabled

    #if os(macOS)
    static let height: CGFloat = 28
    #else
    static let height: CGFloat = 36
    #endif

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font((large ? Font.body : .subheadline).weight(.semibold))
            .lineLimit(1)
            .padding(.horizontal, large ? BoardStyle.Space.l : BoardStyle.Space.m)
            .frame(minHeight: Self.height + (large ? 8 : 0))
            .foregroundStyle(kind == .primary ? BoardStyle.controlLabel : BoardStyle.control)
            .background(kind == .primary ? BoardStyle.control : BoardStyle.control.opacity(0.12), in: Capsule())
            .opacity(!enabled ? 0.4 : configuration.isPressed ? 0.65 : 1)
            .contentShape(Capsule())
    }
}

extension Color {
    init(hex: String) {
        let clean = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        let value = UInt64(clean, radix: 16) ?? 0x75867B
        self.init(.sRGB, red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255, opacity: 1)
    }
}

struct StatusLabel: View {
    let status: String
    var compact = false

    var body: some View {
        // The symbol takes the text style of the word next to it.
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: symbol)
                .imageScale(.small)
                .fontWeight(.semibold)
            Text(Self.label(for: status))
        }
        .font(compact ? .subheadline : .footnote)
        .foregroundStyle(tint)
    }

    static func label(for status: String) -> String {
        switch status {
        case "needs_you", "waiting", "pending": "Needs you"
        case "working", "running": "Working"
        case "scheduled": "Scheduled"
        case "queued": "Queued"
        case "failed": "Failed"
        case "completed", "executed": "Complete"
        case "checks_running": "Checks running"
        case "ready_for_review": "Ready for review"
        case "opened": "Opened"
        case "merged": "Merged"
        case "closed": "Closed"
        case "blocked": "Blocked"
        case "needs_recovery": "Needs recovery"
        case "preparing": "Preparing"
        case "status_out_of_date": "Status out of date"
        case "cancelled", "canceled": "Stopped"
        case "paused": "Paused"
        default: status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private var symbol: String {
        switch status {
        case "needs_you", "waiting", "pending": "circle.inset.filled"
        case "working", "running", "checks_running": "circle.dotted"
        case "scheduled", "queued": "clock"
        case "paused": "pause"
        case "failed", "blocked", "needs_recovery": "exclamationmark.circle.fill"
        case "completed", "executed", "merged": "checkmark"
        default: "circle"
        }
    }

    private var tint: Color {
        switch status {
        case "needs_you", "waiting", "pending", "failed", "blocked", "needs_recovery": BoardStyle.attention
        case "working", "running", "completed", "executed", "merged": BoardStyle.positive
        default: .secondary
        }
    }
}

struct QuietEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    @ScaledMetric(relativeTo: .title) private var iconSize = 72
    @ScaledMetric(relativeTo: .subheadline) private var messageWidth = 280

    var body: some View {
        VStack(spacing: BoardStyle.Space.m) {
            Image(systemName: symbol)
                .font(.largeTitle.weight(.light))
                .foregroundStyle(.secondary)
                .frame(width: iconSize, height: iconSize)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
            Text(title).font(.title3.weight(.semibold))
            Text(message).font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: messageWidth)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 70)
        .padding(.horizontal, BoardStyle.Space.xl)
    }
}

/// The words of the Stop confirmation in the chat.
enum StopCopy {
    static let message = "The Mac records the stop request. An action that already runs can finish before it stops."
    static func title(executor: String) -> String { "Stop \(executor)'s task?" }
}

/// A Stop asks before it acts. The confirmation holds the item until the person stops it or cancels.
struct StopConfirmation<Item> {
    private(set) var item: Item?
    var isPresented: Bool {
        get { item != nil }
        set { if !newValue { item = nil } }
    }

    mutating func request(_ item: Item) { self.item = item }
}

/// The one place that turns a published figure into the string a person reads. Nothing here computes a number: every
/// value arrives already published by the generator, and a value that did not arrive reads as a dash, never as zero.
enum BusinessFormat {
    /// Millions keep two decimals, thousands one, and anything smaller shows whole dollars. `$8.50M`, `$268.4K`, `$842`.
    static func money(_ value: Double?, compact: Bool = true) -> String {
        guard let value, value.isFinite else { return "—" }
        let magnitude = abs(value)
        let sign = value < 0 ? "-" : ""
        if compact, magnitude >= 1_000_000_000 { return sign + "$" + decimals(magnitude / 1_000_000_000, 2) + "B" }
        if compact, magnitude >= 1_000_000 { return sign + "$" + decimals(magnitude / 1_000_000, 2) + "M" }
        if compact, magnitude >= 1_000 { return sign + "$" + decimals(magnitude / 1_000, 1) + "K" }
        return sign + "$" + whole(magnitude)
    }

    /// The same scale without a dollar sign, for a count that a person reads at the same size as money.
    static func number(_ value: Double?, money: Bool = false) -> String {
        money ? Self.money(value) : compact(value)
    }

    /// `51.8%`, or `+2.3%` when the sign carries the meaning.
    static func percent(_ value: Double?, signed: Bool = false) -> String {
        guard let value, value.isFinite else { return "—" }
        let sign = signed && value > 0 ? "+" : ""
        return sign + decimals(value, 1) + "%"
    }

    /// `2026-10` as `October` in the current year and `Oct 2025` before it, `2026Q4` as `Q4 2026`, `2021` as it is.
    static func month(_ raw: String, now: Date = .now, calendar: Calendar = .current) -> String {
        if let q = raw.firstIndex(of: "Q"), q != raw.startIndex { return String(raw[q...]) + " " + String(raw[..<q]) }
        let parts = raw.split(separator: "-")
        guard parts.count >= 2, let year = Int(parts[0]), let month = Int(parts[1]) else { return raw }
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = 1
        guard let date = Calendar(identifier: .gregorian).date(from: components) else { return raw }
        if calendar.component(.year, from: now) == year { return date.formatted(.dateTime.month(.wide)) }
        return date.formatted(.dateTime.month(.abbreviated).year())
    }

    /// `2026-10` as `Oct`, for an axis whose year is already written beside it.
    static func shortMonth(_ raw: String) -> String {
        let parts = raw.split(separator: "-")
        guard parts.count >= 2, let month = Int(parts[1]) else { return raw }
        var components = DateComponents()
        components.year = 2000
        components.month = month
        components.day = 1
        guard let date = Calendar(identifier: .gregorian).date(from: components) else { return raw }
        return date.formatted(.dateTime.month(.abbreviated))
    }

    /// A published day as a person says it: `Today`, `Yesterday`, `Monday` within the week, `Nov 16` this year, `Nov 16, 2025` before.
    static func day(_ raw: String, now: Date = .now, calendar: Calendar = .current) -> String {
        guard let date = calendarDay(raw, calendar: calendar) else { return raw }
        return Self.date(date, now: now, calendar: calendar)
    }

    /// `2026-11-16` as `Nov 16`, for a chart axis where every label must stay short and in order.
    static func axisDay(_ raw: String) -> String {
        guard let date = calendarDay(raw, calendar: .current) else { return raw }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }

    /// The day of a moment: `Today`, `Yesterday`, `Monday`, `Nov 16`, or `Nov 16, 2025`.
    static func date(_ date: Date, now: Date = .now, calendar: Calendar = .current) -> String {
        let today = calendar.startOfDay(for: now)
        let day = calendar.startOfDay(for: date)
        let days = calendar.dateComponents([.day], from: day, to: today).day ?? 0
        if days == 0 { return "Today" }
        if days == 1 { return "Yesterday" }
        if days == -1 { return "Tomorrow" }
        if days > 1, days < 7 { return date.formatted(.dateTime.weekday(.wide)) }
        if calendar.component(.year, from: date) == calendar.component(.year, from: now) {
            return date.formatted(.dateTime.month(.abbreviated).day())
        }
        return date.formatted(.dateTime.month(.abbreviated).day().year())
    }

    /// A moment as a person says it: `Just now`, `5 minutes ago`, `3 hours ago` today, then `Yesterday at 4:02 PM` or `Nov 16 at 4:02 PM`.
    static func time(_ date: Date, now: Date = .now, calendar: Calendar = .current) -> String {
        let age = now.timeIntervalSince(date)
        if age >= -60, age < 60 { return "Just now" }
        if age > 0, age < 3600 { let minutes = Int(age / 60); return "\(minutes) minute\(minutes == 1 ? "" : "s") ago" }
        if age > 0, calendar.isDate(date, inSameDayAs: now) { let hours = Int(age / 3600); return "\(hours) hour\(hours == 1 ? "" : "s") ago" }
        return Self.date(date, now: now, calendar: calendar) + " at " + date.formatted(date: .omitted, time: .shortened)
    }

    /// `2026-11-16` as noon of that day in the person's calendar, so a time zone never moves it to another day.
    private static func calendarDay(_ raw: String, calendar: Calendar) -> Date? {
        let parts = raw.prefix(10).split(separator: "-")
        guard parts.count == 3, let year = Int(parts[0]), let month = Int(parts[1]), let day = Int(parts[2]) else { return nil }
        return calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 12))
    }

    /// A write-off keeps the publisher's two-decimal-million precision, because it is the figure a person checks.
    static func writeOff(_ value: Double) -> String {
        if abs(value) >= 1_000_000 { return (value < 0 ? "-" : "") + "$" + decimals(abs(value) / 1_000_000, 2) + "M" }
        return value.formatted(.currency(code: "USD").precision(.fractionLength(0...2)))
    }

    private static func compact(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "—" }
        let magnitude = abs(value)
        let sign = value < 0 ? "-" : ""
        if magnitude >= 1_000_000_000 { return sign + decimals(magnitude / 1_000_000_000, 2) + "B" }
        if magnitude >= 1_000_000 { return sign + decimals(magnitude / 1_000_000, 2) + "M" }
        if magnitude >= 1_000 { return sign + decimals(magnitude / 1_000, 1) + "K" }
        return sign + whole(magnitude)
    }

    private static func decimals(_ value: Double, _ places: Int) -> String {
        value.formatted(.number.precision(.fractionLength(places)))
    }

    private static func whole(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0)).grouping(.automatic))
    }
}
