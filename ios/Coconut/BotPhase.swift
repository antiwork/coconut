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

    var gesture: CoconutGesture {
        switch self {
        case .thinking, .working: .ponder
        case .reading: .scan
        case .editing, .command, .testing: .focus
        case .searching, .browsing: .dart
        case .memory: .recall
        case .github, .helpers: .spin
        case .writing: .write
        }
    }
}

struct ConversationActivity: Equatable {
    var phase: BotPhase
    var startedAt: Date?
    var reconnecting: Bool
    /// The command, file or query of the phase, as Codex and Claude Code show it.
    var detail: String? = nil
    /// The name of the model that runs the turn.
    var model: String? = nil

    /// The time of a turn to the second: 9s, 4m 05s, 1h 02m 05s.
    static func elapsed(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        let (hours, minutes, rest) = (total / 3600, total / 60 % 60, total % 60)
        if hours > 0 { return String(format: "%dh %02dm %02ds", hours, minutes, rest) }
        if minutes > 0 { return String(format: "%dm %02ds", minutes, rest) }
        return "\(rest)s"
    }
}

// A bright band crosses the label from left to right, then rests. One repeating offset
// animation moves the band, so the label does not redraw on every frame.
struct ShimmerText: View {
    static let sweep = 1.2
    static let rest = 0.6
    // The band covers 0.44 of the width. Past 1.3 it is off the label, which makes the rest.
    static let start = -0.3, end = 1.3 + (1.3 - -0.3) * rest / sweep
    let text: String
    var active = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var position = start

    var body: some View {
        let base = Text(text).lineLimit(1)
        base.foregroundStyle(.secondary)
            .overlay(alignment: .leading) {
                if active && !reduceMotion {
                    base.foregroundStyle(.primary)
                        .mask(alignment: .leading) {
                            GeometryReader { geometry in
                                LinearGradient(colors: [.clear, .black, .clear], startPoint: .leading, endPoint: .trailing)
                                    .frame(width: geometry.size.width * 0.44)
                                    .offset(x: (position - 0.22) * geometry.size.width)
                            }
                        }
                        .onAppear {
                            position = Self.start
                            withAnimation(.linear(duration: Self.sweep + Self.rest).repeatForever(autoreverses: false)) {
                                position = Self.end
                            }
                        }
                }
            }
    }
}
