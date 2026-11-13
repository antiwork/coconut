import SwiftUI

// One line below the newest message: what the bot does now. The header avatar is the only live character.
struct BotStatusRow: View {
    static let minimumDwell = 1.2
    let bot: Bot
    let activity: ConversationActivity
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown: BotPhase?
    @State private var shownAt = Date.distantPast

    private var phase: BotPhase { shown ?? activity.phase }
    private var writing: Bool { phase == .writing && !reduceMotion && !activity.reconnecting }
    private var fade: Animation? { BoardStyle.fade(reduceMotion: reduceMotion) }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 15)) { timeline in
            let label = label(at: timeline.date)
            HStack(spacing: 8) {
                if writing { TypingDots().transition(.opacity) }
                ZStack(alignment: .leading) {
                    ShimmerText(text: label, active: !activity.reconnecting)
                        .id(label)
                        .transition(.opacity)
                }
            }
            .font(.subheadline)
            .animation(fade, value: label)
            .animation(fade, value: writing)
        }
        .frame(minHeight: 32, alignment: .leading)
        // The text starts at the edge of the text in a bubble.
        .padding(.leading, BoardStyle.Space.m)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(bot.name): \(label(at: .now))")
        .accessibilityIdentifier("bot-working-\(bot.id)")
        .task(id: activity.phase) {
            // Hold each label long enough to read, so fast tool calls do not flicker.
            let wait = Self.minimumDwell - Date().timeIntervalSince(shownAt)
            if shown != nil, wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
            guard !Task.isCancelled, shown != activity.phase else { return }
            withAnimation(fade) { shown = activity.phase }
            shownAt = .now
        }
    }

    private func label(at date: Date) -> String {
        if activity.reconnecting { return "Reconnecting…" }
        let text = phase.label
        guard let startedAt = activity.startedAt, date.timeIntervalSince(startedAt) >= 60,
              let span = CompactAgeLabel.span(date.timeIntervalSince(startedAt)) else { return text }
        return "\(text) · \(span)"
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
