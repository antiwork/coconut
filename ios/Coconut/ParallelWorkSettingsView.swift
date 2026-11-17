import Foundation

/// A limit of parallel work, as the Mac reports it. The Coconut chat changes it (coconut_settings).
struct ParallelWorkSettings: Codable, Equatable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

/// An agent's queue on the Hermes board of the Mac.
struct AgentQueue: Codable, Equatable, Sendable {
    var running = 0
    var waiting = 0
    var needsYou = 0
    var doneToday = 0

    var isEmpty: Bool { running + waiting + needsYou + doneToday == 0 }

    /// "1 running · 2 waiting · 1 needs you · 3 done today", without the empty lanes. A count stays with its words when
    /// the line breaks: only the space after a dot breaks.
    var summary: String {
        [(running, "running"), (waiting, "waiting"), (needsYou, "needs\u{00A0}you"), (doneToday, "done\u{00A0}today")]
            .filter { $0.0 > 0 }.map { "\($0.0)\u{00A0}\($0.1)" }.joined(separator: "\u{00A0}· ")
    }
}
