import SwiftUI

// One line below the newest message: what the bot does now. The header avatar is the only live character.
struct BotStatusRow: View {
    static let minimumDwell = 1.2
    let bot: Bot
    let activity: ConversationActivity
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown: BotPhase?
    @State private var shownAt = Date.distantPast
    /// The start of the step that the row shows. A newer step that still waits for its dwell keeps its own time.
    @State private var shownStart: Date?

    private var phase: BotPhase { shown ?? activity.phase }
    private var writing: Bool { phase == .writing && !reduceMotion && !activity.reconnecting }
    private var fade: Animation? { BoardStyle.fade(reduceMotion: reduceMotion) }

    // The detail and the time belong to the phase that the row shows, not to a newer one that still waits for its dwell.
    private var current: Bool { shown == nil || shown == activity.phase }
    private var detail: String? { current ? activity.detail : nil }
    private var start: Date? { current ? activity.startedAt : shownStart }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                HStack(spacing: 8) {
                    if writing { TypingDots().transition(.opacity) }
                    ZStack(alignment: .leading) {
                        ShimmerText(text: label, active: !activity.reconnecting)
                            .id(label)
                            .transition(.opacity)
                    }
                    // The time of the newest step to the second, and the model, as a professional tool shows them.
                    if !activity.reconnecting {
                        // Read the start here, not in the timeline: state read inside it left a stale copy of the
                        // line, which faded in and out over the live one.
                        let start = start
                        TimelineView(.periodic(from: .now, by: 1)) { timeline in
                            Text(meta(at: timeline.date, from: start))
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(([bot.name + ": " + label, detail, meta(at: .now, from: start)].compactMap { $0 }).joined(separator: ", "))
                .accessibilityIdentifier("bot-working-\(bot.id)")
            }
            .font(.subheadline)
            if let detail, !activity.reconnecting {
                Text(detail)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .transition(.opacity)
                    .accessibilityHidden(true)
            }
        }
        .animation(fade, value: label)
        .animation(fade, value: writing)
        .animation(fade, value: detail)
        .frame(minHeight: 32, alignment: .leading)
        // The text starts at the edge of the text in a bubble.
        .padding(.leading, BoardStyle.Space.m)
        .task(id: activity.phase) {
            // Hold each label long enough to read, so fast tool calls do not flicker.
            let wait = Self.minimumDwell - Date().timeIntervalSince(shownAt)
            if shown != nil, wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
            guard !Task.isCancelled, shown != activity.phase else { return }
            withAnimation(fade) { shown = activity.phase }
            shownAt = .now
            shownStart = activity.startedAt
        }
        // A new search, file or command within the same phase starts a new step: its time starts again.
        .onChange(of: activity.startedAt) { _, start in
            if shown == activity.phase { shownStart = start }
        }
    }

    private var label: String { activity.reconnecting ? "Reconnecting…" : phase.label }

    /// "· 23s · Claude Opus 5.5 · 2 queued": the time since the newest step started, which matches the step that the
    /// label names, the model of the turn, and the messages that wait for the next turn.
    func meta(at date: Date, from start: Date?) -> String {
        var parts: [String] = []
        if let start { parts.append(ConversationActivity.elapsed(date.timeIntervalSince(start))) }
        if let model = activity.model, !model.isEmpty { parts.append(model) }
        if let queued = MessageQueue.label(activity.queued) { parts.append(queued) }
        return parts.map { "· " + $0 }.joined(separator: " ")
    }
}

// Three dots show that the bot writes a reply.
struct TypingDots: View {
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var dot = 6

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: scenePhase != .active)) { timeline in
            let time = timeline.date.timeIntervalSinceReferenceDate
            // The three dots sit in a triangle, as the three holes of a coconut: two eyes above, the mouth below.
            ZStack {
                ForEach(0..<3) { index in
                    let level = Self.level(index: index, time: time)
                    Circle()
                        .fill(Color.primary.opacity(0.35 + 0.65 * level))
                        .frame(width: dot, height: dot)
                        .scaleEffect(1 + 0.18 * level)
                        .offset(Self.hole(index, dot: dot))
                }
            }
            .frame(width: dot * 2.6, height: dot * 2.3)
            // The coconut rolls: one turn in 1.6 s while the wave crosses its holes. Reduce Motion keeps it upright.
            .rotationEffect(.degrees(reduceMotion ? 0 : Self.roll(time: time)))
        }
        .accessibilityHidden(true)
    }

    /// The place of a dot around the center: the left eye, the right eye, then the mouth.
    static func hole(_ index: Int, dot: CGFloat) -> CGSize {
        [CGSize(width: -dot * 0.8, height: -dot * 0.65), CGSize(width: dot * 0.8, height: -dot * 0.65),
         CGSize(width: 0, height: dot * 0.65)][index]
    }

    /// The angle of the rolling triangle at `time`, in degrees: a full turn every 1.6 s.
    static func roll(time: Double) -> Double {
        time.truncatingRemainder(dividingBy: 1.6) / 1.6 * 360
    }

    // A wave crosses the three dots in 0.6 s, then rests for 0.4 s.
    static func level(index: Int, time: Double) -> Double {
        let phase = time.truncatingRemainder(dividingBy: 1.0) - Double(index) * 0.2
        guard phase >= 0, phase < 0.4 else { return 0 }
        return sin(.pi * phase / 0.4)
    }
}
