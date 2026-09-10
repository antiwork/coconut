import SwiftUI

// Appearance stays in the bot's existing avatar and color fields.
enum CoconutShape: String, CaseIterable {
    case round, oval, square, wide, triangle, hexagon, bean, pointed

    var avatar: String { "coconut.\(rawValue)" }
    var label: String {
        switch self {
        case .round: "Circle"
        case .oval: "Pebble"
        case .square: "Squircle"
        case .wide: "Capsule"
        case .triangle: "Triangle"
        case .hexagon: "Hexagon"
        case .bean: "Cloud"
        case .pointed: "Droplet"
        }
    }

    static func resolve(_ avatar: String, identity: String) -> Self {
        // Keep earlier generated assignments stable when the picker gains shapes.
        let original: [Self] = [.round, .oval, .wide, .square, .pointed, .bean]
        return allCases.first { $0.avatar == avatar }
            ?? original[Int(characterSeed(identity.isEmpty ? avatar : identity) % UInt64(original.count))]
    }

    var outline: Path { Self.outlines[self]! }

    // Narrow silhouettes need space for the full range of gaze and mouth poses.
    var facePlacement: (scale: CGFloat, x: CGFloat, y: CGFloat) {
        switch self {
        case .pointed: (0.85, -3, 8)
        case .triangle: (0.9, -2, 4)
        case .wide: (0.92, 0, 2.5)
        case .bean: (0.94, 0, 2)
        default: (1, 0, 0)
        }
    }

    // Bloub's 64 radial samples and closed Catmull-Rom curves, at Coconut's scale.
    // Copyright (c) 2026 Jérémy Perret. See Resources/Bloub-LICENSE.txt.
    private static let outlines: [Self: Path] = BotShapeProfiles.radii.mapValues { radii in
        let points = radii.enumerated().map { index, radius in
            let angle = Double(index) / Double(radii.count) * .pi * 2
            return CGPoint(x: 50 + 37 * radius * cos(angle), y: 51 + 37 * radius * sin(angle))
        }
        return Path { path in
            path.move(to: points[0])
            for i in points.indices {
                let p0 = points[(i - 1 + points.count) % points.count]
                let p1 = points[i]
                let p2 = points[(i + 1) % points.count]
                let p3 = points[(i + 2) % points.count]
                path.addCurve(to: p2,
                              control1: CGPoint(x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6),
                              control2: CGPoint(x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6))
            }
            path.closeSubpath()
        }
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

    private static let displayPalette = [
            "A85F32": "CE7A3C", "C76552": "F56B5D", "B5913F": "F2BB3F",
            "64866A": "3ECF8E", "448A89": "24C8B5", "6087B0": "4395F7",
            "9272B3": "A06AF4", "BF759B": "EE66B5", "D6C6A2": "F1E3BE",
            "858680": "A3AAA8"
    ]

    // Stored palette values keep their identity when the visual palette changes.
    static func displayHex(_ value: String) -> String {
        let hex = value.trimmingCharacters(in: CharacterSet.alphanumerics.inverted).uppercased()
        return displayPalette[hex] ?? hex
    }
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
            CoconutFigure(shape: CoconutShape.resolve(symbol, identity: identity), color: CoconutColor.displayHex(color), size: size,
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
        let expression = CoconutExpression.sample(time: time, seed: seed, thinking: thinking)
        ZStack {
            CoconutShell(shape: shape, color: color, seed: seed)
            CoconutFace(shape: shape, color: color, eyelid: eyelid, expression: expression)
        }
        .frame(width: size, height: size)
        .rotationEffect(.degrees(expression.roll))
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

private struct CoconutShell: View {
    let shape: CoconutShape
    let color: String
    let seed: UInt64

    var body: some View {
        Canvas { context, size in
            let unit = min(size.width, size.height)
            context.scaleBy(x: unit / 100, y: unit / 100)
            let outline = shape.outline
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
        }
    }

    private func noise(_ index: Int) -> Double {
        // Mix neighboring seeds and indices to vary the shell grain.
        var value = seed &+ UInt64(index) &* 0x9E3779B97F4A7C15
        value = (value ^ (value >> 30)) &* 0xBF58476D1CE4E5B9
        value = (value ^ (value >> 27)) &* 0x94D049BB133111EB
        value ^= value >> 31
        return Double(value >> 11) / 9_007_199_254_740_992
    }

}

private struct CoconutFace: View {
    let shape: CoconutShape
    let color: String
    let eyelid: Double
    let expression: CoconutExpression

    private var ink: Color {
        let value = UInt64(color.trimmingCharacters(in: CharacterSet.alphanumerics.inverted), radix: 16) ?? 0
        let luminance = Double((value >> 16) & 255) * 0.2126 + Double((value >> 8) & 255) * 0.7152 + Double(value & 255) * 0.0722
        return luminance > 170 ? Color(red: 0.22, green: 0.15, blue: 0.1) : Color(red: 1, green: 0.97, blue: 0.88)
    }

    var body: some View {
        Canvas { context, size in
            context.scaleBy(x: size.width / 100, y: size.height / 100)
            let placement = shape.facePlacement
            context.translateBy(x: 50 * (1 - placement.scale) + placement.x,
                                y: 50 * (1 - placement.scale) + placement.y)
            context.scaleBy(x: placement.scale, y: placement.scale)
            let yaw = expression.yaw
            let pitch = expression.pitch
            let faceX = 50 + yaw * 13
            let faceY = 43 + pitch * 12
            for side in [-1.0, 1.0] {
                let farSide = max(0, side * yaw)
                let openness = (side < 0 ? expression.leftEye : expression.rightEye) * eyelid
                let width = 7.2 * (1 - farSide * 0.22)
                let height = 12.4 * openness * (1 - farSide * 0.12)
                var eye = context
                eye.translateBy(x: faceX + side * 10 * (1 - abs(yaw) * 0.18),
                                y: faceY - side * yaw * 1.8)
                eye.rotate(by: .degrees(expression.eyeTilt))
                eye.fill(Path(ellipseIn: CGRect(x: -width / 2, y: -height / 2, width: width, height: height)), with: .color(ink))
            }
            var mouth = context
            mouth.translateBy(x: faceX, y: 57 + pitch * 7)
            mouth.rotate(by: .degrees(-yaw * 9))
            let halfWidth = expression.mouthWidth / 2
            var lip = Path()
            lip.move(to: CGPoint(x: -halfWidth, y: 0))
            lip.addQuadCurve(to: CGPoint(x: halfWidth, y: 0), control: CGPoint(x: 0, y: expression.smile + expression.mouthOpen))
            if expression.mouthOpen > 0.05 {
                lip.addQuadCurve(to: CGPoint(x: -halfWidth, y: 0),
                                 control: CGPoint(x: 0, y: expression.smile - expression.mouthOpen))
                lip.closeSubpath()
                mouth.fill(lip, with: .color(ink))
            }
            mouth.stroke(lip, with: .color(ink), style: StrokeStyle(lineWidth: 2.7, lineCap: .round, lineJoin: .round))
        }
    }
}
