import Foundation

// The banners of the Mac app for a new team message and a new direct message. This file has each decision, and the
// Mac shell (mac/Sources/CoconutApp/MacBanners.swift) only gives them to macOS. The iPhone app posts none of them: the
// Mac sends it a push. The alert of that push has the same form (backend/coconut/push_relay.py, `team_alert` and
// `direct_alert`).

/// One banner: a new message of one chat of a team.
struct MessageBanner: Equatable, Sendable {
    let chat: TeamChatKey
    let title: String
    let subtitle: String?
    let body: String

    var identifier: String { MessageBanners.identifier(chat) }
    var thread: String { MessageBanners.thread(chat) }
}

/// What the Mac shell does after a list of the teams: the banners to post, the delivered banners to remove, and
/// whether macOS can ask the person for permission now.
struct MessageBannerPlan: Equatable, Sendable {
    var post: [MessageBanner] = []
    /// The identifiers of the banners to remove from Notification Center.
    var remove: [String] = []
    var asksPermission = false
}

/// The banners of the app that are in Notification Center, as far as the app knows. The app asks macOS to remove a
/// banner only when this set has it, so an equal list costs no call to macOS.
struct PostedBanners: Equatable, Sendable {
    var identifiers: Set<String> = []

    /// The set at a start of the app: the banners of a chat that macOS still has from an earlier run. It ignores each
    /// other identifier.
    init(delivered: [String] = []) {
        identifiers = Set(delivered.filter(MessageBanners.isIdentifier))
    }

    /// The removals of `plan` that the set has. The set then loses the removals and gets the posts of the plan.
    mutating func removals(for plan: MessageBannerPlan) -> [String] {
        let removals = plan.remove.filter(identifiers.contains)
        identifiers.subtract(removals)
        identifiers.formUnion(plan.post.map(\.identifier))
        return removals
    }
}

enum MessageBanners {
    /// The banners for the change from the list `before` to the list `after`.
    ///
    /// - A chat gets a banner only when its newest message is newer (a higher `first`) and is not a message of this
    ///   Mac's person. A chat that is new in the list starts at 0.
    /// - A new mention of this person in the Team chat or a named chat of an active team makes a banner: `mentionFirst` grew since the
    ///   list `before`. It needs no growth of the unread count, because `mention_first` counts unread messages only.
    ///   So a mention reaches the person also in a quiet team, as the push of the iPhone does, also when a plain
    ///   message came after it, and also when the count is at its limit.
    /// - Each other banner needs the unread count to grow, or to be at its limit (TeamRoom.maxUnread), where the
    ///   service stops the count. A Team chat also needs the switch "Notifications" of its team, and a named chat
    ///   also needs that the person did not mute it (TeamRoom.chatNotifies). A direct chat, also with a bot, needs an
    ///   active team only.
    /// - The first list after a start or a new pairing (`before` is nil) is history and makes no banner.
    /// - A chat has one banner, for its newest message, also after several new messages.
    /// - The banner of a chat with no unread message, and of a chat that left the list, goes away.
    /// - macOS asks for permission when the Mac has an active team.
    static func plan(before: TeamRooms?, after: TeamRooms) -> MessageBannerPlan {
        let chats = after.rooms.flatMap(chats(of:))
        var plan = MessageBannerPlan(asksPermission: after.rooms.contains { $0.status == .active })
        let current = Set(chats.map(\.key))
        let earlier = Dictionary((before?.rooms ?? []).flatMap(chats(of:)).map { ($0.key, $0) }) { first, _ in first }
        plan.remove = chats.filter { $0.unread == 0 }.map { identifier($0.key) }
            + earlier.keys.filter { !current.contains($0) }.sorted { identifier($0) < identifier($1) }.map(identifier)
        guard before != nil else { return plan }
        for chat in chats {
            guard let last = chat.last, !last.isOwn else { continue }
            let old = earlier[chat.key]
            guard last.first ?? 0 > old?.last?.first ?? 0 else { continue }
            let mention = chat.mentions && chat.mentionFirst > old?.mentionFirst ?? 0
            let counted = chat.unread > old?.unread ?? 0 || chat.unread >= TeamRoom.maxUnread
            guard mention || (chat.notifies && counted) else { continue }
            plan.post.append(banner(chat.key, room: chat.room, last: last))
        }
        return plan
    }

