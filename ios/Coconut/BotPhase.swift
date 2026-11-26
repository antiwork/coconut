import SwiftUI

// The Mac reports one phase for each running turn. Keep the cases in step with PHASES in turn_phase.py.
enum BotPhase: String, CaseIterable, Sendable {
    case thinking, working, reading, editing, command, testing, searching, browsing, memory, github, helpers, writing

    var label: String {
        switch self {
        case .thinking: "Thinking it through"
        case .working: "Working on it"
        case .reading: "Reading files"
        case .editing: "Editing files"
        case .command: "Running a command"
        case .testing: "Running tests"
        case .searching: "Searching files"
        case .browsing: "Searching the web"
        case .memory: "Checking memory"
        case .github: "Working in GitHub"
        case .helpers: "Working with helpers"
        case .writing: "Writing…"
        }
    }
}

struct ConversationActivity: Equatable {
    var phase: BotPhase
    /// When the newest step of the turn started: the last action, which the label names. Its time shows next to the
    /// label, so a web search that began 23 seconds ago says 23s, however long the turn has run.
    var startedAt: Date?
    var reconnecting: Bool
    /// The command, file or query of the phase, as Codex and Claude Code show it.
    var detail: String? = nil
    /// The name of the model that runs the turn.
    var model: String? = nil
    /// How many messages of the person wait for the next turn.
    var queued = 0

    /// A time to the second: 9s, 4m 5s, 1h 2m 5s. No part has a leading zero (the person: "06s is weird just 6s").
    static func elapsed(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        let (hours, minutes, rest) = (total / 3600, total / 60 % 60, total % 60)
        if hours > 0 { return "\(hours)h \(minutes)m \(rest)s" }
        if minutes > 0 { return "\(minutes)m \(rest)s" }
        return "\(rest)s"
    }
}

/// The time since the step that an agent does now started, to the second and live: the age of a working chat in the
/// chat list. It matches the time on the working line of the chat.
struct StepTimeLabel: View {
    let start: Date

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            Text(ConversationActivity.elapsed(context.date.timeIntervalSince(start)))
        }
    }
}

// A bright band crosses the label from left to right, then rests. A keyframe animator of
// its own moves the band, so the label does not redraw on every frame. A repeating
// withAnimation in onAppear also took along whatever else changed in that update: the
// elapsed time beside the label then faded between its first value and the live one.
struct ShimmerText: View {
    static let sweep = 1.2
    static let rest = 0.6
    // The band covers 0.44 of the width. Past 1.3 it is off the label, which makes the rest.
    static let start = -0.3, end = 1.3 + (1.3 - -0.3) * rest / sweep
    let text: String
    var active = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let base = Text(text).lineLimit(1)
        base.foregroundStyle(.secondary)
            .overlay(alignment: .leading) {
                if active && !reduceMotion {
                    base.foregroundStyle(.primary)
                        .mask(alignment: .leading) {
                            GeometryReader { geometry in
                                let width = geometry.size.width
                                LinearGradient(colors: [.clear, .black, .clear], startPoint: .leading, endPoint: .trailing)
                                    .frame(width: width * 0.44)
                                    .keyframeAnimator(initialValue: Self.start, repeating: true) { band, position in
                                        band.offset(x: (position - 0.22) * width)
                                    } keyframes: { _ in
                                        LinearKeyframe(Self.end, duration: Self.sweep + Self.rest)
                                    }
                            }
                        }
                }
            }
    }
}
