import SwiftUI

// Each wait in Coconut shows the coconut, never a system spinner (the person, 2026-11-25: "add way more personality
// and brand enforcement to coconut 3 dots"). The three holes of the mark juggle on a page and in the working line of a
// chat, and hop in a row inside a row or a button. The ProgressView at the end of this file keeps it so: a system
// spinner no longer builds. The motion is the one of the Claude Design canvas "Coconut loaders" of 2026-11-25: Cascade,
// Toss and settle, Fumble and Columns, with one coconut as the unit of length.
struct CoconutLoader: View {
    enum Place {
        /// A page or a panel that loads: the holes juggle in a box 44 pt tall.
        case page
        /// A row, a cell or a small control: the holes hop in a row 16 pt tall.
        case row
        /// The label of a button that works, in the color of the label: the holes hop in a row 18 pt tall.
        case button
        /// The working line of a chat: the holes juggle at the size of its text. The line itself speaks to VoiceOver.
        case typing
    }

    let place: Place
    let label: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var typingCoconut: CGFloat = 5
    @State private var start = Date()

    /// `label` is what VoiceOver reads; nil leaves the wait to the text beside the loader.
    init(_ place: Place = .page, label: String? = "Loading") {
        self.place = place
        self.label = label
    }

    /// The line that the coconuts land on, from the top of a juggle, as a fraction of its height. In a sentence it
    /// sits on the baseline of the text.
    nonisolated static let handLine: CGFloat = Juggle.handLine

    /// The width of one coconut, in points.
    private var coconut: CGFloat {
        switch place {
        case .page: 9.65
        case .row: 4.08
        case .button: 4.6
        case .typing: typingCoconut
        }
    }

    private var juggles: Bool { place == .page || place == .typing }

    var body: some View {
        let coconut = coconut
        let box = juggles ? Juggle.box : Columns.box
        // No pause for the scene phase: a share extension has no SwiftUI scene, and nothing draws in the background.
        TimelineView(.animation(minimumInterval: reduceMotion ? 1.0 / 30 : 1.0 / 60)) { timeline in
            let time = timeline.date.timeIntervalSince(start)
            let holes = holes(at: time)
            Canvas { context, _ in
                for (index, hole) in holes.enumerated() {
                    // Reduce Motion keeps the holes still: a wave crosses them, as it crossed the old three dots.
                    if reduceMotion { context.opacity = 0.35 + 0.65 * Self.wave(index, time) }
                    let rect = CGRect(x: (hole.x - 0.5) * coconut, y: (hole.y - 0.5) * coconut,
                                      width: coconut, height: coconut)
                    context.fill(Path(ellipseIn: rect), with: .foreground)
                }
            }
        }
        .frame(width: box.width * coconut, height: box.height * coconut)
        // A dropped coconut leaves the box, and a new one falls into it from the top.
        .clipped()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label ?? "")
        .accessibilityHidden(label == nil || place == .typing)
    }

    /// The centers of the three holes at `time` after the loader showed, in coconuts from the top left of its box.
    private func holes(at time: Double) -> [CGPoint] {
        switch (juggles, reduceMotion) {
        case (true, false): Juggle.holes(at: time)
        case (true, true): Juggle.mark
        case (false, false): Columns.holes(at: time)
        case (false, true): Columns.still
        }
    }

    /// A wave crosses the three holes in 0.6 s, then rests for 0.4 s.
    private static func wave(_ index: Int, _ time: Double) -> Double {
        let phase = time.truncatingRemainder(dividingBy: 1) - Double(index) * 0.2
        guard phase >= 0, phase < 0.4 else { return 0 }
        return sin(.pi * phase / 0.4)
    }
}

/// The juggle. The coconut shows as its mark, hops, and tosses its three holes into a cascade: two hands at the bottom
/// throw in turn, a hole every 0.28 s, and each hole crosses to the other hand. Once in a long wait a throw goes wide:
/// the hole flies out of the box, and a new coconut falls in from the top and joins the cascade.
private enum Juggle {
    /// The box, in coconuts. It holds the whole cascade.
    static let box = CGSize(width: 4.2, height: 4.56)
    /// The middle of the two hands, from the top left of the box.
    static let hand = CGPoint(x: 2.1, y: 3.5)
    static let handLine = (hand.y + 0.5) / box.height
    /// The mark at rest, with the proportions of the app icon: two eyes above, the mouth below.
    static let mark = [CGPoint(x: 1.355, y: 1.635), CGPoint(x: 2.845, y: 1.635), CGPoint(x: 2.1, y: 2.925)]
    /// The time a hole takes to go round both hands, in seconds. The three holes are a third of it apart.
    static let period = 1.68
    /// Where the left eye, the right eye and the mouth join the cascade, in parts of a period.
    static let phases = [2.0 / 3, 0, 1.0 / 3]
    /// The canvas starts its toss 0.35 s in, so the mark hops at once.
    static let lead = 0.35
    /// The toss of Toss and settle, 4.2 s long, gives way to the cascade at 2 s, where both show the same holes.
    static let settle = 4.2, toss = 2.0
    /// A throw that goes wide takes three periods. The first comes after 20 s of waiting, then one every 30 s.
    static let fumble = 3 * period, firstFumble = 4, fumbleEvery = 6

