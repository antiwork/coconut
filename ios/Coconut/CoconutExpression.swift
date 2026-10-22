import Foundation

// Each working phase has its own gesture. The face acts as a sphere: the eyes and mouth
// sit on its surface, so a gaze change moves them across the fruit and foreshortens them.
enum CoconutGesture: Sendable {
    case ponder, scan, focus, dart, recall, spin, write
}

// Every idle bot shares this raised, right-facing pose. Yaw and pitch are in radians.
struct CoconutExpression: Equatable {
    var yaw = 0.16
    var pitch = -0.08
    var eyeWidth = 1.0
    var eyeHeight = 1.0
    var eyeTilt = -12.0
    var mouthWidth = 13.0
    var smile = 4.5
    var mouthOpen = 0.0
    // Movement energy from 0 to 1. The figure squashes a little while its gaze travels.
    var squash = 0.0
    var roll = -2.0

    static let resting = Self()
    static let transition = 0.5

    private struct Beat {
        var pose: CoconutExpression
        var move: Double
        var hold: Double
    }

    private static func pose(yaw: Double, pitch: Double, eyeWidth: Double = 1, eyeHeight: Double = 1, eyeTilt: Double = -12,
                             mouthWidth: Double = 11, smile: Double = 2, mouthOpen: Double = 0, roll: Double = 0) -> Self {
        Self(yaw: yaw, pitch: pitch, eyeWidth: eyeWidth, eyeHeight: eyeHeight, eyeTilt: eyeTilt,
             mouthWidth: mouthWidth, smile: smile, mouthOpen: mouthOpen, roll: roll)
    }

    // Quick saccades with readable holds.
    // Built once. Sampling runs on every frame of every working avatar.
    private static let tables: [CoconutGesture: (beats: [Beat], cycle: Double)] = Dictionary(uniqueKeysWithValues:
        [CoconutGesture.ponder, .scan, .focus, .dart, .recall, .write].map { gesture in
            let beats = beats(gesture)
            return (gesture, (beats, beats.reduce(0) { $0 + $1.move + $1.hold }))
        })

    private static func beats(_ gesture: CoconutGesture) -> [Beat] {
        switch gesture {
        case .ponder: [
            Beat(pose: pose(yaw: -0.5, pitch: -0.42, eyeHeight: 0.9, eyeTilt: -6, mouthWidth: 9, smile: 1, roll: -4), move: 0.42, hold: 0.95),
            Beat(pose: pose(yaw: 0.55, pitch: -0.5, eyeTilt: -18, mouthWidth: 8, smile: 0, roll: 3), move: 0.38, hold: 0.75),
            Beat(pose: pose(yaw: 0.05, pitch: 0.08, eyeHeight: 0.62, mouthWidth: 12), move: 0.3, hold: 0.55),
            Beat(pose: pose(yaw: -0.25, pitch: -0.6, eyeHeight: 1.12, mouthWidth: 6, smile: 0, mouthOpen: 3, roll: -2), move: 0.4, hold: 1.0)]
        case .scan: [-0.42, -0.14, 0.14, 0.42].enumerated().map { index, yaw in
            Beat(pose: pose(yaw: yaw, pitch: 0.28, eyeHeight: 0.82, mouthWidth: 9, smile: 1),
                 move: index == 0 ? 0.28 : 0.15, hold: index == 3 ? 0.36 : 0.26) }
        case .focus: [
            Beat(pose: pose(yaw: -0.18, pitch: 0.22, eyeHeight: 0.6, eyeTilt: -4, mouthWidth: 9, smile: 0), move: 0.24, hold: 0.7),
            Beat(pose: pose(yaw: 0.14, pitch: 0.22, eyeHeight: 0.6, eyeTilt: -4, mouthWidth: 9, smile: 0), move: 0.22, hold: 0.6),
            Beat(pose: pose(yaw: 0, pitch: 0.34, eyeHeight: 0.55, eyeTilt: -4, mouthWidth: 8, smile: 0), move: 0.2, hold: 0.5),
            Beat(pose: pose(yaw: -0.05, pitch: 0.18, eyeHeight: 0.68, eyeTilt: -4, mouthWidth: 9, smile: 1), move: 0.18, hold: 0.55)]
        case .dart: [(0.55, -0.38), (-0.5, -0.3), (-0.35, 0.3), (0.45, 0.28), (0, -0.1)].map { yaw, pitch in
            Beat(pose: pose(yaw: yaw, pitch: pitch, eyeWidth: 1.05, eyeHeight: 1.18, mouthWidth: 7, smile: 0, mouthOpen: 2),
                 move: 0.16, hold: 0.36) }
        case .recall: [
            Beat(pose: pose(yaw: -0.32, pitch: -0.62, eyeHeight: 0.95, mouthWidth: 8, smile: 1, roll: -3), move: 0.55, hold: 1.1),
            Beat(pose: pose(yaw: 0.3, pitch: -0.62, eyeHeight: 0.95, mouthWidth: 8, smile: 1, roll: 3), move: 0.7, hold: 0.9),
            Beat(pose: pose(yaw: 0.05, pitch: -0.4, eyeHeight: 0.7, mouthWidth: 10, smile: 2), move: 0.4, hold: 0.7)]
        case .write: [
            Beat(pose: pose(yaw: -0.12, pitch: 0.34, eyeHeight: 0.85, mouthWidth: 8, mouthOpen: 1.5), move: 0.25, hold: 0.32),
            Beat(pose: pose(yaw: 0.12, pitch: 0.34, eyeHeight: 0.85, mouthWidth: 8, mouthOpen: 1.5), move: 0.25, hold: 0.32)]
        case .spin: []
        }
    }

