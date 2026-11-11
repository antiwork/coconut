import Foundation

// The Team chat of the app (docs/team-log.md, "Team chat"). The values come from the Mac, and in the end from the
// other members of the team. So a decode keeps each field that can be missing optional, and a page drops a message
// that it cannot read instead of the whole page.

/// The newest message of a team, for its row in the Chats list. `text` is the start of the message.
struct TeamLastMessage: Decodable, Equatable, Sendable {
    var first: Int? = nil
    var member: String? = nil
    var memberName: String? = nil
    /// True for a message of this Mac, also of a bot of this Mac. An older service sends none.
    var thisMac: Bool? = nil
    var bot: String? = nil
    var botName: String? = nil
    var text: String? = nil
    var createdAt: String? = nil
}

/// The answered message that a message quotes: the start of its text.
struct TeamMessageQuote: Codable, Equatable, Sendable {
    var id: String? = nil
    var member: String? = nil
    var memberName: String? = nil
    var bot: String? = nil
    var botName: String? = nil
    var text: String? = nil
}

/// One message of the Team chat. `bot` is the `t-<room>-<entry>` ID of the team bot that wrote it.
struct TeamMessage: Decodable, Identifiable, Equatable, Sendable {
    var id: String
    var first: Int
    var member: String? = nil
    var memberName: String? = nil
    var thisMac: Bool? = nil
    var bot: String? = nil
    var botName: String? = nil
    var text: String
    var replyTo: String? = nil
    var reply: TeamMessageQuote? = nil
    var mentions: [String]? = nil
    var createdAt: String
    var editedAt: String? = nil

    /// A message of this Mac's person. A bot of this Mac writes as itself, not as the person.
    var isOwn: Bool { thisMac == true && bot == nil }
    var isEdited: Bool { editedAt != nil }
}

/// One page of the chat, newest first. `before` is the `first` to ask for the next older page, or nil.
struct TeamMessagePage: Decodable, Sendable {
    var messages: [TeamMessage]
    var before: Int?

    init(messages: [TeamMessage], before: Int?) {
        self.messages = messages
        self.before = before
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        messages = (try container.decodeIfPresent([Lossy<TeamMessage>].self, forKey: .messages) ?? []).compactMap(\.value)
        before = try? container.decodeIfPresent(Int.self, forKey: .before)
    }

    private enum CodingKeys: String, CodingKey { case messages, before }
}

/// A value that decodes to nil when its JSON does not fit, so one bad item does not fail the list.
private struct Lossy<Value: Decodable & Sendable>: Decodable, Sendable {
    let value: Value?
    init(from decoder: Decoder) throws { value = try? Value(from: decoder) }
}

struct TeamMessageAnswer: Decodable, Sendable {
    var message: TeamMessage
}

struct TeamReadAnswer: Decodable, Sendable {
    var unread: Int? = nil
}

struct TeamDeleteAnswer: Decodable, Sendable {
    var id: String
}

/// A message of this person that is on its way to the Mac, or that did not send. The ID is the `Idempotency-Key` of
/// each try, so the Mac writes the message one time, also when an earlier try reached it without an answer.
struct TeamPendingMessage: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let text: String
    let replyTo: String?
    let reply: TeamMessageQuote?
    let createdAt: String
    var failed = false

    var idempotencyKey: String { id }

    /// The same message for a new try: the same key.
    var retried: TeamPendingMessage {
        var next = self
        next.failed = false
        return next
    }

    /// A message that was on its way when the app ended did not send for sure. The person decides to try again.
    var afterRestart: TeamPendingMessage {
        var restored = self
        restored.failed = true
        return restored
    }
}

/// The part of the chat of one team that the app has: the newest messages and the older pages that the person asked
/// for, the messages on their way, and the edits and removals that the Mac did not answer yet.
struct TeamChat: Equatable, Sendable {
    /// Oldest first, by `first`. The Mac sorts a chat by `first`, so an edit keeps its place.
    var messages: [TeamMessage] = []
    /// The `first` of the next older page. Nil when the chat has its oldest message.
    var before: Int?
    /// True after the first page arrived.
    var loaded = false
    var loadingEarlier = false
    /// The text of the last failed load. The chat shows it only while it has no page.
    var error: String?
    var pending: [TeamPendingMessage] = []
    /// The new text of an edit that the Mac did not answer yet, by message ID.
    var editing: [String: String] = [:]
    /// The messages whose removal the Mac did not answer yet. They do not show.
    var deleting: Set<String> = []

