import SwiftUI

/// Each agent of a person has a letter from A to Z: its face, and its name until the person gives another. No two
/// agents share a letter, so the 26 letters are the agents a person can have. The Mac keeps the rule
/// (backend/coconut/agent_letters.py). The avatar of an agent is `letter.<A-Z>`. The color comes from the letter.
enum AgentLetter {
    static let all = (UInt8(ascii: "A")...UInt8(ascii: "Z")).map { String(UnicodeScalar($0)) }
    static let prefix = "letter."

    static func symbol(_ letter: String) -> String { prefix + letter }

    /// The letter of an avatar, or nil for another avatar, for example a fruit from an older release.
    static func exact(_ symbol: String) -> String? {
        guard symbol.hasPrefix(prefix) else { return nil }
        let letter = String(symbol.dropFirst(prefix.count))
        return all.contains(letter) ? letter : nil
    }

    /// The letter to show. A face without its own letter (a teammate's agent on an older release) shows the initial
    /// of its name, else a letter that stays the same for its identity.
    static func letter(symbol: String, name: String? = nil, identity: String = "") -> String {
        if let exact = exact(symbol) { return exact }
        if let name, let first = initial(name) { return first }
        let seed = identity.isEmpty ? symbol : identity
        return all[Int(characterSeed(seed) % UInt64(all.count))]
    }

    /// The first letter of a name as A to Z, or nil. “Émile” gives E.
    static func initial(_ name: String) -> String? {
        for character in name.folding(options: .diacriticInsensitive, locale: nil).uppercased() {
            let text = String(character)
            if all.contains(text) { return text }
            if character.isLetter || character.isNumber { return nil }
        }
        return nil
    }

    /// The fill and the ink of a letter. Neighboring letters get different fills.
    static func colors(_ letter: String) -> (fill: String, ink: String) {
        BoardStyle.faceColors[(all.firstIndex(of: letter) ?? 0) % BoardStyle.faceColors.count]
    }

    /// The letters that agents hold, with the name of each. `besides` leaves out one agent, so it can keep its own.
    static func taken(by bots: [Bot], besides: String = "") -> [String: String] {
        var held: [String: String] = [:]
        for bot in bots where bot.chatOf == nil && bot.id != besides {
            if let letter = exact(bot.avatar), held[letter] == nil { held[letter] = bot.name }
        }
        return held
    }

    /// The letter for a new agent: the initial of its name when that is free, else the first free letter.
    static func free(_ held: [String: String], name: String = "") -> String? {
        if let first = initial(name), held[first] == nil { return first }
        return all.first { held[$0] == nil }
    }
}
