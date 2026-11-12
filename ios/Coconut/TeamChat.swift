import Foundation

// The Team chat of the app (docs/team-log.md, "Team chat"). The values come from the Mac, and in the end from the
// other members of the team. So a decode keeps each field that can be missing optional, and a page drops a message
// that it cannot read instead of the whole page.

/// The key of one chat of a team: the Team chat of a room, or the direct chat of this person with one other member of
/// the room (docs/team-log.md, "Direct messages"). The view, the route, the store and the paths of the requests use it,
/// so both chats have one code path. A room ID alone names the Team chat of the room.
struct TeamChatKey: Hashable, Sendable, ExpressibleByStringLiteral {
    let room: String
    /// The member ID of the other person of a direct chat. Nil for the Team chat.
    var member: String? = nil

    init(room: String, member: String? = nil) {
        self.room = room
        self.member = member
    }

    init(stringLiteral room: String) { self.init(room: room) }

    var isDirect: Bool { member != nil }

    /// The path of the chat on the Mac. The messages and the read position are under it.
    var path: String {
        guard let member else { return "team/rooms/\(room)" }
        return "team/rooms/\(room)/directs/\(member)"
    }
}

/// One direct chat of a room, from `directs` of the list of the teams: the other member, its last name, the unread
/// count and the newest message. A chat with a member that left stays.
struct TeamDirect: Decodable, Identifiable, Equatable, Sendable {
    var member: String
    var memberName: String? = nil
    var unread: Int? = nil
    var lastMessage: TeamLastMessage? = nil

    var id: String { member }
}

/// The other person of a direct chat, as the chat and its row show it.
struct TeamDirectPeer: Equatable, Sendable {
    let member: String
    /// The member entry while the member is in the team. Its face comes from it.
    let entry: TeamMember?
    let name: String
    /// The Mac can send a direct message to the member now.
    let canReceive: Bool

    /// The line that takes the place of the composer when the person cannot write to the member.
    var closedLine: String { name + " is no longer in this team." }
}

extension TeamRoom {
    /// The member that this person can send a direct message to now: another person whose `direct` is true. The
    /// service sets `direct` only for a member that can get one, in an active team. Nil for each other member, for an
    /// older service, and for a member that is not in the list. This is the one place of that rule: the arrow of the
    /// Team screen, the name above a message and the composer of a direct chat read it.
    func directMember(_ member: String?) -> TeamMember? {
        memberList.first { $0.member == member && $0.direct == true && $0.thisMac != true && !$0.isBotOnly }
    }

    /// The other person of a direct chat. The name comes from the member list, else from the list of the direct
    /// chats, which keeps the last name of a member that left.
    func directPeer(_ member: String) -> TeamDirectPeer {
        let entry = memberList.first { $0.member == member }
        let name = entry?.name ?? directList.first { $0.member == member }?.memberName ?? TeamNames.formerMember
        return TeamDirectPeer(member: member, entry: entry, name: name, canReceive: directMember(member) != nil)
    }

    var directList: [TeamDirect] { directs ?? [] }

    /// The direct chats of this room that the Chats list and the Mac section "People" show. This is the one place of
    /// that rule: each chat of `directs`, and each direct chat of this room that the app has with a message that
    /// `directs` does not have yet. Such a chat has a first message that did not send, or a message that the Mac
    /// saved after the last list. Its row has no unread count. A chat of `directs` keeps the row of the Mac. An older
    /// service sends no `directs`, so its room has no direct chat, also with unsent messages in the cache.
    /// `local` is the newest message of each chat of the store (AppStore.newestLocalMessages).
    func listedDirects(local: [TeamChatKey: TeamLastMessage]) -> [TeamDirect] {
        guard let directs else { return [] }
        let listed = Set(directs.map(\.member))
        let added = local.compactMap { chat, last -> TeamDirect? in
            guard chat.room == room, let member = chat.member, !listed.contains(member) else { return nil }
            return TeamDirect(member: member, lastMessage: last)
        }
        return directs + added.sorted { $0.member < $1.member }
    }
}

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

    /// A message of a bot. An older service can send the name of the bot without its ID.
    var isBot: Bool { bot != nil || botName != nil }
    /// A message of this Mac's person, as TeamMessage.isOwn. A bot of this Mac writes as itself, not as the person.
    var isOwn: Bool { thisMac == true && !isBot }
    /// The name of the writer that the message has: the bot for a message of a bot, else the member. Nil without one.
    var writerName: String? { isBot ? botName : memberName }
}