    /// The content of the banner of the newest message `last` of a chat of `room`. A team message: the writer (the bot
    /// for a message of a bot) over the team, or the team alone when the message has no name of its writer. A direct
    /// message: the other person or the bot, without a subtitle. The names are the ones of the chat rows.
    static func banner(_ chat: TeamChatKey, room: TeamRoom, last: TeamLastMessage) -> MessageBanner {
        let body = last.text.map(TranscriptPresentation.previewText) ?? ""
        if let peer = room.peer(of: chat) {
            return MessageBanner(chat: chat, title: peer.name, subtitle: nil, body: body)
        }
        // A named chat has its name and the name of the team in the place of the team, as the alert of the iPhone.
        let writer = last.writerName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if writer.isEmpty { return MessageBanner(chat: chat, title: room.chatAndTeam, subtitle: nil, body: body) }
        return MessageBanner(chat: chat, title: writer, subtitle: room.chatAndTeam, body: body)
    }

    /// The identifier of the banner of a chat. A newer banner of the chat takes the place of the older one.
    static func identifier(_ chat: TeamChatKey) -> String { identifierPrefix + thread(chat) }

    /// True for the identifier of the banner of a chat.
    static func isIdentifier(_ identifier: String) -> Bool { identifier.hasPrefix(identifierPrefix) }

    private static let identifierPrefix = "team-chat:"

    /// The thread of a chat, so macOS keeps the banners of one chat together. The push of the iPhone has the same one:
    /// for a direct chat with a bot, the entry ID of its `bot` entry.
    static func thread(_ chat: TeamChatKey) -> String {
        guard let other = chat.member ?? chat.botEntry ?? chat.chat else { return chat.room }
        return chat.room + ":" + other
    }

    /// The chat in the `userInfo` of a banner. The push of the iPhone names a direct chat with a person with the same
    /// keys. The key `team_bot` names the team bot ID of a direct chat with an agent; only the banners of the Mac use it.
    static func userInfo(_ chat: TeamChatKey) -> [String: String] {
        var info = ["team": chat.room]
        if let member = chat.member { info["member"] = member }
        if let bot = chat.bot { info["team_bot"] = bot }
        if let named = chat.chat { info["chat"] = named }
        return info
    }

    /// The chat of a banner from its `userInfo`, or nil for another notification.
    static func chat(userInfo: [AnyHashable: Any]) -> TeamChatKey? {
        guard let room = userInfo["team"] as? String, !room.isEmpty else { return nil }
        return TeamChatKey(room: room, member: userInfo["member"] as? String, chat: userInfo["chat"] as? String,
                           bot: userInfo["team_bot"] as? String)
    }

    /// The chat that a click on a banner shows: the chat while the list of the teams has it. Nil when the app has no
    /// list yet or no longer has the chat: the click then shows the window and nothing more.
    static func shownChat(_ chat: TeamChatKey?, rooms: TeamRooms?) -> TeamChatKey? {
        guard let chat, let room = rooms?.rooms.first(where: { $0.room == chat.room })?.inChat(chat.chat) else {
            return nil
        }
        if let member = chat.member { return room.directList.contains { $0.member == member } ? chat : nil }
        if let bot = chat.bot { return room.botDirectList.contains { $0.bot == bot } ? chat : nil }
        return chat
    }

    /// One chat of a room in the list: the Team chat, a named chat, a direct chat, or a direct chat with a bot.
    private struct Chat {
        let key: TeamChatKey
        let room: TeamRoom
        let unread: Int
        let last: TeamLastMessage?
        /// The chat notifies each new message.
        let notifies: Bool
        /// The chat notifies a new mention of this person: a Team chat of an active team.
        var mentions = false
        /// The `first` of the newest unread message that names this person, or 0.
        var mentionFirst = 0
    }

    private static func chats(of room: TeamRoom) -> [Chat] {
        // The Team chat and each named chat follow the switch of the team, and a muted named chat notifies only a
        // mention (TeamRoom.chatNotifies).
        let team = ([room] + room.namedChatRooms).map {
            Chat(key: $0.chatKey, room: $0, unread: $0.unread ?? 0, last: $0.lastMessage, notifies: $0.chatNotifies,
                 mentions: $0.status == .active, mentionFirst: $0.mentionFirst ?? 0)
        }
        return team + room.directList.map {
            Chat(key: TeamChatKey(room: room.room, member: $0.member), room: room, unread: $0.unread ?? 0,
                 last: $0.lastMessage, notifies: room.status == .active)
        } + room.botDirectList.map {
            Chat(key: TeamChatKey(room: room.room, bot: $0.bot), room: room, unread: $0.unread ?? 0,
                 last: $0.lastMessage, notifies: room.status == .active)
        }
    }
}
