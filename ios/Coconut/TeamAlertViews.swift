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

/// The filter of the alerts of a chat: "All", "Critical", and one chip for each type with its count. The chips scroll
/// to the side when the chat has many types. A tap on the chosen type gives each type again.
struct TeamAlertFilterBar: View {
    let chips: [TeamAlertChip]
    let choose: (TeamAlertFilter) -> Void

    /// The height of the bar in the Mac window, where the chat keeps this space under its title.
    static let macHeight: CGFloat = 36

    var body: some View {
        // A few chips keep their own width. More chips than the width holds scroll to the side.
        ViewThatFits(in: .horizontal) {
            row
            ScrollView(.horizontal) { row }.scrollIndicators(.hidden)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Filter alerts")
        .accessibilityIdentifier("team-alert-filter")
    }

    private var row: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            ForEach(chips) { chip in
                ChipButton(title: chip.title, selected: chip.selected, count: chip.countText,
                           symbol: chip.kind == .critical ? TeamAlertLevel.critical.symbol : nil) {
                    choose(chip.filter)
                }
                .accessibilityLabel(chip.spoken)
                .accessibilityIdentifier("team-alert-filter-\(chip.id)")
                // A line parts the level from the types.
                if chip.kind == .critical { Divider().frame(height: 18).padding(.horizontal, BoardStyle.Space.xs) }
            }
        }
        .padding(.horizontal, BoardStyle.Space.s)
    }
}