    static func holes(at time: Double) -> [CGPoint] {
        let t = lead + time
        if t < toss {
            let f = t / settle
            return [(s1x, s1y), (s2x, s2y), (s3x, s3y)].map { x, y in at(x.value(f), y.value(f)) }
        }
        let round = Int(t / fumble)
        let fumbles = round >= firstFumble && (round - firstFumble) % fumbleEvery == 0
        return phases.enumerated().map { index, phase in
            if index == 0, fumbles {
                let f = t.truncatingRemainder(dividingBy: fumble) / fumble
                return at(fumbleX.value(f), fumbleY.value(f))
            }
            let f = t / period + phase
            return at(x.value(f), y.value(f))
        }
    }

    private static func at(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: hand.x + x, y: hand.y + y) }

    // The keyframes of the canvas, in coconuts from the hand. A negative y is up.
    static let x = Track([
        .init(0, 0.25), .init(0.4, -1.6, 0, 0.8858), .init(0.5, -0.25), .init(0.9, 1.6, 0, 0.8858), .init(1, 0.25),
    ])
    static let y = Track([
        .init(0, 0, 0.6667, 1), .init(0.2, -3, 0, 0.3333), .init(0.4, 0, 0.4694, 1),
        .init(0.45863, 0.5621, 0, 0.2641), .init(0.5, 0, 0.6667, 1), .init(0.7, -3, 0, 0.3333),
        .init(0.9, 0, 0.4694, 1), .init(0.95863, 0.5621, 0, 0.2641), .init(1, 0),
    ])
    static let s1x = Track([
        .init(0, -0.745), .init(0.14079, -0.745, 0, 1), .init(0.15635, -0.7221, 0, 0.25), .init(0.16413, -0.745),
        .init(0.29333, -1.6, 0, 0.8858), .init(0.33333, -0.25), .init(0.49333, 1.6, 0, 0.8858), .init(0.53333, 0.25),
        .init(0.69333, -1.6, 0, 0.8442), .init(0.73333, -0.25), .init(0.79641, 0.745, 0.75, 1),
        .init(0.80752, 0.7839, 0, 1), .init(0.82974, 0.745), .init(1, 0.745),
    ])
    static let s1y = Track([
        .init(0, -1.865), .init(0.14079, -1.865, 0, 1), .init(0.15635, -1.7055, 0, 0.25),
        .init(0.16413, -1.865, 0.6667, 1), .init(0.21333, -3, 0, 0.3333), .init(0.29333, 0, 0.4694, 1),
        .init(0.31679, 0.5621, 0, 0.2641), .init(0.33333, 0, 0.6667, 1), .init(0.41333, -3, 0, 0.3333),
        .init(0.49333, 0, 0.4694, 1), .init(0.51679, 0.5621, 0, 0.2641), .init(0.53333, 0, 0.6667, 1),
        .init(0.61333, -3, 0, 0.3333), .init(0.69333, 0, 0.5376, 1), .init(0.71593, 0.4728, 0, 0.2744),
        .init(0.73333, 0, 0.6667, 1), .init(0.79641, -1.865), .init(1, -1.865),
    ])
    static let s2x = Track([
        .init(0, 0.745), .init(0.20746, 0.745, 0, 1), .init(0.22302, 0.7221, 0, 0.25), .init(0.23079, 0.745),
        .init(0.36, 1.6, 0, 0.8858), .init(0.4, 0.25), .init(0.56, -1.6, 0, 0.8858), .init(0.6, -0.25),
        .init(0.76, 1.6, 0, 0.8442), .init(0.8, 0.25), .init(0.86308, -0.745, 0.75, 1), .init(0.87419, -0.7839, 0, 1),
        .init(0.89641, -0.745), .init(1, -0.745),
    ])
    static let s2y = Track([
        .init(0, -1.865), .init(0.20746, -1.865, 0, 1), .init(0.22302, -1.7055, 0, 0.25),
        .init(0.23079, -1.865, 0.6667, 1), .init(0.28, -3, 0, 0.3333), .init(0.36, 0, 0.4694, 1),
        .init(0.38345, 0.5621, 0, 0.2641), .init(0.4, 0, 0.6667, 1), .init(0.48, -3, 0, 0.3333),
        .init(0.56, 0, 0.4694, 1), .init(0.58345, 0.5621, 0, 0.2641), .init(0.6, 0, 0.6667, 1),
        .init(0.68, -3, 0, 0.3333), .init(0.76, 0, 0.5376, 1), .init(0.78259, 0.4728, 0, 0.2744),
        .init(0.8, 0, 0.6667, 1), .init(0.86308, -1.865), .init(1, -1.865),
    ])
    static let s3x = Track([
        .init(0, 0), .init(0.20667, 0, 0, 1), .init(0.25504, 0.3129, 0, 0.2873), .init(0.26667, 0.25),
        .init(0.42667, -1.6, 0, 0.8858), .init(0.46667, -0.25), .init(0.62667, 1.6, 0, 0.8858), .init(0.66667, 0.25),
        .init(0.82667, -1.6, 0, 0.9295), .init(0.86667, -0.25), .init(0.90169, 0), .init(1, 0),
    ])
    static let s3y = Track([
        .init(0, -0.575), .init(0.20667, -0.575, 0, 1), .init(0.25074, 0.5445, 0, 0.2686), .init(0.26667, 0, 0.6667, 1),
        .init(0.34667, -3, 0, 0.3333), .init(0.42667, 0, 0.4694, 1), .init(0.45012, 0.5621, 0, 0.2641),
        .init(0.46667, 0, 0.6667, 1), .init(0.54667, -3, 0, 0.3333), .init(0.62667, 0, 0.4694, 1),
        .init(0.65012, 0.5621, 0, 0.2641), .init(0.66667, 0, 0.6667, 1), .init(0.74667, -3, 0, 0.3333),
        .init(0.82667, 0, 0.6712, 1), .init(0.84653, 0.3329, 0, 0.338), .init(0.86667, 0, 0.6667, 1),
        .init(0.90169, -0.575), .init(1, -0.575),
    ])
    static let fumbleX = Track([
        .init(0, 0.5208), .init(0.07778, 1.6, 0, 0.8858), .init(0.11111, 0.25), .init(0.24444, -1.6, 0, 0.8858),
        .init(0.27778, -0.25), .init(0.44628, 2.088, 0, 1), .init(0.5, 5.6), .init(0.55556, 5.6, 0, 1),
        .init(0.61111, 1.6), .init(0.74444, 1.6, 0, 0.8858), .init(0.77778, 0.25), .init(0.91111, -1.6, 0, 0.8858),
        .init(0.94444, -0.25), .init(1, 0.5208),
    ])
    static let fumbleY = Track([
        .init(0, -2.9167, -0.1333, 0.2), .init(0.07778, 0, 0.4694, 1), .init(0.09732, 0.5621, 0, 0.2641),
        .init(0.11111, 0, 0.6667, 1), .init(0.17778, -3, 0, 0.3333), .init(0.24444, 0, 0.4694, 1),
        .init(0.26399, 0.5621, 0, 0.2641), .init(0.27778, 0, -1.2638, -0.9304), .init(0.44628, 4), .init(0.5, 4, 0, 1),
        .init(0.55556, -6.4), .init(0.64707, -6.4, 0, 0.3333), .init(0.74444, 0, 0.5758, 1),
        .init(0.76278, 0.628, 0, 0.2836), .init(0.77778, 0, 0.6667, 1), .init(0.84444, -3, 0, 0.3333),
        .init(0.91111, 0, 0.4694, 1), .init(0.93066, 0.5621, 0, 0.2641), .init(0.94444, 0, 0.5714, 0.9048),
        .init(1, -2.9167),
    ])
}

