import SwiftUI

// An agent can have more chats. Each chat is a bot with `chatOf`, the ID of its agent: a fresh conversation with the
// same Hermes profile. The lists show the chats under their agent, newest first, with the first message as the title.

extension Bot {
    /// True for a further chat of an agent. The agent itself is the first chat.
    var isChat: Bool { chatOf != nil }

    /// The agent of this bot: the bot itself, or the agent of a chat. The settings and the pages belong to it.
    var agentId: String { chatOf ?? id }

    /// The title of a further chat in a list: its first message, or "New chat" before the person wrote one.
    var chatLabel: String {
        let title = (chatTitle ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? AgentChats.untitled : title
    }

    /// The name of a row that stands alone, for example in "Archived": the agent, and the title of a further chat.
    var listName: String { isChat ? name + " · " + chatLabel : name }
}

enum AgentChats {
    static let untitled = "New chat"
    /// A title from the first message of a chat, as the Mac makes it: the first line with text, at most 72 characters.
    static let titleLength = 72

    static func title(from text: String) -> String {
        let line = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty } ?? ""
        guard line.count > titleLength else { return line }
        return line.prefix(titleLength - 1).trimmingCharacters(in: .whitespaces) + "…"
    }

    /// The further chats of an agent that are not archived, newest activity first. `latest` is the date of the newest
    /// message of each bot. A chat without a message counts from its start, so a new chat comes first.
    static func chats(of agentId: String, in bots: [Bot], latest: [String: Date]) -> [Bot] {
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return bots.filter { $0.chatOf == agentId && !$0.archived }
            .sorted { (activity($0), $0.id) > (activity($1), $1.id) }
    }

    /// The newest activity of each agent with its chats, so an agent keeps its place in a list by its newest chat.
    /// An archived chat does not count.
    static func activity(bots: [Bot], latest: [String: Date]) -> [String: Date] {
        var result = latest
        for chat in bots where chat.isChat && !chat.archived {
            let date = latest[chat.id] ?? chat.createdAt.apiDate ?? .distantPast
            if let agent = chat.chatOf, date > (result[agent] ?? .distantPast) { result[agent] = date }
        }
        return result
    }

    /// Each unread chat stands alone, including a further chat whose agent has no unread messages.
    static func unread(in bots: [Bot], latest: [String: Date]) -> [Bot] {
        let active = Set(bots.filter { !$0.archived && !$0.isChat }.map(\.id))
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return bots.filter { !$0.archived && $0.unreadCount > 0 && active.contains($0.agentId) }
            .sorted { (activity($0), $0.id) > (activity($1), $1.id) }
    }

    /// The agent of a bot ID: the ID itself for an agent or an unknown bot.
    static func agentId(of botId: String, in bots: [Bot]) -> String {
        bots.first { $0.id == botId }?.agentId ?? botId
    }
}

extension Array where Element == Bot {
    /// The agents without the Coconut chat, which has its own place at the top, and without the further chats, which
    /// show under their agent.
    var agents: [Bot] { filter { !$0.coconut && !$0.isChat } }

    /// The bots that the person can mention or choose: each agent one time.
    var withoutChats: [Bot] { filter { !$0.isChat } }
}

extension AppStore {
    /// Starts a new chat with the agent of `bot`: a fresh conversation with the same Hermes profile. Returns the chat,
    /// or nil when the Mac did not make it. The chat that opens next puts the cursor in its message field.
    func newChat(with bot: Bot) async -> Bot? {
        let agent = bots.first { $0.id == bot.agentId } ?? bot
        var chat: Bot?
        if isDemo {
            var copy = agent
            copy.id = UUID().uuidString
            copy.sessionId = UUID().uuidString
            copy.chatOf = agent.id
            copy.chatTitle = ""
            copy.coconut = false
            copy.status = "idle"
            copy.lastMessage = ""
            copy.unreadCount = 0
            copy.archived = false
            copy.pinned = false
            copy.health = nil
            copy.createdAt = ISO8601DateFormatter().string(from: Date())
            chat = copy
        } else {
            chat = await startChat(agent.id)
        }
        guard let chat else { return nil }
        // The update of the Mac brings the chat too. The app shows it at once.
        if !bots.contains(where: { $0.id == chat.id }) { bots.append(chat) }
        composerFocusRequest = chat.id
        return chat
    }

    /// The first message of the person names a chat. The Mac does this for a paired app; the demo does it here.
    func nameDemoChat(_ botId: String, from text: String) {
        guard isDemo, let index = bots.firstIndex(where: { $0.id == botId }), bots[index].isChat,
              (bots[index].chatTitle ?? "").isEmpty else { return }
        bots[index].chatTitle = AgentChats.title(from: text)
    }
}

/// The menu item "New Chat" of an agent or of one of its chats.
struct NewChatButton: View {
    let bot: Bot
    let open: (Bot) -> Void
    @Environment(AppStore.self) private var store

    var body: some View {
        // A Mac menu has titles only, as the other items of these menus. An iPhone menu shows the symbol too.
        #if os(macOS)
        Button("New Chat", action: start).accessibilityIdentifier("new-chat-\(bot.id)")
        #else
        Button("New Chat", systemImage: "square.and.pencil", action: start).accessibilityIdentifier("new-chat-\(bot.id)")
        #endif
    }

    private func start() {
        Task { if let chat = await store.newChat(with: bot) { open(chat) } }
    }
}

/// The control in the header of a chat that starts a new chat with the same agent. On the Mac it sits next to the
/// agent's title and takes Option-Command-N: Command-N makes an agent, and Shift-Command-N belongs to notices.
struct NewChatHeaderButton: View {
    let bot: Bot
    @Environment(AppStore.self) private var store
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute

    var body: some View {
        if !bot.archived && !bot.coconut {
            Button {
                Task {
                    guard let chat = await store.newChat(with: bot) else { return }
                    if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
                }
            } label: {
                #if os(macOS)
                Image(systemName: "square.and.pencil").font(.body.weight(.medium))
                    .frame(width: 34, height: 34)
                    .modifier(FloatingCapsule())
                    .contentShape(Circle())
                #else
                Image(systemName: "square.and.pencil")
                #endif
            }
            #if os(macOS)
            .keyboardShortcut("n", modifiers: [.command, .option])
            .help("New chat with \(bot.name) (Option-Command-N)")
            #endif
            .accessibilityLabel("New chat with \(bot.name)")
            .accessibilityIdentifier("new-chat")
        }
    }
}