    /// The `first` of the newest message that the Mac has.
    var newestFirst: Int? { messages.last?.first }

    /// The lower end of the range of the newest page: the `before` of its answer. Nil when the newest page reaches the
    /// start of the chat. The older pages are the messages under it.
    var newestFloor: Int?

    /// The chat has older pages that the person loaded, under the newest page.
    var hasOlderPages: Bool { loaded && before != newestFloor }

    /// Puts a page into the chat. A page covers a range of `first`: from its oldest message, or from the start when
    /// `before` of the page is nil, up to `requested` (the `before` of the request), or up to the end for the newest
    /// page. In that range the page is the truth: a new message appears, an edit replaces its message, and a message
    /// that the page does not have goes. A message outside the range stays.
    ///
    /// `floor` is for a page that reads a loaded range again: the page covers nothing under it, and the chat keeps its
    /// `before`.
    ///
    /// Returns true when the page started the loaded range again. Each read of the old range is then out of date.
    @discardableResult
    mutating func merge(_ page: TeamMessagePage, requested: Int?, floor: Int? = nil) -> Bool {
        var reset = false
        var lower = page.before
        if let floor {
            lower = max(lower ?? floor, floor)
        } else if requested == nil {
            if loaded, let lower, messages.last.map({ $0.first < lower }) ?? true {
                // More messages came than one page holds, so the chat would have a gap. The page starts it again.
                messages = []
                before = lower
                reset = true
            } else if !loaded || lower == nil {
                before = lower
            }
            // Else the chat keeps its older pages, and their `before`.
            newestFloor = lower
        } else {
            before = lower
        }
        var seen = Set<String>()
        let incoming = page.messages.filter { message in
            (floor.map { message.first >= $0 } ?? true) && seen.insert(message.id).inserted
        }
        messages.removeAll { message in
            seen.contains(message.id)
                || ((lower.map { message.first >= $0 } ?? true) && (requested.map { message.first < $0 } ?? true))
        }
        messages = (messages + incoming).sorted { ($0.first, $0.id) < ($1.first, $1.id) }
        // A page of the Mac says which messages exist. It is newer than a removal that it has again.
        removed.subtract(seen)
        loaded = true
        return reset
    }

    /// The messages that this person removed. A late answer cannot put one back. Only a page of the Mac can.
    var removed: Set<String> = []

    /// Puts one message that the Mac answered into the chat: a new message, or the new form of an edited one.
    mutating func put(_ message: TeamMessage) {
        guard !removed.contains(message.id) else { return }
        messages.removeAll { $0.id == message.id }
        let index = messages.firstIndex { ($0.first, $0.id) > (message.first, message.id) } ?? messages.endIndex
        messages.insert(message, at: index)
    }

    mutating func remove(_ id: String) {
        messages.removeAll { $0.id == id }
        removed.insert(id)
    }

    /// Drops the older pages and keeps the newest page, so a chat that opens again reads no stale history.
    mutating func keepNewestPage() {
        guard hasOlderPages, let floor = newestFloor else { return }
        messages.removeAll { $0.first < floor }
        before = floor
    }
}

/// Who wrote a message. A run of messages has one writer.
enum TeamWriter: Hashable, Sendable {
    case you
    case member(String)
    case bot(String)

    init(_ message: TeamMessage) {
        if let bot = message.bot { self = .bot(bot) }
        else if message.thisMac == true { self = .you }
        else { self = .member(message.member ?? "") }
    }
}

/// One row of the chat: a message of the Mac, or a message of this person on its way.
struct TeamChatRow: Identifiable, Equatable, Sendable {
    enum Content: Equatable, Sendable {
        case message(TeamMessage)
        case pending(TeamPendingMessage)
    }

    let id: String
    let content: Content
    let writer: TeamWriter
    let createdAt: String
    /// The first row of a calendar day. The day pill shows above it.
    let startsDay: Bool
    /// The first row of a run: messages of one writer on one day with no other writer between them. The name of the
    /// writer shows above it.
    let startsRun: Bool

    var message: TeamMessage? { if case .message(let message) = content { message } else { nil } }
    var text: String {
        switch content {
        case .message(let message): message.text
        case .pending(let pending): pending.text
        }
    }