/// The answered message that a message quotes: the start of its text.
struct TeamMessageQuote: Codable, Equatable, Sendable {
    var id: String? = nil
    /// True for a message of this Mac. A quote in a direct chat has it instead of a member.
    var thisMac: Bool? = nil
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
    /// The `id` of each team bot that the text names. Nil for a message without a mention, and for a message that an
    /// older app saved.
    var mentions: [String]? = nil

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

    /// The newest message of the chat in the app, for the row of a direct chat that the list of the Mac does not have
    /// yet: the newest unsent message, else the newest saved message that shows. Nil for a chat without one.
    var newestLocal: TeamLastMessage? {
        if let pending = pending.last {
            return TeamLastMessage(thisMac: true, text: pending.text, createdAt: pending.createdAt)
        }
        guard let message = messages.last(where: { !deleting.contains($0.id) }) else { return nil }
        return TeamLastMessage(first: message.first, thisMac: message.thisMac, text: message.text,
                               createdAt: message.createdAt)
    }

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

    /// The writer of a quoted message. `ownMember` is the member ID of this Mac. `peer` is the name of the other
    /// person of a direct chat, whose messages have no name of their own.
    static func author(of quote: TeamMessageQuote, ownMember: String?, peer: String? = nil) -> String {
        if let bot = quote.botName ?? quote.bot.map({ _ in "Bot" }) { return bot }
        if quote.thisMac == true { return "You" }
        if let member = quote.member, member == ownMember { return "You" }
        return peer ?? quote.memberName ?? formerMember
    }

    /// The writer of a message of the Team chat, for a quote of a reply in the composer and for VoiceOver.
    static func author(of message: TeamMessage) -> String { author(of: message, peer: nil) }

    /// The writer of a message of a chat. `peer` as above.
    static func author(of message: TeamMessage, peer: String?) -> String {
        if message.bot != nil { return message.botName ?? "Bot" }
        if message.isOwn { return "You" }
        return peer ?? message.memberName ?? formerMember
    }
}

/// The second line of the row of a team or a direct chat in the Chats list.
enum TeamChatPreview {
    static let empty = "No messages yet"

    /// "Ada: text" for a message of another member, "You: text" for a message of this person, and the name of the bot
    /// for a message of a bot. An older service does not say whose message it is, so its row never says "You". A
    /// direct chat has one other writer, so its row names nobody but "You".
    static func text(_ last: TeamLastMessage?, direct: Bool = false) -> String {
        guard let last, let text = last.text else { return empty }
        let body = TranscriptPresentation.previewText(text)
        if last.isBot { return (last.writerName ?? "Bot") + ": " + body }
        if last.thisMac == true { return "You: " + body }
        if direct { return body }
        return (last.writerName ?? TeamNames.formerMember) + ": " + body
    }
}

/// One row of the Chats list: a bot, a team, or a direct chat with one person of a team.
enum ChatListItem: Identifiable, Equatable {
    case bot(Bot)
    case team(TeamRoom)
    case direct(TeamRoom, TeamDirect)

    var id: String {
        switch self {
        case .bot(let bot): "bot-" + bot.id
        case .team(let room): "team-" + room.room
        case .direct(let room, let direct): "direct-" + room.room + "-" + direct.member
        }
    }

    /// The chat of a team or a direct chat. Nil for a bot.
    var chat: TeamChatKey? {
        switch self {
        case .bot: nil
        case .team(let room): TeamChatKey(room: room.room)
        case .direct(let room, let direct): TeamChatKey(room: room.room, member: direct.member)
        }
    }

    /// The room of a team or a direct chat. Nil for a bot.
    var room: String? { chat?.room }

    /// A row of "People": a team or a direct chat.
    var isPeople: Bool { chat != nil }

