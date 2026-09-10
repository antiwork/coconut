import SwiftUI

enum BoardStyle {
    static let paper = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.065, alpha: 1) : .systemBackground })
    static let surface = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.13, alpha: 1) : UIColor(white: 0.955, alpha: 1) })
    static let userBubble = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 0.36, alpha: 1) : UIColor(white: 0.86, alpha: 1) })
    static let userBubbleLink = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.75, green: 0.87, blue: 1, alpha: 1) : UIColor(red: 0, green: 0.31, blue: 0.68, alpha: 1) })
    static let ink = Color.primary
    static let accent = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.99, green: 0.52, blue: 0.36, alpha: 1) : UIColor(red: 0.72, green: 0.27, blue: 0.14, alpha: 1) })
    static let green = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.56, green: 0.75, blue: 0.60, alpha: 1) : UIColor(red: 0.27, green: 0.43, blue: 0.32, alpha: 1) })
    static let line = Color(uiColor: UIColor { traits in
        let color: UIColor = traits.userInterfaceStyle == .dark ? .white : .black
        return color.withAlphaComponent(traits.accessibilityContrast == .high ? 0.4 : 0.1)
    })
    static let control = Color.primary
    static let controlLabel = Color(uiColor: .systemBackground)
}

extension Color {
    init(hex: String) {
        let clean = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        let value = UInt64(clean, radix: 16) ?? 0x75867B
        self.init(.sRGB, red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255, opacity: 1)
    }
}

struct BotAvatar: View {
    let symbol: String
    let color: String
    var size: CGFloat = 52

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size * 0.42, weight: .semibold))
            .foregroundStyle(symbolColor)
            .frame(width: size, height: size)
            .background(Color(hex: color), in: Circle())
            .accessibilityHidden(true)
    }

    private var symbolColor: Color {
        let value = UInt64(color.trimmingCharacters(in: CharacterSet.alphanumerics.inverted), radix: 16) ?? 0x75867B
        let red = Double((value >> 16) & 255) / 255
        let green = Double((value >> 8) & 255) / 255
        let blue = Double(value & 255) / 255
        return red * 0.2126 + green * 0.7152 + blue * 0.0722 > 0.65 ? .black : .white
    }
}

struct StatusLabel: View {
    let status: String
    var compact = false

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .bold))
            Text(label)
                .font(compact ? .caption2.weight(.medium) : .caption.weight(.medium))
        }
        .foregroundStyle(tint)
    }

    private var label: String {
        switch status {
        case "needs_you", "waiting", "pending": "Needs you"
        case "working", "running": "Working"
        case "scheduled", "queued": "Scheduled"
        case "failed": "Failed"
        case "completed", "executed": "Complete"
        case "checks_running": "Checks running"
        case "ready_for_review": "Ready for review"
        case "opened": "Opened"
        case "merged": "Merged"
        case "preparing": "Preparing"
        case "cancelled", "canceled": "Stopped"
        default: status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private var symbol: String {
        switch status {
        case "needs_you", "waiting", "pending": "circle.inset.filled"
        case "working", "running", "checks_running": "circle.dotted"
        case "scheduled", "queued": "clock"
        case "failed": "exclamationmark.circle.fill"
        case "completed", "executed", "merged": "checkmark"
        default: "circle"
        }
    }

    private var tint: Color {
        switch status {
        case "needs_you", "waiting", "pending", "failed": BoardStyle.accent
        case "working", "running", "completed", "executed", "merged": BoardStyle.green
        default: .secondary
        }
    }
}

struct QuietEmptyState: View {
    let symbol: String
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 30, weight: .light))
                .foregroundStyle(.secondary)
                .frame(width: 72, height: 72)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 24))
            Text(title).font(.title3.weight(.semibold))
            Text(message).font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: 280)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 70)
        .padding(.horizontal, 24)
    }
}

struct SectionEyebrow: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.semibold))
            .tracking(1.1)
            .foregroundStyle(.secondary)
    }
}
