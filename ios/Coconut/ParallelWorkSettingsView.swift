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
}
