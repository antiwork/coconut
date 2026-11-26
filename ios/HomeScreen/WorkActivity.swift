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
        /// The glance that the Mac sealed for this iPhone, in a state that came from the Mac through the relay after the
        /// person left the app (relay/src/apns.js). The other fields are then empty, and `shown` opens it.
        var sealed: SealedGlance? = nil
        /// True for a sealed state that did not open: the activity then asks to open Coconut. It is never sent.
        var unreadable = false

        private enum CodingKeys: String, CodingKey { case card, cards, working, workingCount, updatedAt, sealed }

        init(card: CoconutGlance.Card? = nil, cards: Int = 0, working: [CoconutGlance.Work] = [], workingCount: Int = 0,
             updatedAt: Date, sealed: SealedGlance? = nil, unreadable: Bool = false) {
            self.card = card
            self.cards = cards
            self.working = working
            self.workingCount = workingCount
            self.updatedAt = updatedAt
            self.sealed = sealed
            self.unreadable = unreadable
        }

        /// A state of the Mac has `sealed` alone, so every other field may be missing.
        init(from decoder: any Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            card = try values.decodeIfPresent(CoconutGlance.Card.self, forKey: .card)
            cards = try values.decodeIfPresent(Int.self, forKey: .cards) ?? 0
            working = try values.decodeIfPresent([CoconutGlance.Work].self, forKey: .working) ?? []
            workingCount = try values.decodeIfPresent(Int.self, forKey: .workingCount) ?? 0
            updatedAt = try values.decodeIfPresent(Date.self, forKey: .updatedAt) ?? .distantPast
            sealed = try values.decodeIfPresent(SealedGlance.self, forKey: .sealed)
        }
    }
}

/// The envelope of a glance of the Mac (PushEnvelope) as the content state of the Live Activity carries it.
struct SealedGlance: Codable, Hashable, Sendable {
    let version: Int
    let id: String
    let keyId: String
    let expires: Int
    let encapsulatedKey: Data
    let ciphertext: Data

    var envelope: PushEnvelope {
        PushEnvelope(version: version, id: id, keyId: keyId, expires: expires, encapsulatedKey: encapsulatedKey,
                     ciphertext: ciphertext)
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

extension CoconutWorkAttributes.ContentState {
    /// What the activity shows: the state that the app wrote, or the glance that the Mac sealed, opened with the key of
    /// this iPhone (MacGlance). The activity renders in the widget extension, which shares that key with the app. A
    /// glance that does not open, for example after the iPhone paired again, asks to open Coconut.
    var shown: Self {
        guard let sealed else { return self }
        guard let data = try? NotificationKeys.open(sealed.envelope, kind: "glance"),
              let glance = try? MacGlance.decode(data) else { return Self(updatedAt: .now, unreadable: true) }
        return Self(glance.glance())
    }
}
