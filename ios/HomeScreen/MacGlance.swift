import Foundation

/// The glance that the Mac sends after the person leaves the app (backend/coconut/home_screen.py): the cards that wait,
/// the agents that work with their step, and the unread agent chats, sealed for this iPhone. The widgets read it from the
/// relay (HomeScreenRelay), and the Live Activity gets it as its content state, so both follow the agents while the app
/// does not run.
struct MacGlance: Decodable, Sendable {
    struct Target: Decodable, Sendable {
        let version: Int
        let deviceId: String
        let kind: String
    }

    struct Card: Decodable, Sendable {
        let id: String
        let title: String
        let chat: String
        let name: String
        let at: Double?
    }

    struct Work: Decodable, Sendable {
        let chat: String
        let name: String
        /// A phase of BotPhase. The phone names it, as the row of the chat does.
        let phase: String
        let detail: String?
        let at: Double?
    }

    struct Unread: Decodable, Sendable {
        let chat: String
        let name: String
        let preview: String
        let count: Int
        let at: Double?
    }

    let coconut: Target
    let at: Double
    let cards: [Card]
    let working: [Work]
    let unread: [Unread]
    let unreadCount: Int

    var updatedAt: Date { Date(timeIntervalSince1970: at) }

    static func decode(_ data: Data) throws -> MacGlance {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let glance = try decoder.decode(MacGlance.self, from: data)
        guard glance.coconut.version == 1, glance.coconut.kind == "glance" else { throw CocoaError(.coderInvalidValue) }
        return glance
    }

    /// The glance as the widgets and the Live Activity show it. The Mac sends no to-dos, so the counts that the app wrote
    /// last stay.
    func glance(todos: [CoconutGlance.Todos] = []) -> CoconutGlance {
        func date(_ value: Double?) -> Date? { value.map(Date.init(timeIntervalSince1970:)) }
        return CoconutGlance(
            cards: cards.map { .init(id: $0.id, title: $0.title, chat: $0.chat, name: $0.name, createdAt: date($0.at) ?? updatedAt) },
            working: working.map { .init(id: $0.chat, name: $0.name, step: (BotPhase(rawValue: $0.phase) ?? .thinking).label,
                                         detail: $0.detail, startedAt: date($0.at)) },
            unread: unread.map { .init(id: $0.chat, name: $0.name, preview: $0.preview, count: $0.count,
                                       date: date($0.at) ?? updatedAt, link: .chat($0.chat, card: nil)) },
            unreadCount: unreadCount, todos: todos, updatedAt: updatedAt, fromMac: true)
    }

    /// The newest glance of the Mac on the relay, or nil.
    static func fromRelay() async -> MacGlance? {
        guard let data = await HomeScreenRelay.sealedGlance() else { return nil }
        return try? decode(data)
    }
}

extension CoconutGlance {
    /// The newest of the glance that the app wrote and the one of the Mac. The Mac sends no to-dos, so a glance of the
    /// Mac keeps the counts of the app.
    static func newest(app: CoconutGlance?, mac: MacGlance?) -> CoconutGlance? {
        guard let mac, mac.updatedAt > app?.updatedAt ?? .distantPast else { return app }
        return mac.glance(todos: app?.todos ?? [])
    }
}