    /// The rows of the chat: the messages without the ones whose removal is on its way, then the messages on their way.
    static func rows(_ chat: TeamChat, calendar: Calendar = .current) -> [TeamChatRow] {
        let shown = chat.messages.filter { !chat.deleting.contains($0.id) }
            .map { (id: $0.id, content: Content.message($0), writer: TeamWriter($0), createdAt: $0.createdAt) }
        let pending = chat.pending.map {
            (id: "pending-" + $0.id, content: Content.pending($0), writer: TeamWriter.you, createdAt: $0.createdAt)
        }
        var rows: [TeamChatRow] = []
        var previous: (writer: TeamWriter, createdAt: String)?
        for item in shown + pending {
            let startsDay = previous.map { !DaySeparator.sameDay($0.createdAt, item.createdAt, calendar: calendar) } ?? true
            rows.append(TeamChatRow(id: item.id, content: item.content, writer: item.writer, createdAt: item.createdAt,
                                    startsDay: startsDay, startsRun: startsDay || previous?.writer != item.writer))
            previous = (item.writer, item.createdAt)
        }
        return rows
    }
}

/// When the app tells the Mac that the person read the chat.
enum TeamReadPosition {
    /// The `first` to post: the newest message, while the chat is on screen and shows it, and only when it is newer
    /// than the position that the app posted or is posting. Nil when the app sends nothing.
    static func toPost(newest: Int?, posted: Int?, visible: Bool, atBottom: Bool) -> Int? {
        guard visible, atBottom, let newest, newest > (posted ?? 0) else { return nil }
        return newest
    }
}

/// The names that the chat shows for a writer.
enum TeamNames {
    static let formerMember = "Former member"

    /// The writer of a quoted message. `ownMember` is the member ID of this Mac.
    static func author(of quote: TeamMessageQuote, ownMember: String?) -> String {
        if let bot = quote.botName ?? quote.bot.map({ _ in "Bot" }) { return bot }
        if let member = quote.member, member == ownMember { return "You" }
        return quote.memberName ?? formerMember
    }

    /// The writer of a message, for a quote of a reply in the composer and for VoiceOver.
    static func author(of message: TeamMessage) -> String {
        if message.bot != nil { return message.botName ?? "Bot" }
        if message.isOwn { return "You" }
        return message.memberName ?? formerMember
    }
}

/// The second line of the row of a team in the Chats list.
enum TeamChatPreview {
    static let empty = "No messages yet"

    /// "Ada: text" for a message of another member, "You: text" for a message of this person, and the name of the bot
    /// for a message of a bot. An older service does not say whose message it is, so its row never says "You".
    static func text(_ last: TeamLastMessage?) -> String {
        guard let last, let text = last.text else { return empty }
        let body = TranscriptPresentation.previewText(text)
        if last.bot != nil || last.botName != nil { return (last.botName ?? "Bot") + ": " + body }
        if last.thisMac == true { return "You: " + body }
        return (last.memberName ?? TeamNames.formerMember) + ": " + body
    }
}

/// One row of the Chats list: a bot or a team.
enum ChatListItem: Identifiable, Equatable {
    case bot(Bot)
    case team(TeamRoom)

    var id: String {
        switch self {
        case .bot(let bot): "bot-" + bot.id
        case .team(let room): "team-" + room.room
        }
    }
}

enum ChatList {
    /// The date of the newest message of each bot, in one pass over the messages. A reaction is no activity.
    static func lastMessageDates(_ messages: [Message]) -> [String: Date] {
        var latest: [String: Date] = [:]
        for message in messages where message.reactionEvent == nil {
            guard let date = message.createdAt.apiDate else { continue }
            if latest[message.botId].map({ date > $0 }) ?? true { latest[message.botId] = date }
        }
        return latest
    }

    /// One list, newest activity first. `bots` is in that order already. The activity of a team is its newest message,
    /// or the time when this Mac made or joined it. A team and a bot with the same time keep the bot first.
    static func items(bots: [Bot], latest: [String: Date], rooms: [TeamRoom]) -> [ChatListItem] {
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        let teams = rooms.sorted {
            ($0.lastActivity, $1.room) > ($1.lastActivity, $0.room)
        }
        var items: [ChatListItem] = []
        items.reserveCapacity(bots.count + teams.count)
        var team = teams.startIndex
        for bot in bots {
            while team < teams.endIndex, teams[team].lastActivity > activity(bot) {
                items.append(.team(teams[team]))
                team += 1
            }
            items.append(.bot(bot))
        }
        items += teams[team...].map(ChatListItem.team)
        return items
    }
}

