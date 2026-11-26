import ActivityKit
import Foundation

/// The Live Activity of the work of the agents, on the Lock Screen and in the Dynamic Island, while an agent works or a
/// card waits for the person (WorkActivities in HomeScreenPublisher.swift starts, updates and ends it). One activity
/// holds every agent, not one for each chat: the person runs many agents at once, and a stack of activities would hide
/// the card that needs them.
struct CoconutWorkAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable, Sendable {
        /// The newest card that waits, and how many wait.
        var card: CoconutGlance.Card?
        var cards = 0
        /// The agents that work, the newest step first, and how many work.
        var working: [CoconutGlance.Work] = []
        var workingCount = 0
        /// When the app wrote this state. The app writes only while it runs, so an old state says when it is from.
        var updatedAt: Date
    }
}

extension CoconutWorkAttributes.ContentState {
    /// The agents that the activity names. The Lock Screen has room for three lines of work.
    static let shownAgents = 3
    /// The longest text of a line. An activity holds at most 4 KB, and a line shows less.
    static let lineLength = 120

    init(_ glance: CoconutGlance) {
        func line(_ text: String) -> String { text.count > Self.lineLength ? String(text.prefix(Self.lineLength - 1)) + "…" : text }
        card = glance.cards.first.map { card in
            var card = card
            card.title = line(card.title)
            card.name = line(card.name)
            return card
        }
        cards = glance.cards.count
        // A chat whose card waits shows as the card, not as work.
        let waiting = Set(glance.cards.map(\.chat))
        let work = glance.working.filter { !waiting.contains($0.id) }
        working = work.prefix(Self.shownAgents).map { work in
            var work = work
            work.name = line(work.name)
            work.detail = work.detail.map(line)
            return work
        }
        workingCount = work.count
        updatedAt = glance.updatedAt
    }

    /// Nothing works and no card waits: the activity ends.
    var isDone: Bool { card == nil && working.isEmpty }

    /// The place that a tap opens: the card, else the one agent that works, else every agent that works.
    var link: HomeScreenLink {
        if let card { return card.link }
        if workingCount == 1, let work = working.first { return work.link }
        return .working
    }

    /// "2 working · 1 needs you": what the header of the activity counts.
    var summary: String {
        var parts: [String] = []
        if workingCount > 0 { parts.append("\(workingCount) working") }
        if cards > 0 { parts.append(cards == 1 ? "1 needs you" : "\(cards) need you") }
        return parts.isEmpty ? "Nothing works" : parts.joined(separator: " · ")
    }
}