    /// The newest activity of a team or a direct chat: its newest message, or for a team the time when this Mac made
    /// or joined it.
    fileprivate var peopleActivity: Date {
        switch self {
        case .bot: .distantPast
        case .team(let room): room.lastActivity
        case .direct(_, let direct): direct.lastMessage?.createdAt?.apiDate ?? .distantPast
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
    /// or the time when this Mac made or joined it, and the activity of a direct chat is its newest message. A team
    /// or a direct chat and a bot with the same time keep the bot first.
    static func items(bots: [Bot], latest: [String: Date], rooms: [TeamRoom],
                      local: [TeamChatKey: TeamLastMessage] = [:]) -> [ChatListItem] {
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        let people = people(rooms, local: local)
        var items: [ChatListItem] = []
        items.reserveCapacity(bots.count + people.count)
        var next = people.startIndex
        for bot in bots {
            while next < people.endIndex, people[next].peopleActivity > activity(bot) {
                items.append(people[next])
                next += 1
            }
            items.append(.bot(bot))
        }
        items += people[next...]
        return items
    }

    /// The teams and the direct chats of the rooms (TeamRoom.listedDirects), newest activity first. An older service
    /// sends no `directs`, so its rooms bring the teams only.
    static func people(_ rooms: [TeamRoom], local: [TeamChatKey: TeamLastMessage] = [:]) -> [ChatListItem] {
        let items = rooms.flatMap { room in
            [.team(room)] + room.listedDirects(local: local).map { ChatListItem.direct(room, $0) }
        }
        return items.sorted { ($0.peopleActivity, $1.id) > ($1.peopleActivity, $0.id) }
    }
}

/// The filter of the Chats list on an iPhone. "People" has the teams and the direct chats.
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
        case .people: items.filter(\.isPeople)
        case .bots: items.filter { !$0.isPeople }
        }
    }
}

/// The sections of the Mac sidebar: the pinned bots, the people (the teams and the direct chats), and the other bots.
/// Each one newest activity first.
struct ChatSections: Equatable {
    var pinned: [Bot]
    var people: [ChatListItem]
    var bots: [Bot]

    /// One pass over the messages. A section without rows does not show.
    static func make(bots: [Bot], messages: [Message], rooms: [TeamRoom],
                     local: [TeamChatKey: TeamLastMessage] = [:]) -> ChatSections {
        let latest = ChatList.lastMessageDates(messages)
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        let active = bots.filter { !$0.archived }.sorted { activity($0) > activity($1) }
        return ChatSections(pinned: active.filter(\.pinned), people: ChatList.people(rooms, local: local),
                            bots: active.filter { !$0.pinned })
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
    /// Who is in the chat: "3 people", or "3 people, 1 bot" when the team has a bot that a teammate can ask. Nil while
    /// the list has no member.
    var whoLine: String? {
        guard !memberList.isEmpty else { return nil }
        let count = people.count
        let line = count == 1 ? "1 person" : "\(count) people"
        let bots = TeamMentions.bots(of: self).count
        guard bots > 0 else { return line }
        return line + (bots == 1 ? ", 1 bot" : ", \(bots) bots")
    }
}

/// The names of team bots with `@` in the team chat. People are not in the list yet: a mention of a person does nothing.
enum TeamMentions {
    /// The most mentions that the Mac takes in one message (`MAX_MENTIONS` in backend/coconut/team_store.py). A send
    /// with more saves nothing, so a message that the Mac refuses for its mentions never waits for a retry.
    static let limit = 16
    static let overLimit = "Name at most \(limit) bots in one message."

    /// True when the `mentions` of a send fit the limit. The Mac counts each item of the list, so this does too.
    static func fits(_ mentions: [String]) -> Bool {
        mentions.count <= limit
    }

    /// The bots that a teammate can ask: the bots of the team that answer each member (`open`). A bot of a person,
    /// also of this person, answers no teammate: it is only the author of the pages that the person shares. This is
    /// the one place of that rule. The `@` list, the line of who is in the chat and the section "Bots" of the Team
    /// screen (TeamBotSection) read it.
    static func bots(of room: TeamRoom) -> [TeamRoomBot] {
        var seen = Set<String>()
        return room.botList.filter { $0.isOpen && seen.insert($0.id).inserted }
    }

