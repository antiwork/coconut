import SwiftUI

/// The label of an alert above its bubble: the symbol of the level, the type and the level in words. A critical alert
/// has a filled capsule, a filled symbol and its level in bold, so it differs in form and in words, not only in color.
/// An alert that resolves an earlier one says Resolved, with a green check, in place of its level.
struct TeamAlertLabel: View {
    let alert: TeamAlert

    private var critical: Bool { alert.level == .critical && !alert.resolved }

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if alert.resolved {
                Image(systemName: "checkmark.circle").imageScale(.small).foregroundStyle(BoardStyle.positive)
            } else {
                Image(systemName: alert.level.symbol).imageScale(.small)
            }
            Text(alert.type).fontWeight(.semibold)
            Text("·")
            Text(alert.resolved ? "Resolved" : alert.level.title).fontWeight(critical ? .bold : .regular)
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
        .accessibilityIdentifier("team-alert-label-\(alert.type)-\(alert.resolved ? "resolved" : alert.level.rawValue)")
    }
}
