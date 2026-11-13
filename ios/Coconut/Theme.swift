import SwiftUI

enum BoardStyle {
    static let paper = Color.adaptive { dark, _ in dark ? PlatformColor(white: 0.065, alpha: 1) : .pageBackground }
    static let surface = Color.adaptive { dark, _ in PlatformColor(white: dark ? 0.13 : 0.955, alpha: 1) }
    static let userBubble = Color.adaptive { dark, _ in PlatformColor(white: dark ? 0.36 : 0.86, alpha: 1) }
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
    static let line = Color.adaptive { dark, highContrast in
        (dark ? PlatformColor.white : .black).withAlphaComponent(highContrast ? 0.4 : 0.1)
    }
    static let control = Color.primary
    static let controlLabel = Color(platformColor: .pageBackground)
    // Semantic tints. Each one names a meaning, not a hue.
    static let positive = green
    static let attention = accent
    static let destructive = Color(platformColor: .systemRed)

    /// The fills of a face with letters, for a team or a person, with the ink of the letters. They are fills and inks
    /// of the fruit faces of the bots, so they read the same in light and in dark mode.
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
        VStack(spacing: 14) {
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