    /// The `mentions` of a send: the `id` of each team bot that the text still names, one time each, in text order.
    static func ids(in text: String, selected: [SelectedBotMention], room: TeamRoom) -> [String] {
        BotMentions.recipients(in: text, selected: selected, bots: bots(of: room)).map(\.id)
    }
}

/// One row of the section "Bots" of the Team screen.
enum TeamBotRow: Identifiable, Equatable, Sendable {
    /// A bot that a teammate can ask (TeamMentions.bots).
    case answers(TeamRoomBot)
    /// A bot of a Mac for a team only that does not answer in the chat. It only publishes pages.
    case pagesOnly(TeamRoomBot)
    /// A Mac for a team only that has no bot in the team.
    case noBot(TeamMember)

    /// Also the accessibility identifier of the row.
    var id: String {
        switch self {
        case .answers(let bot), .pagesOnly(let bot): "team-bot-" + bot.id
        case .noBot(let mac): "team-mac-" + mac.member
        }
    }

    var name: String {
        switch self {
        case .answers(let bot), .pagesOnly(let bot): bot.name
        case .noBot(let mac): mac.name
        }
    }

    /// The member ID of the Mac for a team only of the row. A removal of the row removes that Mac (TeamRemoval).
    var member: String? {
        switch self {
        case .answers(let bot), .pagesOnly(let bot): bot.member
        case .noBot(let mac): mac.member
        }
    }

    /// The bot of the row, for its face. A Mac without a bot shows a quiet symbol of a Mac instead.
    var bot: TeamRoomBot? {
        switch self {
        case .answers(let bot), .pagesOnly(let bot): bot
        case .noBot: nil
        }
    }

    /// The quiet text at the right end of the row.
    var note: String? {
        switch self {
        case .answers: nil
        case .pagesOnly: "Pages only"
        case .noBot: "No bot yet"
        }
    }

    /// What VoiceOver reads for the row: one sentence.
    var spoken: String {
        switch self {
        case .answers: name + ", bot"
        case .pagesOnly: name + ", bot, pages only"
        case .noBot: name + ", no bot yet"
        }
    }
}

/// The section "Bots" of the Team screen: what the Macs for a team only bring to the team. A bot of a person is only
/// the author of the pages that the person shares, so it never shows. A Mac for a team only is no person, so the
/// section "Members" does not show it (TeamRoom.people), and this section does.
enum TeamBotSection {
    static let askLine = "Type @ in the team chat to ask a bot."

    /// The bots that a teammate can ask, the other bots of the Macs for a team only, and one row for each such Mac
    /// without a bot, in the order of their names. An older service sends no `open` and no `bot_only`, so the
    /// section then has no row, as before.
    static func rows(of room: TeamRoom) -> [TeamBotRow] {
        let macs = room.memberList.filter(\.isBotOnly)
        let macIds = Set(macs.map(\.member))
        let withBot = Set(room.botList.compactMap(\.member))
        let answers = TeamMentions.bots(of: room)
        var seen = Set(answers.map(\.id))
        let pagesOnly = room.botList.filter { bot in
            bot.member.map(macIds.contains) == true && seen.insert(bot.id).inserted
        }
        let rows = answers.map(TeamBotRow.answers) + pagesOnly.map(TeamBotRow.pagesOnly)
            + macs.filter { !withBot.contains($0.member) }.map(TeamBotRow.noBot)
        return rows.sorted {
            switch $0.name.localizedStandardCompare($1.name) {
            case .orderedAscending: true
            case .orderedDescending: false
            case .orderedSame: $0.id < $1.id
            }
        }
    }

    /// The line under the section: only while a row is a bot that a teammate can ask.
    static func footer(for rows: [TeamBotRow]) -> String? {
        rows.contains { if case .answers = $0 { true } else { false } } ? askLine : nil
    }
}

/// The line under the name in an empty team chat.
enum TeamChatEmptyLine {
    /// One sentence, and a second one when the team has a bot that the person can ask (TeamMentions.bots). A direct
    /// chat has one sentence about its person.
    static func text(for room: TeamRoom, peer: TeamDirectPeer? = nil) -> String {
        if let peer { return "Messages here go only to \(peer.name)." }
        let line = "Messages here go to each person in \(room.name)."
        return TeamMentions.bots(of: room).isEmpty ? line : line + " Type @ to ask a bot."
    }
}