    static func sample(time: Double, seed: UInt64, thinking: Double, gesture: CoconutGesture = .ponder,
                       previous: CoconutGesture? = nil, changedAt: Double = -.infinity) -> Self {
        guard thinking > 0 else { return resting }
        let local = time + Double(seed % 1000) / 97
        var expression = pose(gesture, time: local)
        // A new phase passes through the resting face, so one gesture never cuts into the next.
        let progress = (time - changedAt) / transition
        if let previous, previous != gesture, progress < 1 {
            expression = progress < 0.5
                ? pose(previous, time: local).wrapped().mixed(with: resting, amount: ease(progress * 2))
                : resting.mixed(with: expression.wrapped(), amount: ease(progress * 2 - 1))
        }
        return resting.mixed(with: expression, amount: min(1, thinking))
    }

    private static func pose(_ gesture: CoconutGesture, time: Double) -> Self {
        if gesture == .spin {
            // One eased revolution, then a short hold that faces forward.
            let revolution = 1.25, hold = 0.45
            let phase = positive(time, revolution + hold)
            let progress = min(1, phase / revolution)
            var spin = pose(yaw: 0.1 + 2 * .pi * ease(progress), pitch: -0.15 + 0.1 * sin(2 * .pi * progress),
                            eyeWidth: 0.85, eyeHeight: 1.3, eyeTilt: -14, mouthWidth: 10)
            spin.squash = phase < revolution ? sin(.pi * progress) * 0.8 : 0
            return spin
        }
        guard let (beats, cycle) = tables[gesture] else { return resting }
        var remaining = positive(time, cycle)
        for (index, beat) in beats.enumerated() {
            if remaining < beat.move {
                let start = beats[(index + beats.count - 1) % beats.count].pose
                let progress = remaining / beat.move
                var moving = start.mixed(with: beat.pose, amount: settle(progress))
                let distance = abs(beat.pose.yaw - start.yaw) + abs(beat.pose.pitch - start.pitch)
                moving.squash = sin(.pi * progress) * min(1, distance * 1.5)
                return moving
            }
            remaining -= beat.move
            if remaining < beat.hold { return beat.pose }
            remaining -= beat.hold
        }
        return beats[0].pose
    }

    private static func positive(_ value: Double, _ cycle: Double) -> Double {
        (value.truncatingRemainder(dividingBy: cycle) + cycle).truncatingRemainder(dividingBy: cycle)
    }

    static func ease(_ value: Double) -> Double {
        let t = min(1, max(0, value))
        return t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2
    }

    // A fast start and a small overshoot, like an eye that lands on its target.
    static func settle(_ value: Double) -> Double {
        let t = min(1, max(0, value)) - 1
        return 1 + 2.2 * t * t * t + 1.2 * t * t
    }

    private func wrapped() -> Self {
        var copy = self
        copy.yaw = atan2(sin(yaw), cos(yaw))
        return copy
    }

    func mixed(with other: Self, amount: Double) -> Self {
        func mix(_ a: Double, _ b: Double) -> Double { a + (b - a) * amount }
        return Self(yaw: mix(yaw, other.yaw), pitch: mix(pitch, other.pitch),
                    eyeWidth: mix(eyeWidth, other.eyeWidth), eyeHeight: mix(eyeHeight, other.eyeHeight),
                    eyeTilt: mix(eyeTilt, other.eyeTilt), mouthWidth: mix(mouthWidth, other.mouthWidth),
                    smile: mix(smile, other.smile), mouthOpen: mix(mouthOpen, other.mouthOpen),
                    squash: mix(squash, other.squash), roll: mix(roll, other.roll))
    }
}
