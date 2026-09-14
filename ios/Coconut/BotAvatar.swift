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
    var isThinking = false
    var animated = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var blinking = false
    @State private var thinkingAmount = 0.0

    init(symbol: String, color: String, size: CGFloat = 52, identity: String = "", isThinking: Bool = false, animated: Bool = true) {
        self.symbol = symbol; self.color = color; self.size = size * Self.scale
        self.identity = identity; self.isThinking = isThinking; self.animated = animated
    }

    init(bot: Bot, size: CGFloat = 52, isThinking: Bool = false, animated: Bool = true) {
        self.init(symbol: bot.avatar, color: bot.color, size: size, identity: bot.id, isThinking: isThinking, animated: animated)
    }

    private var motionEnabled: Bool { animated && !reduceMotion && scenePhase == .active }
    private var seed: UInt64 { characterSeed(identity.isEmpty ? symbol + color : identity) }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !motionEnabled || !isThinking)) { timeline in
            FruitFigure(index: FruitArtwork.index(symbol: symbol, identity: identity),
                        size: size, seed: seed, time: timeline.date.timeIntervalSinceReferenceDate,
                        thinking: thinkingAmount, eyelid: blinking ? 0.08 : 1)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
        .onChange(of: isThinking, initial: true) { _, _ in updateThinking() }
        .onChange(of: motionEnabled) { _, _ in updateThinking() }
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

    private func updateThinking() {
        withAnimation(motionEnabled ? .easeOut(duration: 0.25) : nil) {
            thinkingAmount = isThinking && motionEnabled ? 1 : 0
        }
    }
}

// One live presentation drives pinned tiles, rows, and the conversation header.
struct LiveBotAvatar: View {
    @Environment(AppStore.self) private var store
    let bot: Bot
    var size: CGFloat = 52
    var body: some View {
        BotAvatar(bot: bot, size: size, isThinking: store.isBotActive(bot))
            .overlay(alignment: .bottomTrailing) { BotActivityDot(bot: bot).padding(size * BotAvatar.scale * 0.12) }
    }
}

// Every idle bot shares this three-quarter pose. Thinking changes expression,
// then holds it long enough to read at contact-row size.
struct CoconutExpression: Equatable {
    var yaw = 0.8
    var pitch = -0.35
    var leftEye = 1.0
    var rightEye = 1.0
    var eyeTilt = -20.0
    var mouthWidth = 15.0
    var smile = 4.5
    var mouthOpen = 0.0
    var roll = -3.0

    static let resting = Self()
    private static let thoughts = [
        Self(yaw: 0.65, pitch: -0.8, leftEye: 1.05, rightEye: 0.85, eyeTilt: -24, mouthWidth: 11, smile: 1, mouthOpen: 0, roll: -5),
        Self(yaw: -0.55, pitch: 0.35, leftEye: 0.55, rightEye: 0.9, eyeTilt: 10, mouthWidth: 14, smile: -1, mouthOpen: 0, roll: 3),
        Self(yaw: -0.15, pitch: -0.65, leftEye: 1.2, rightEye: 1.15, eyeTilt: -5, mouthWidth: 6, smile: 0, mouthOpen: 5, roll: 1),
        Self(yaw: 0.85, pitch: -0.15, leftEye: 0.7, rightEye: 0.32, eyeTilt: -24, mouthWidth: 12, smile: 2, mouthOpen: 0, roll: -4),
        Self(yaw: 0.1, pitch: 0.15, leftEye: 0.95, rightEye: 1.05, eyeTilt: 5, mouthWidth: 17, smile: 6, mouthOpen: 0, roll: 2),
        Self(yaw: 0.6, pitch: -0.7, leftEye: 0.75, rightEye: 1.05, eyeTilt: -18, mouthWidth: 9, smile: 0, mouthOpen: 2.5, roll: -2),
        resting
    ]

    static func sample(time: Double, seed: UInt64, thinking: Double) -> Self {
        guard thinking > 0 else { return resting }
        let duration = 1.8
        let cycle = duration * Double(thoughts.count)
        let phase = (time + Double(seed % 1000) / 97).truncatingRemainder(dividingBy: cycle)
        let position = (phase + cycle).truncatingRemainder(dividingBy: cycle) / duration
        let index = Int(position)
        let transition = min(1, max(0, (position - Double(index) - 0.6) / 0.4))
        let eased = transition * transition * (3 - 2 * transition)
        let expression = thoughts[index].mixed(with: thoughts[(index + 1) % thoughts.count], amount: eased)
        return resting.mixed(with: expression, amount: min(1, thinking))
    }

    private func mixed(with other: Self, amount: Double) -> Self {
        func mix(_ a: Double, _ b: Double) -> Double { a + (b - a) * amount }
        return Self(yaw: mix(yaw, other.yaw), pitch: mix(pitch, other.pitch),
                    leftEye: mix(leftEye, other.leftEye), rightEye: mix(rightEye, other.rightEye),
                    eyeTilt: mix(eyeTilt, other.eyeTilt), mouthWidth: mix(mouthWidth, other.mouthWidth),
                    smile: mix(smile, other.smile), mouthOpen: mix(mouthOpen, other.mouthOpen), roll: mix(roll, other.roll))
    }
}
