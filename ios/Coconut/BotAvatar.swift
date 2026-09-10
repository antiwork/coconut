import SwiftUI

// Appearance stays in the bot's existing avatar and color fields.
enum CoconutShape: String, CaseIterable {
    case round, oval, wide, square, pointed, bean

    var avatar: String { "coconut.\(rawValue)" }
    var label: String { rawValue.capitalized }

    static func resolve(_ avatar: String, identity: String) -> Self {
        allCases.first { $0.avatar == avatar }
            ?? allCases[Int(characterSeed(identity.isEmpty ? avatar : identity) % UInt64(allCases.count))]
    }
}

struct CoconutColor {
    let name: String
    let hex: String
    static let palette = [
        Self(name: "Coconut", hex: "A85F32"), Self(name: "Coral", hex: "C76552"),
        Self(name: "Gold", hex: "B5913F"), Self(name: "Green", hex: "64866A"),
        Self(name: "Teal", hex: "448A89"), Self(name: "Blue", hex: "6087B0"),
        Self(name: "Purple", hex: "9272B3"), Self(name: "Pink", hex: "BF759B"),
        Self(name: "Cream", hex: "D6C6A2"), Self(name: "Gray", hex: "858680")
    ]
}

// Swift's Hasher changes between launches. Character details must not.
func characterSeed(_ value: String) -> UInt64 {
    value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
}

struct BotAvatar: View {
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
        self.symbol = symbol; self.color = color; self.size = size
        self.identity = identity; self.isThinking = isThinking; self.animated = animated
    }

    init(bot: Bot, size: CGFloat = 52, isThinking: Bool = false, animated: Bool = true) {
        self.init(symbol: bot.avatar, color: bot.color, size: size, identity: bot.id, isThinking: isThinking, animated: animated)
    }

    private var motionEnabled: Bool { animated && !reduceMotion && scenePhase == .active }
    private var seed: UInt64 { characterSeed(identity.isEmpty ? symbol + color : identity) }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !motionEnabled || !isThinking)) { timeline in
            CoconutFigure(shape: CoconutShape.resolve(symbol, identity: identity), color: color, size: size,
                          seed: seed, time: timeline.date.timeIntervalSinceReferenceDate,
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
            .overlay(alignment: .bottomTrailing) { BotActivityDot(bot: bot).padding(size * 0.12) }
    }
}

private struct CoconutFigure: View, Animatable {
    let shape: CoconutShape
    let color: String
    let size: CGFloat
    let seed: UInt64
    let time: Double
    var thinking: Double
    var eyelid: Double
    nonisolated var animatableData: AnimatablePair<Double, Double> {
        get { AnimatablePair(thinking, eyelid) }
        set { thinking = newValue.first; eyelid = newValue.second }
    }

    var body: some View {
        let phase = time * .pi / 2.8 + Double(seed % 1000) / 1000 * .pi * 2
        let turn = sin(phase) * thinking
        ZStack {
            CoconutShell(shape: shape, color: color, seed: seed)
            CoconutFace(color: color, eyelid: eyelid,
                        lookX: turn * 0.065, lookY: -thinking * (0.025 + 0.035 * cos(phase * 2)))
        }
        .frame(width: size, height: size)
        .rotationEffect(.degrees(turn * 4))
        .scaleEffect(x: 1 + 0.025 * thinking * sin(phase * 2), y: 1 - 0.025 * thinking * sin(phase * 2))
    }
}

private struct CoconutShell: View {
    let shape: CoconutShape
    let color: String
    let seed: UInt64

