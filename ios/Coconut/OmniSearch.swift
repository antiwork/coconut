import SwiftUI

/// The search of the whole app (the person, 2026-11-26: "search via omnibar or something is missing from coconut"):
/// the chats by their names, the to-dos of gumroad-private, the pages of the Library, the cards that wait, and the
/// messages of the chats. The Mac opens it over its window with ⌘K or the Search row of the sidebar (MacOmnibar). The
/// phone has its field at the top of Chats. The names match on the device. The messages match on the device and on the
/// Mac, which keeps every message (/v1/search), so a hit can come from a chat that the phone never loaded.
enum OmniSearch {
    /// One message that has every word of the search.
    struct MessageHit: Decodable, Identifiable, Equatable, Sendable {
        let id: String
        let botId: String
        let role: String
        let createdAt: String
        /// The words of the message around the first match, on one line.
        let snippet: String
    }

    static let chatLimit = 8
    static let todoLimit = 5
    static let pageLimit = 4
    static let cardLimit = 3
    static let messageLimit = 20

    /// A search ignores case, accents and the width of characters, so "cafe" finds "Café".
    static let matching: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive, .widthInsensitive]

    static func fold(_ text: String) -> String { text.folding(options: matching, locale: nil) }

    /// The words of a search. A name or a message matches when it has each of them.
    static func words(_ query: String) -> [String] {
        fold(String(query.prefix(200))).split(whereSeparator: \.isWhitespace).map(String.init)
    }

    /// How well a name matches: 0 when it starts with the search, 1 when each word starts a word of the name, 2 when
    /// the words are inside it. Nil when a word is missing.
    static func rank(_ name: String, _ words: [String]) -> Int? {
        guard !words.isEmpty else { return nil }
        let folded = fold(name)
        guard words.allSatisfy({ folded.contains($0) }) else { return nil }
        if folded.hasPrefix(words.joined(separator: " ")) { return 0 }
        let parts = folded.split { !$0.isLetter && !$0.isNumber }
        return words.allSatisfy { word in parts.contains { $0.hasPrefix(word) } } ? 1 : 2
    }

    /// The name of a chat as its row shows it.
    static func name(of item: ChatListItem) -> String {
        switch item {
        case .bot(let bot): bot.listName
        case .team(let room): room.title
        case .direct(let room, let direct): room.directPeer(direct.member).name
        case .botDirect(let room, let direct): room.botPeer(direct.bot).name
        }
    }

    static func isArchived(_ item: ChatListItem) -> Bool {
        if case .bot(let bot) = item { return bot.archived }
        return false
    }

    /// The chats whose names have the words: the best match first, an archived chat after the others, then the
    /// newest. Without words, the newest chats that are not archived.
    @MainActor
    static func chats(_ words: [String], in store: AppStore, limit: Int = chatLimit) -> [ChatListItem] {
        let latest = store.chatSummaries.newestActivity
        var seen = Set<String>()
        let items = ChatList.sections(bots: store.bots, latest: latest, rooms: store.shownTeamRooms,
                                      local: store.newestLocalMessages)
            .flatMap(\.items).filter { seen.insert($0.id).inserted }
        guard !words.isEmpty else {
            return Array(items.filter { !isArchived($0) }
                .sorted { $0.activity(latest: latest) > $1.activity(latest: latest) }.prefix(limit))
        }
        let ranked = items.compactMap { item in rank(name(of: item), words).map { (item: item, rank: $0) } }
        return ranked.sorted { a, b in
            if a.rank != b.rank { return a.rank < b.rank }
            if isArchived(a.item) != isArchived(b.item) { return !isArchived(a.item) }
            return a.item.activity(latest: latest) > b.item.activity(latest: latest)
        }.prefix(limit).map(\.item)
    }

    /// The to-dos whose title or number has the words, in the order of their lists after the best match.
    @MainActor
    static func todos(_ words: [String], in store: AppStore, limit: Int = todoLimit) -> [TodoIssue] {
        guard !words.isEmpty else { return [] }
        let ranked = (store.todoIssues?.items ?? []).enumerated().compactMap { index, todo in
            rank("\(todo.title) #\(todo.number)", words).map { (todo: todo, rank: $0, index: index) }
        }
        return ranked.sorted { ($0.rank, $0.index) < ($1.rank, $1.index) }.prefix(limit).map(\.todo)
    }

    /// The pages of the Library whose title has the words, newest first after the best match. A page of an owner that
    /// the app does not know stays hidden, as in the Library.
    @MainActor
    static func pages(_ words: [String], in store: AppStore, limit: Int = pageLimit) -> [BotDocument] {
        guard !words.isEmpty else { return [] }
        let owners = Set(store.bots.map(\.id) + store.teamBots.map(\.id))
        let ranked = (store.documents ?? []).filter { owners.contains($0.botId) }.compactMap { page in
            rank(page.title, words).map { (page: page, rank: $0) }
        }
        return ranked.sorted { a, b in
            a.rank != b.rank ? a.rank < b.rank : a.page.updatedAt > b.page.updatedAt
        }.prefix(limit).map(\.page)
    }

    /// The cards that wait whose title has the words, newest first after the best match.
    @MainActor
    static func cards(_ words: [String], in store: AppStore, limit: Int = cardLimit) -> [Decision] {
        guard !words.isEmpty else { return [] }
        let ranked = store.pendingDecisions.compactMap { card in rank(card.title, words).map { (card: card, rank: $0) } }
        return ranked.sorted { a, b in
            a.rank != b.rank ? a.rank < b.rank : a.card.createdAt > b.card.createdAt
        }.prefix(limit).map(\.card)
    }

    /// A card waits for the person, as the "Needs you" of a chat says (StatusLabel).
    static let cardSymbol = "circle.inset.filled"

    /// The list of a to-do and its line: "Finance · #4770 · Awaiting human · Nov 21".
    static func detail(of todo: TodoIssue) -> String {
        let area = todo.areas.lazy.compactMap(TodoArea.init(rawValue:)).first
        return [area?.title, todo.detail()].compactMap { $0 }.joined(separator: " · ")
    }

    /// The name of the owner of a page, or of the agent of a card.
    @MainActor
    static func owner(_ botId: String, in store: AppStore) -> String? {
        store.bots.first { $0.id == botId }?.listName ?? store.teamBots.first { $0.id == botId }?.name
    }

    /// The text messages of the device that have every word, newest first. It runs off the main actor: the device can
    /// hold a few megabytes of messages.
    static func localMessages(_ words: [String], in messages: [Message], bots: Set<String>,
                              limit: Int = messageLimit) async -> [MessageHit] {
        guard let first = words.first else { return [] }
        let found = messages.filter { message in
            message.kind == "text" && !message.streaming && bots.contains(message.botId)
                && words.allSatisfy { message.text.range(of: $0, options: matching) != nil }
        }
        return found.sorted { $0.createdAt > $1.createdAt }.prefix(limit).map { message in
            MessageHit(id: message.id, botId: message.botId, role: message.role, createdAt: message.createdAt,
                       snippet: snippet(message.text, around: first))
        }
    }

    /// The messages of a search: those of the device at once, then with those of the Mac a moment after the person
    /// stops typing. `show` gets each list. False when a newer search took its place.
    @MainActor
    static func messages(_ query: String, in store: AppStore, show: ([MessageHit]) -> Void) async -> Bool {
        let words = words(query)
        guard !words.isEmpty else {
            show([])
            return true
        }
        let bots = Set(store.bots.map(\.id))
        let local = await localMessages(words, in: store.messages, bots: bots)
        guard !Task.isCancelled else { return false }
        show(local)
        try? await Task.sleep(for: .milliseconds(200))
        guard !Task.isCancelled else { return false }
        let remote = await store.searchMessages(query)
        guard !Task.isCancelled else { return false }
        show(merged(local, remote, bots: bots))
        return true
    }

    /// The hits of the device and of the Mac in one list, newest first. A hit of the Mac in a chat that the device
    /// does not show stays out.
    static func merged(_ local: [MessageHit], _ remote: [MessageHit], bots: Set<String>,
                       limit: Int = messageLimit) -> [MessageHit] {
        var seen = Set<String>()
        return (local + remote.filter { bots.contains($0.botId) }.map { hit in
            MessageHit(id: hit.id, botId: hit.botId, role: hit.role, createdAt: hit.createdAt, snippet: plain(hit.snippet))
        })
        .filter { seen.insert($0.id).inserted }
        .sorted { ($0.createdAt.apiDate ?? .distantPast) > ($1.createdAt.apiDate ?? .distantPast) }
        .prefix(limit).map { $0 }
    }

    /// The words around the first match: 40 characters before it and 160 after, cut at a space, with "…" where the
    /// text goes on.
    static func snippet(_ text: String, around word: String) -> String {
        guard let match = text.range(of: word, options: matching) else { return plain(String(text.prefix(200))) }
        var start = text.index(match.lowerBound, offsetBy: -40, limitedBy: text.startIndex) ?? text.startIndex
        var end = text.index(match.upperBound, offsetBy: 160, limitedBy: text.endIndex) ?? text.endIndex
        if start > text.startIndex, let space = text[start..<match.lowerBound].firstIndex(where: \.isWhitespace) {
            start = text.index(after: space)
        }
        if end < text.endIndex, let space = text[match.upperBound..<end].lastIndex(where: \.isWhitespace) {
            end = space
        }
        let line = plain(String(text[start..<end]))
        return (start > text.startIndex ? "…" : "") + line + (end < text.endIndex ? "…" : "")
    }

    /// A text on one line without the marks of Markdown: a link shows its words, and no star, underscore, backtick,
    /// heading mark or bullet stays.
    static func plain(_ text: String) -> String {
        var text = text.replacingOccurrences(of: #"!?\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
        text = text.replacingOccurrences(of: #"(?m)^\s*(#{1,6}|[-*+>]|\d+\.)\s+"#, with: "", options: .regularExpression)
        for mark in ["**", "__", "`"] { text = text.replacingOccurrences(of: mark, with: "") }
        return text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    /// A line with each word of the search in bold, in the color of the text, for a line that is otherwise quiet.
    static func highlighted(_ text: String, _ words: [String]) -> AttributedString {
        var result = AttributedString(text)
        for word in words where !word.isEmpty {
            var from = text.startIndex
            while from < text.endIndex, let match = text.range(of: word, options: matching, range: from..<text.endIndex) {
                let lower = result.characters.index(result.startIndex, offsetBy: text.distance(from: text.startIndex, to: match.lowerBound))
                let upper = result.characters.index(lower, offsetBy: text.distance(from: match.lowerBound, to: match.upperBound))
                result[lower..<upper].inlinePresentationIntent = .stronglyEmphasized
                result[lower..<upper].swiftUI.foregroundColor = .primary
                from = match.upperBound
            }
        }
        return result
    }
}

extension AppStore {
    /// The messages on the Mac that have every word, newest first. None in a demo, without a connection, or from a
    /// Mac whose service still finds only the chats (it answers only bot_ids).
    func searchMessages(_ query: String) async -> [OmniSearch.MessageHit] {
        guard !isDemo else { return [] }
        var components = URLComponents()
        components.queryItems = [URLQueryItem(name: "q", value: String(query.prefix(200)))]
        // The service reads "+" as a space, as a form does.
        guard let encoded = components.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B") else { return [] }
        struct Answer: Decodable, Sendable { var messages: [OmniSearch.MessageHit]? }
        let answer: Answer? = try? await businessRequest("search?" + encoded)
        return answer?.messages ?? []
    }
}
