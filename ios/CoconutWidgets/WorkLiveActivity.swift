import ActivityKit
import SwiftUI
import WidgetKit

/// The work of the agents on the Lock Screen and in the Dynamic Island (CoconutWorkAttributes): the card that waits
/// first, then the agents that work, each with its step and the time of that step, as the rows of Chats show them. A
/// tap opens the card, the one agent that works, or every agent that works.
struct WorkLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: CoconutWorkAttributes.self) { context in
            // A state of the Mac comes sealed, and opens here with the key of this iPhone (ContentState.shown).
            let state = context.state.shown
            WorkLockScreen(state: state, stale: context.isStale)
                .widgetURL(state.link.url)
        } dynamicIsland: { context in
            let state = context.state.shown
            let stale = context.isStale
            return DynamicIsland {
                // How many cards wait and how many agents work, with the marks of their rows, beside the camera (the
                // words do not fit there), then the card and the newest step. Each row has its own time, so the corner
                // on the right stays empty.
                DynamicIslandExpandedRegion(.leading) {
                    HStack(spacing: 8) {
                        CoconutMark().frame(height: 12)
                        if stale {
                            Text(WorkLockScreen.time(state)).foregroundStyle(.secondary)
                        } else {
                            if state.cards > 0 {
                                Label("\(state.cards)", systemImage: "circle.inset.filled")
                                    .foregroundStyle(BoardStyle.attention)
                            }
                            if state.workingCount > 0 {
                                Label("\(state.workingCount)", systemImage: "circle.dotted")
                                    .foregroundStyle(BoardStyle.positive)
                            }
                        }
                    }
                    .labelStyle(CountLabelStyle())
                    .font(.caption.weight(.semibold).monospacedDigit())
                    .lineLimit(1)
                    .padding(.leading, 6)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(stale ? "As of " + WorkLockScreen.time(state) : state.summary)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(WorkLockScreen.rows(state).prefix(2)) { row in
                            GlanceRowView(row: row, now: .now, stale: stale)
                        }
                    }
                    .padding(.horizontal, 6)
                }
            } compactLeading: {
                CoconutMark().frame(width: 20)
            } compactTrailing: {
                if state.card != nil {
                    Image(systemName: "circle.inset.filled").font(.caption.weight(.semibold))
                        .foregroundStyle(BoardStyle.attention)
                } else {
                    WorkTime(state: state, stale: stale).font(.caption.weight(.semibold))
                }
            } minimal: {
                if state.card != nil {
                    Image(systemName: "circle.inset.filled").font(.caption.weight(.semibold))
                        .foregroundStyle(BoardStyle.attention)
                } else {
                    CoconutMark().frame(width: 18)
                }
            }
            .widgetURL(state.link.url)
            .keylineTint(BoardStyle.control)
        }
    }
}

/// A count after its mark, close together.
private struct CountLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 3) {
            configuration.icon.imageScale(.small)
            configuration.title
        }
    }
}

/// The time of the newest step, live, or the number of the agents that work once the state is old.
private struct WorkTime: View {
    let state: CoconutWorkAttributes.ContentState
    let stale: Bool

    var body: some View {
        if !stale, let start = state.working.first?.startedAt {
            StepTime(start: start, now: .now)
                .foregroundStyle(BoardStyle.positive)
        } else if state.workingCount > 0 {
            Text("\(state.workingCount)").monospacedDigit().foregroundStyle(BoardStyle.positive)
        }
    }
}

/// The activity on the Lock Screen: what waits and what works, then the card and up to three agents.
struct WorkLockScreen: View {
    let state: CoconutWorkAttributes.ContentState
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                CoconutMark().frame(height: 14)
                Text(state.unreadable ? "Open Coconut" : state.summary).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer(minLength: 0)
                if stale {
                    Text("As of " + Self.time(state)).font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(Self.rows(state).prefix(3)) { row in
                GlanceRowView(row: row, now: .now, stale: stale)
            }
        }
        .padding(16)
    }

    /// The card first, then the agents that work, as the rows of a widget.
    static func rows(_ state: CoconutWorkAttributes.ContentState) -> [CoconutGlance.Row] {
        (state.card.map { [CoconutGlance.Row.card($0)] } ?? []) + state.working.map(CoconutGlance.Row.work)
    }

    static func time(_ state: CoconutWorkAttributes.ContentState) -> String {
        state.updatedAt.formatted(date: .omitted, time: .shortened)
    }
}
