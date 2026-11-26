import Foundation

// The banners of the Mac app for the agents: a new card, and a new reply in a chat with an agent. This file has each
// decision, and the Mac shell (mac/Sources/CoconutApp/MacBanners.swift) only gives them to macOS. The iPhone app posts
// none of them: the Mac sends it a push, with the same title and subtitle (backend/coconut/push_relay.py,
// `notification_alert`).

/// What the banners of the agents read of the state: the pending cards and the chats with an agent.
struct AgentActivity: Equatable, Sendable {
    struct Card: Equatable, Sendable {
        let id: String
        let bot: String
        /// The name of the chat of the card, as its row in the list.
        let agent: String
        let title: String
        /// The agent marked the card urgent (Decision.needsAttentionNow).
        let urgent: Bool
    }

    struct Chat: Equatable, Sendable {
        let bot: String
        let name: String
        let unread: Int
        let last: String
        /// The person did not mute the chat (Bot.notifications).
        let notifies: Bool
    }

    var cards: [Card] = []
    var chats: [Chat] = []

    /// A card waits for the person while its status is pending, also while its sources refresh.
    init(bots: [Bot], decisions: [Decision]) {
        let names = Dictionary(bots.map { ($0.id, $0.listName) }) { first, _ in first }
        cards = decisions.filter { $0.status == "pending" }.map {
            Card(id: $0.id, bot: $0.botId, agent: names[$0.botId] ?? "Coconut", title: $0.title,
                 urgent: $0.needsAttentionNow == true)
        }
        chats = bots.filter { !$0.archived }.map {
            Chat(bot: $0.id, name: $0.listName, unread: $0.unreadCount, last: $0.lastMessage, notifies: $0.notifications)
        }
    }
}

/// One banner of an agent: a new card, or the newest reply of a chat.
struct AgentBanner: Equatable, Sendable {
    let identifier: String
    let bot: String
    let title: String
    let subtitle: String?
    let body: String

    var thread: String { AgentBanners.thread(bot) }
}

/// The banners to post and the delivered banners to remove after a change of the state.
struct AgentBannerPlan: Equatable, Sendable {
    var post: [AgentBanner] = []
    var remove: [String] = []
}

enum AgentBanners {
    /// The banners for the change from the state `before` to the state `after`.
    ///
    /// - A card that is new and pending gets a banner. The banner goes away when the card is no longer pending.
    /// - A chat gets a banner when its unread count grows: a new reply. A chat that is new in the list starts at 0. The
    ///   banner goes away when the chat has no unread reply. A chat has one banner, for its newest reply.
    /// - A muted chat posts its cards and no reply, as the push of the iPhone (push.py, `record`).
    /// - The first state after a start, and a state that the store cleared, make no banner and remove none.
    /// - The notification settings decide last (NotificationSettings.notifiesCard and notifiesReply).
    static func plan(before: AgentActivity?, after: AgentActivity, settings: NotificationSettings = .standard,
                     now: Date = Date()) -> AgentBannerPlan {
        guard !after.chats.isEmpty else { return AgentBannerPlan() }
        var plan = AgentBannerPlan()
        let pending = Set(after.cards.map(\.id))
        plan.remove = (before?.cards ?? []).filter { !pending.contains($0.id) }.map { Self.card($0.id) }
            + after.chats.filter { $0.unread == 0 }.map { Self.chat($0.bot) }
        guard let before, !before.chats.isEmpty else { return plan }
        let known = Set(before.cards.map(\.id))
        for card in after.cards where !known.contains(card.id) && settings.notifiesCard(urgent: card.urgent, at: now) {
            plan.post.append(AgentBanner(identifier: Self.card(card.id), bot: card.bot, title: card.agent,
                                         subtitle: "Needs your decision", body: card.title))
        }
        let unread = Dictionary(before.chats.map { ($0.bot, $0.unread) }) { first, _ in first }
        for chat in after.chats where chat.notifies && chat.unread > unread[chat.bot] ?? 0 {
            guard settings.notifiesReply(at: now) else { break }
            plan.post.append(AgentBanner(identifier: Self.chat(chat.bot), bot: chat.bot, title: chat.name, subtitle: nil,
                                         body: TranscriptPresentation.previewText(chat.last)))
        }
        return plan
    }

    /// The identifier of the banner of a card.
    static func card(_ id: String) -> String { cardPrefix + id }

    /// The identifier of the banner of a chat. A newer reply takes the place of the older one.
    static func chat(_ bot: String) -> String { chatPrefix + bot }

    static func isIdentifier(_ identifier: String) -> Bool {
        identifier.hasPrefix(cardPrefix) || identifier.hasPrefix(chatPrefix)
    }

    private static let cardPrefix = "agent-card:"
    private static let chatPrefix = "agent-chat:"

    /// The thread of a chat, so macOS keeps its cards and replies together.
    static func thread(_ bot: String) -> String { "agent:" + bot }

    static func userInfo(_ bot: String) -> [String: String] { ["agent_bot": bot] }

    /// The chat of a banner from its `userInfo`, or nil for another notification.
    static func bot(userInfo: [AnyHashable: Any]) -> String? {
        guard let bot = userInfo["agent_bot"] as? String, !bot.isEmpty else { return nil }
        return bot
    }
}