/// The filter of the Chats list on an iPhone. "People" has the teams, and later the direct messages.
enum ChatFilter: String, CaseIterable, Identifiable, Sendable {
    case all, people, bots

    var id: String { rawValue }

    var title: String {
        switch self {
        case .all: "All"
        case .people: "People"
        case .bots: "Bots"
        }
    }

    /// The chips show only while the paired Mac is a member of a team: an active team, or one that it joins. A team
    /// that the Mac left (`removed`, `gone`) keeps its history as a row of "All", but it brings no chips. Without them
    /// the list is "All".
    static func showsChips(rooms: [TeamRoom]) -> Bool {
        rooms.contains { $0.status == .active || $0.status == .joining }
    }

    /// The filter that the list uses: the choice of the person while the chips show, else "All". The store also keeps
    /// this value after each list of the teams, so a Mac that left its last team starts at "All" when chips return.
    static func effective(_ chosen: ChatFilter, rooms: [TeamRoom]) -> ChatFilter {
        showsChips(rooms: rooms) ? chosen : .all
    }

    /// The pinned tiles are bots, so "People" has none.
    var showsPinned: Bool { self != .people }

    /// The rows of the list for this filter, in the order of the list.
    func items(_ items: [ChatListItem]) -> [ChatListItem] {
        switch self {
        case .all: items
        case .people: items.filter { if case .team = $0 { true } else { false } }
        case .bots: items.filter { if case .bot = $0 { true } else { false } }
        }
    }
}

/// The sections of the Mac sidebar: the pinned bots, the people (the teams), and the other bots. Each one newest
/// activity first.
struct ChatSections: Equatable {
    var pinned: [Bot]
    var people: [TeamRoom]
    var bots: [Bot]

    /// One pass over the messages. A section without rows does not show.
    static func make(bots: [Bot], messages: [Message], rooms: [TeamRoom]) -> ChatSections {
        let latest = ChatList.lastMessageDates(messages)
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        let active = bots.filter { !$0.archived }.sorted { activity($0) > activity($1) }
        let people = rooms.sorted { ($0.lastActivity, $1.room) > ($1.lastActivity, $0.room) }
        return ChatSections(pinned: active.filter(\.pinned), people: people, bots: active.filter { !$0.pinned })
    }
}

/// The face of a team, or of a person without a face of its own: letters on a fill of `BoardStyle.faceColors`.
enum LetterFace {
    /// The first letters of the first two words, or the first letters of a name of one word, in uppercase. Nil for a
    /// name without a letter: the face then shows its symbol.
    static func letters(_ name: String, count: Int = 2) -> String? {
        let words = name.split { !$0.isLetter && !$0.isNumber }.filter { $0.contains(where: \.isLetter) }
        guard let first = words.first else { return nil }
        if count > 1, words.count > 1, let a = first.first(where: \.isLetter), let b = words[1].first(where: \.isLetter) {
            return String([a, b]).uppercased()
        }
        return String(first.filter(\.isLetter).prefix(count)).uppercased()
    }

    /// The face of the writer of a message without a face of its own: the first letter of the name that the message
    /// has, on the fill of the member ID of the message. Both come from the message, not from the member list, so a
    /// message looks the same before and after its writer leaves the team. Only a writer whose name the Mac never
    /// learned ("Former member") has no letter: the neutral face.
    static func person(memberId: String, name: String?) -> (letters: String?, colorIndex: Int) {
        (name.flatMap { letters($0, count: 1) }, colorIndex(memberId))
    }

    /// The fill for an ID: the same ID always gets the same fill, also after a new name.
    static func colorIndex(_ id: String) -> Int {
        Int(characterSeed(id) % UInt64(BoardStyle.faceColors.count))
    }
}

extension TeamRoom {
    /// Who is in the chat: "3 people", or "3 people, 1 bot" when the team has bots. Nil while the list has no member.
    var whoLine: String? {
        let people = memberList.count
        guard people > 0 else { return nil }
        let line = people == 1 ? "1 person" : "\(people) people"
        let bots = botList.count
        guard bots > 0 else { return line }
        return line + (bots == 1 ? ", 1 bot" : ", \(bots) bots")
    }
}
