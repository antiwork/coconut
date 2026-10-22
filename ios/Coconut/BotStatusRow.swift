import SwiftUI

// One line below the newest message: the live character and what it does now.
struct BotStatusRow: View {
    static let minimumDwell = 1.2
    let bot: Bot
    let activity: ConversationActivity
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown: BotPhase?
    @State private var shownAt = Date.distantPast
    @State private var appeared = false

    private var phase: BotPhase { shown ?? activity.phase }
    private var writing: Bool { phase == .writing && !reduceMotion && !activity.reconnecting }

    var body: some View {
        HStack(spacing: 8) {
            ZStack {
                if writing {
                    TypingDots().transition(.identity)
                } else {
                    BotAvatar(bot: bot, size: 20, phase: activity.reconnecting ? nil : phase)
                        // Reduce Motion keeps the character still, so the green dot shows the work.
                        .overlay(alignment: .bottomTrailing) {
                            if reduceMotion { BotActivityDot(bot: bot).padding(20 * BotAvatar.scale * 0.12) }
                        }
                        .transition(.scale(scale: 0.3).combined(with: .opacity))
                }
            }
            .frame(width: 26, height: 26)
            .scaleEffect(appeared || reduceMotion ? 1 : 0.2)
            .opacity(appeared || reduceMotion ? 1 : 0)
            TimelineView(.periodic(from: .now, by: 15)) { timeline in
                let label = label(at: timeline.date)
                ZStack(alignment: .leading) {
                    ShimmerText(text: label, active: !activity.reconnecting)
                        .id(label)
                        .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 4)),
                                                removal: .opacity.combined(with: .offset(y: -4))))
                }
                .font(.subheadline)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: label)
            }
        }
        .animation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.82), value: writing)
        .frame(minHeight: 32, alignment: .leading)
        .padding(.leading, 4)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(bot.name): \(label(at: .now))")
        .accessibilityIdentifier("bot-working-\(bot.id)")
        .onAppear {
            // The character grows in with a small overshoot.
            withAnimation(.spring(response: 0.35, dampingFraction: 0.62)) { appeared = true }
        }
        .task(id: activity.phase) {
            // Hold each label long enough to read, so fast tool calls do not flicker.
            let wait = Self.minimumDwell - Date().timeIntervalSince(shownAt)
            if shown != nil, wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
            guard !Task.isCancelled, shown != activity.phase else { return }
            withAnimation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.82)) { shown = activity.phase }
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

// The character collapses into the middle dot, and the side dots grow out of it.
struct TypingDots: View {
    @State private var spread = 0.0
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: scenePhase != .active)) { timeline in
            let time = timeline.date.timeIntervalSinceReferenceDate
            HStack(spacing: 3 * spread) {
                ForEach(0..<3) { index in
                    let level = Self.level(index: index, time: time)
                    Circle()
                        .fill(Color.primary.opacity(0.35 + 0.65 * level))
                        .frame(width: index == 1 ? 7 : 5 * spread + 0.01, height: index == 1 ? 7 : 5 * spread + 0.01)
                        .scaleEffect(1 + 0.18 * level)
                }
            }
        }
        .onAppear { withAnimation(.spring(response: 0.25, dampingFraction: 0.7)) { spread = 1 } }
        .accessibilityHidden(true)
    }

    // A wave crosses the three dots in 0.6 s, then rests for 0.4 s.
    static func level(index: Int, time: Double) -> Double {
        let phase = time.truncatingRemainder(dividingBy: 1.0) - Double(index) * 0.2
        guard phase >= 0, phase < 0.4 else { return 0 }
        return sin(.pi * phase / 0.4)
    }
}