/// Columns: the three holes stand in a row and hop in turn, the middle one against the two outside, each landing with
/// a small squash. A hop takes 0.76 s.
private enum Columns {
    static let box = CGSize(width: 3.98, height: 3.92)
    static let still = (0..<3).map { CGPoint(x: 0.5 + 1.49 * Double($0), y: box.height / 2) }

    static func holes(at time: Double) -> [CGPoint] {
        (0..<3).map { index in
            CGPoint(x: 0.5 + 1.49 * Double(index), y: 3.08 + y.value(time / 0.76 + (index == 1 ? 0.5 : 0)))
        }
    }

    static let y = Track([
        .init(0, 0, 0.6667, 1), .init(0.425, -2.58, 0, 0.3333), .init(0.85, 0, 0.4694, 1),
        .init(0.93795, 0.3412, 0, 0.2641), .init(1, 0),
    ])
}

/// The keyframes of one loop, as in the CSS of the canvas. Each key eases to the next one on a cubic Bézier whose
/// handles sit at a third and two thirds of the time, so the heights `y1` and `y2` of its handles are its whole curve.
private struct Track {
    struct Key {
        let at: Double, value: Double, y1: Double, y2: Double

        init(_ at: Double, _ value: Double, _ y1: Double = 1.0 / 3, _ y2: Double = 2.0 / 3) {
            (self.at, self.value, self.y1, self.y2) = (at, value, y1, y2)
        }
    }

    let keys: [Key]

    init(_ keys: [Key]) { self.keys = keys }

    /// The value at `fraction` of the loop. A fraction past one starts the loop again.
    func value(_ fraction: Double) -> Double {
        let f = fraction - fraction.rounded(.down)
        var index = 0
        while index < keys.count - 2, keys[index + 1].at <= f { index += 1 }
        let from = keys[index], to = keys[index + 1]
        let p = min(max((f - from.at) / (to.at - from.at), 0), 1), q = 1 - p
        return from.value + (to.value - from.value) * (3 * q * q * p * from.y1 + 3 * q * p * p * from.y2 + p * p * p)
    }
}

/// Coconut has no system spinner: this name hides the one of SwiftUI in the app, the Mac app and the share extensions.
@available(*, unavailable, message: "Coconut shows a CoconutLoader for each wait, never a system spinner.")
struct ProgressView {}
