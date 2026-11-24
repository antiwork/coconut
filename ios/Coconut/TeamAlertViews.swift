import SwiftUI

/// The label of an alert above its bubble: the symbol of the level, the type and the level in words. A critical alert
/// has a filled capsule, a filled symbol and its level in bold, so it differs in form and in words, not only in color.
struct TeamAlertLabel: View {
    let alert: TeamAlert

    private var critical: Bool { alert.level == .critical }

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: alert.level.symbol).imageScale(.small)
            Text(alert.type).fontWeight(.semibold)
            Text("·")
            Text(alert.level.title).fontWeight(critical ? .bold : .regular)
        }
        .font(.caption)
        .lineLimit(1)
        .foregroundStyle(critical ? AnyShapeStyle(Color.white) : AnyShapeStyle(.secondary))
        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, BoardStyle.Space.line)
        .background {
            if critical { Capsule().fill(BoardStyle.destructive) } else { Capsule().strokeBorder(BoardStyle.line) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(alert.spoken)
        .accessibilityIdentifier("team-alert-label-\(alert.type)-\(alert.level.rawValue)")
    }
}