    var body: some View {
        Canvas { context, size in
            let unit = min(size.width, size.height)
            context.scaleBy(x: unit / 100, y: unit / 100)
            let outline = shellPath()
            context.fill(outline, with: .color(Color(hex: color)))
            var grain = context
            grain.clip(to: outline)
            // Uneven pencil strokes stay fixed to the shell as it moves.
            for i in 0..<22 {
                let x = 11 + Double(i) * 3.8
                let bend = noise(i) * 9
                var line = Path()
                line.move(to: CGPoint(x: x + 15, y: 7))
                line.addCurve(to: CGPoint(x: x - 12, y: 96),
                              control1: CGPoint(x: x - 24 + bend, y: 35),
                              control2: CGPoint(x: x + 25, y: 65))
                grain.stroke(line, with: .color(i.isMultiple(of: 3) ? .white.opacity(0.14) : .black.opacity(0.16)),
                             style: StrokeStyle(lineWidth: 0.65 + noise(i + 30) * 1.15, lineCap: .round))
            }
            if unit >= 40 {
                for i in 0..<100 {
                    let x = noise(i + 100) * 100, y = noise(i + 300) * 100
                    var fleck = Path()
                    fleck.move(to: CGPoint(x: x, y: y))
                    fleck.addLine(to: CGPoint(x: x + 0.6, y: y - 1.2))
                    grain.stroke(fleck, with: .color(.white.opacity(0.14)), lineWidth: 0.55)
                }
            }
            context.stroke(outline, with: .color(.black.opacity(0.28)), style: StrokeStyle(lineWidth: 1.4, lineJoin: .round))
            for i in 0..<9 {
                let angle = Double(i) * .pi * 2 / 9 + 0.12
                let root = point(angle)
                var fiber = Path()
                fiber.move(to: CGPoint(x: root.x * 0.96 + 2, y: root.y * 0.96 + 2))
                fiber.addQuadCurve(to: CGPoint(x: root.x + cos(angle + 0.4) * 6, y: root.y + sin(angle + 0.4) * 6),
                                   control: CGPoint(x: root.x + cos(angle) * 4, y: root.y + sin(angle) * 2))
                context.stroke(fiber, with: .color(Color(hex: color)), style: StrokeStyle(lineWidth: 1.2, lineCap: .round))
            }
        }
    }

    private func noise(_ index: Int) -> Double { Double(characterSeed("\(seed)/\(index)") % 1000) / 1000 }

    private func point(_ angle: Double) -> CGPoint {
        let x = cos(angle), y = sin(angle)
        let wobble = 1 + 0.018 * sin(angle * 7 + 1) + 0.012 * cos(angle * 11)
        let radius: Double = 37 * wobble
        switch shape {
        case .round: return CGPoint(x: 50 + radius * x, y: 51 + radius * y)
        case .oval: return CGPoint(x: 50 + radius * x * 0.84, y: 51 + radius * y * 1.08)
        case .wide: return CGPoint(x: 50 + radius * x * 1.1, y: 53 + radius * y * 0.8)
        case .square:
            let r = radius / pow(pow(abs(x), 3.6) + pow(abs(y), 3.6), 1 / 3.6)
            return CGPoint(x: 50 + r * x * 0.91, y: 51 + r * y * 0.91)
        case .pointed: return CGPoint(x: 50 + radius * x * (0.88 + y * 0.19), y: 50 + radius * y * 1.06)
        case .bean: return CGPoint(x: 50 + radius * x * (0.97 + 0.1 * sin(angle * 2)), y: 51 + radius * y * 0.92)
        }
    }

    private func shellPath() -> Path {
        Path { path in
            path.move(to: point(0))
            for i in 1...128 { path.addLine(to: point(Double(i) / 128 * .pi * 2)) }
            path.closeSubpath()
        }
    }
}

private struct CoconutFace: View {
    let color: String
    let eyelid: Double
    let lookX: Double
    let lookY: Double

    private var ink: Color {
        let value = UInt64(color.trimmingCharacters(in: CharacterSet.alphanumerics.inverted), radix: 16) ?? 0
        let luminance = Double((value >> 16) & 255) * 0.2126 + Double((value >> 8) & 255) * 0.7152 + Double(value & 255) * 0.0722
        return luminance > 170 ? Color(red: 0.22, green: 0.15, blue: 0.1) : Color(red: 1, green: 0.97, blue: 0.88)
    }

    var body: some View {
        Canvas { context, size in
            context.scaleBy(x: size.width / 100, y: size.height / 100)
            context.translateBy(x: lookX * 100, y: lookY * 100)
            for x in [39.0, 60.0] {
                var eye = context
                eye.translateBy(x: x, y: 45)
                eye.rotate(by: .degrees(13))
                let rect = CGRect(x: -3.6, y: -6.2 * eyelid, width: 7.2, height: 12.4 * eyelid)
                eye.fill(Path(ellipseIn: rect), with: .color(ink))
            }
            var smile = Path()
            smile.move(to: CGPoint(x: 41, y: 61))
            smile.addQuadCurve(to: CGPoint(x: 59, y: 62), control: CGPoint(x: 49, y: 68))
            context.stroke(smile, with: .color(ink), style: StrokeStyle(lineWidth: 3.1, lineCap: .round))
        }
    }
}
