import SwiftUI

// Swift's Hasher changes between launches. Character details must not.
func characterSeed(_ value: String) -> UInt64 {
    value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
}

/// How an agent feels: a finished turn makes it happy, a new card surprises it, a provider problem makes it sad, and two
/// quiet days make it sleepy. The letter shows it with its posture.
enum CoconutMood: Sendable {
    case neutral, happy, surprised, sad, sleepy
}

/// The face of an agent: its letter on a rounded square in the color of the letter. People and teams have round faces
/// (LetterFaceView), so an agent reads as an agent. The letter bobs while the agent works and shows its mood.
struct BotAvatar: View {
    static let scale: CGFloat = 1.1
    let symbol: String
    let color: String
    var size: CGFloat = 52
    var identity = ""
    /// The name, for a face without its own letter.
    var name: String? = nil
    // A phase means the bot works. Nil keeps the idle pose.
    var phase: BotPhase? = nil
    /// The lasting mood. A finished turn and a new card show a short mood over it.
    var mood: CoconutMood = .neutral
    /// When the newest card that waits for an answer arrived. Only a later card surprises the bot, so an
    /// answered card that leaves an older one does not.
    var newestCardAt: Date? = nil
    /// A phase also ends when the connection drops. Only an online end is a finished turn.
    var online = true
    var animated = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var thinkingAmount = 0.0
    @State private var flash: Flash?

    // Each event has its own identity, so a repeated mood starts its time again.
    private struct Flash: Equatable {
        let mood: CoconutMood
        let id = UUID()
    }

    init(symbol: String, color: String, size: CGFloat = 52, identity: String = "", name: String? = nil,
         phase: BotPhase? = nil, mood: CoconutMood = .neutral, newestCardAt: Date? = nil, online: Bool = true,
         animated: Bool = true) {
        self.symbol = symbol; self.color = color; self.size = size * Self.scale
        self.identity = identity; self.name = name; self.phase = phase; self.mood = mood
        self.newestCardAt = newestCardAt; self.online = online; self.animated = animated
    }

    init(bot: Bot, size: CGFloat = 52, phase: BotPhase? = nil, mood: CoconutMood = .neutral, newestCardAt: Date? = nil,
         online: Bool = true, animated: Bool = true) {
        self.init(symbol: bot.avatar, color: bot.color, size: size, identity: bot.chatOf ?? bot.id, name: bot.name,
                  phase: phase, mood: mood, newestCardAt: newestCardAt, online: online, animated: animated)
    }

    var letter: String { AgentLetter.letter(symbol: symbol, name: name, identity: identity) }
    private var isThinking: Bool { phase != nil }
    private var motionEnabled: Bool { animated && !reduceMotion && scenePhase == .active }
    private var shownMood: CoconutMood { flash?.mood ?? mood }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !motionEnabled || !isThinking)) { timeline in
            LetterTile(letter: letter, size: size, mood: shownMood, thinking: thinkingAmount,
                       time: timeline.date.timeIntervalSinceReferenceDate,
                       phaseOffset: Double(characterSeed(identity.isEmpty ? symbol : identity) % 1000) / 1000)
        }
        .frame(width: size, height: size)
        .animation(motionEnabled ? .spring(response: 0.32, dampingFraction: 0.55) : nil, value: shownMood)
        .accessibilityHidden(true)
        .onChange(of: isThinking, initial: true) { _, _ in updateThinking() }
        .onChange(of: motionEnabled) { _, _ in updateThinking() }
        .onChange(of: phase) { old, new in if old != nil && new == nil && online { flash = Flash(mood: .happy) } }
        .onChange(of: newestCardAt) { old, new in
            if let new, new > (old ?? .distantPast) { flash = Flash(mood: .surprised) }
        }
        .task(id: flash) {
            guard flash != nil else { return }
            do {
                try await Task.sleep(for: .seconds(3))
                flash = nil
            } catch {}
        }
    }

    private func updateThinking() {
        withAnimation(motionEnabled ? .easeOut(duration: 0.25) : nil) {
            thinkingAmount = isThinking && motionEnabled ? 1 : 0
        }
    }
}

/// The drawn letter. A mood changes the posture: happy hops up, surprised grows, sad droops and pales, and sleepy
/// leans and fades. Working bobs the letter inside its tile.
struct LetterTile: View {
    let letter: String
    let size: CGFloat
    var mood: CoconutMood = .neutral
    var thinking = 0.0
    var time = 0.0
    var phaseOffset = 0.0

    var body: some View {
        let colors = AgentLetter.colors(letter)
        let bob = sin((time / 0.9 + phaseOffset) * 2 * .pi) * thinking
        RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
            .fill(Color(hex: colors.fill))
            .overlay {
                Text(letter)
                    .font(.system(size: size * 0.56, weight: .bold, design: .rounded))
                    .foregroundStyle(Color(hex: colors.ink))
                    .offset(y: -bob * size * 0.05 + (mood == .sad ? size * 0.04 : 0))
                    .scaleEffect(1 + abs(bob) * 0.04)
                    .rotationEffect(.degrees(mood == .sleepy ? -14 : mood == .sad ? 6 : 0))
            }
            .saturation(mood == .sad ? 0.45 : 1)
            .opacity(mood == .sleepy ? 0.7 : 1)
            .scaleEffect(mood == .surprised ? 1.1 : 1)
            .offset(y: mood == .happy ? -size * 0.06 : 0)
            .frame(width: size, height: size)
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
        let latest = store.chatSummaries[bot.id].newestDate
        return Date().timeIntervalSince(latest ?? bot.createdAt.apiDate ?? .now) > 2 * 86_400 ? .sleepy : .neutral
    }
    private var newestCardAt: Date? {
        store.decisions.filter { $0.isPending && store.conversationBotId(for: $0) == bot.id }
            .compactMap(\.createdAt.apiDate).max()
    }
    var body: some View {
        BotAvatar(bot: bot, size: size, phase: store.botPhase(bot), mood: mood, newestCardAt: newestCardAt,
                  online: store.isDemo || store.connection == .connected)
            .overlay(alignment: .bottomTrailing) {
                // The moving face shows the work. Reduce Motion keeps the face still, so the dot shows it.
                if reduceMotion { BotActivityDot(bot: bot).padding(size * BotAvatar.scale * 0.12) }
            }
    }
}
