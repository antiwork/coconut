import SwiftUI

// One line below the newest message: what the bot does now. The header avatar is the only live character.
struct BotStatusRow: View {
    static let minimumDwell = 1.2
    let bot: Bot
    let activity: ConversationActivity
    /// Opens the live view of the turn: its steps, its files and its images. Nil for a row without one.
    var openLive: (() -> Void)? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown: BotPhase?
    @State private var shownAt = Date.distantPast

    private var phase: BotPhase { shown ?? activity.phase }
    private var writing: Bool { phase == .writing && !reduceMotion && !activity.reconnecting }
    private var fade: Animation? { BoardStyle.fade(reduceMotion: reduceMotion) }

    // The detail belongs to the phase that the row shows, not to a newer one that still waits for its dwell.
    private var detail: String? { shown == nil || shown == activity.phase ? activity.detail : nil }

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
                    // The time of the turn to the second, and the model, as a professional tool shows them.
                    if !activity.reconnecting {
                        TimelineView(.periodic(from: .now, by: 1)) { timeline in
                            Text(meta(at: timeline.date))
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(([bot.name + ": " + label, detail, meta(at: .now)].compactMap { $0 }).joined(separator: ", "))
                .accessibilityIdentifier("bot-working-\(bot.id)")
                // The line opens the live view of the turn. A small control says so.
                if let openLive, !activity.reconnecting {
                    Button(action: openLive) {
                        HStack(spacing: 3) {
                            Text("Live")
                            Image(systemName: "chevron.right").imageScale(.small)
                        }
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 3)
                        .background(BoardStyle.surface, in: Capsule())
                        #if os(iOS)
                        // A finger needs more room than the capsule.
                        .frame(minWidth: 44, minHeight: 32)
                        #endif
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Show what \(bot.name) does now")
                    .accessibilityLabel("Show what \(bot.name) does now")
                    .accessibilityIdentifier("live-activity-open")
                }
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
        }
    }

    private var label: String { activity.reconnecting ? "Reconnecting…" : phase.label }

    /// "· 4m 05s · Claude Opus 5.5 · 2 queued": the time since the turn started, the model of the turn, and the
    /// messages that wait for the next turn.
    func meta(at date: Date) -> String {
        var parts: [String] = []
        if let startedAt = activity.startedAt { parts.append(ConversationActivity.elapsed(date.timeIntervalSince(startedAt))) }
        if let model = activity.model, !model.isEmpty { parts.append(model) }
        if let queued = MessageQueue.label(activity.queued) { parts.append(queued) }
        return parts.map { "· " + $0 }.joined(separator: " ")
    }
}

// Three dots show that the bot writes a reply.
struct TypingDots: View {
    @Environment(\.scenePhase) private var scenePhase
    @ScaledMetric(relativeTo: .subheadline) private var dot = 6

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: scenePhase != .active)) { timeline in
            let time = timeline.date.timeIntervalSinceReferenceDate
            HStack(spacing: dot / 2) {
                ForEach(0..<3) { index in
                    let level = Self.level(index: index, time: time)
                    Circle()
                        .fill(Color.primary.opacity(0.35 + 0.65 * level))
                        .frame(width: dot, height: dot)
                        .scaleEffect(1 + 0.18 * level)
                }
            }
        }
        .accessibilityHidden(true)
    }

    // A wave crosses the three dots in 0.6 s, then rests for 0.4 s.
    static func level(index: Int, time: Double) -> Double {
        let phase = time.truncatingRemainder(dividingBy: 1.0) - Double(index) * 0.2
        guard phase >= 0, phase < 0.4 else { return 0 }
        return sin(.pi * phase / 0.4)
    }
}
