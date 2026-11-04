import SwiftUI

// Swift's Hasher changes between launches. Character details must not.
func characterSeed(_ value: String) -> UInt64 {
    value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
}

struct BotAvatar: View {
    static let scale: CGFloat = 1.1
    let symbol: String
    let color: String
    var size: CGFloat = 52
    var identity = ""
    // A phase means the bot works. Nil keeps the idle pose.
    var phase: BotPhase? = nil
    /// The lasting mood. A finished turn and a new card show a short mood over it.
    var mood: CoconutMood = .neutral
    var needsYou = false
    var animated = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var blinking = false
    @State private var thinkingAmount = 0.0
    @State private var previousGesture: CoconutGesture?
    @State private var gestureChangedAt = -Double.infinity
    @State private var flash: CoconutMood?
    @State private var shownMood: CoconutMood?

    init(symbol: String, color: String, size: CGFloat = 52, identity: String = "", phase: BotPhase? = nil,
         mood: CoconutMood = .neutral, needsYou: Bool = false, animated: Bool = true) {
        self.symbol = symbol; self.color = color; self.size = size * Self.scale
        self.identity = identity; self.phase = phase; self.mood = mood; self.needsYou = needsYou; self.animated = animated
    }

    init(bot: Bot, size: CGFloat = 52, phase: BotPhase? = nil, mood: CoconutMood = .neutral, needsYou: Bool = false,
         animated: Bool = true) {
        self.init(symbol: bot.avatar, color: bot.color, size: size, identity: bot.id, phase: phase, mood: mood,
                  needsYou: needsYou, animated: animated)
    }

    private var isThinking: Bool { phase != nil }

    private var gesture: CoconutGesture { (phase ?? .thinking).gesture }

    private var motionEnabled: Bool { animated && !reduceMotion && scenePhase == .active }
    private var seed: UInt64 { characterSeed(identity.isEmpty ? symbol + color : identity) }
    private var targetMood: CoconutMood { flash ?? mood }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60, paused: !motionEnabled || !isThinking)) { timeline in
            FruitFigure(index: FruitArtwork.index(symbol: symbol, identity: identity),
                        size: size, seed: seed, time: timeline.date.timeIntervalSinceReferenceDate,
                        thinking: thinkingAmount, eyelid: blinking ? 0.08 : 1,
                        gesture: gesture, previousGesture: previousGesture, gestureChangedAt: gestureChangedAt,
                        mood: shownMood ?? targetMood)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
        .onChange(of: isThinking, initial: true) { _, _ in updateThinking() }
        .onChange(of: gesture) { old, _ in
            previousGesture = old
            gestureChangedAt = Date().timeIntervalSinceReferenceDate
        }
        .onChange(of: motionEnabled) { _, _ in updateThinking() }
        .onChange(of: phase) { old, new in if old != nil && new == nil { flash = .happy } }
        .onChange(of: needsYou) { old, new in if new && !old { flash = .surprised } }
        .task(id: flash) {
            guard flash != nil else { return }
            try? await Task.sleep(for: .seconds(3))
            if !Task.isCancelled { flash = nil }
        }
        .task(id: targetMood) { await showMood(targetMood) }
        .task(id: motionEnabled) {
            blinking = false
            guard motionEnabled else { return }
            var cycle: UInt64 = 0
            do {
                while !Task.isCancelled {
                    let delay = 5 + Double(characterSeed("\(seed)-\(cycle)") % 6000) / 1000
                    try await Task.sleep(for: .seconds(delay))
                    withAnimation(.easeIn(duration: 0.055)) { blinking = true }
                    try await Task.sleep(for: .milliseconds(95))
                    withAnimation(.easeOut(duration: 0.11)) { blinking = false }
                    cycle &+= 1
                }
            } catch { blinking = false }
        }
    }

    /// A new mood comes in a blink: the eyes close, the face changes, and the eyes open with a small overshoot.
    private func showMood(_ next: CoconutMood) async {
        guard let current = shownMood, current != next, motionEnabled else { shownMood = next; return }
        withAnimation(.easeIn(duration: 0.09)) { blinking = true }
        try? await Task.sleep(for: .milliseconds(110))
        shownMood = next
        withAnimation(.spring(response: 0.28, dampingFraction: 0.55)) { blinking = false }
    }

    private func updateThinking() {
        withAnimation(motionEnabled ? .easeOut(duration: 0.25) : nil) {
            thinkingAmount = isThinking && motionEnabled ? 1 : 0
        }
    }
}

// One live presentation drives pinned tiles, rows, and the conversation header.
struct LiveBotAvatar: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot
    var size: CGFloat = 52
    /// A provider problem makes the bot sad. A bot with no message for two days is sleepy.
    private var mood: CoconutMood {
        if bot.health != nil { return .sad }
        let latest = store.messages.lazy.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
        return Date().timeIntervalSince(latest ?? bot.createdAt.apiDate ?? .now) > 2 * 86_400 ? .sleepy : .neutral
    }
    var body: some View {
        BotAvatar(bot: bot, size: size, phase: store.botPhase(bot), mood: mood, needsYou: store.botNeedsYou(bot))
            .overlay(alignment: .bottomTrailing) {
                // The moving face shows the work. Reduce Motion keeps the face still, so the dot shows it.
                if reduceMotion { BotActivityDot(bot: bot).padding(size * BotAvatar.scale * 0.12) }
            }
    }
}

