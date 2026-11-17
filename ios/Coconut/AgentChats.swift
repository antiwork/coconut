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

    /// The further chats of an agent that are not archived and that the person wrote in, newest activity first.
    /// `latest` is the date of the newest message of each bot.
    static func chats(of agentId: String, in bots: [Bot], latest: [String: Date]) -> [Bot] {
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return bots.filter { $0.chatOf == agentId && !$0.archived && !isBlank($0, latest: latest) }
            .sorted { (activity($0), $0.id) > (activity($1), $1.id) }
    }

    /// A chat that nobody wrote in: no message, no title (the first message names a chat), nothing unread and nothing
    /// running. No list shows it, and the next first message of its agent goes into it (spare), so a chat that the
    /// person never wrote in does not stay behind. The Coconut chat is the one place where Coconut changes, so it
    /// always shows.
    static func isBlank(_ bot: Bot, latest: [String: Date]) -> Bool {
        guard !bot.coconut, bot.unreadCount == 0, bot.health == nil, bot.status == "idle", latest[bot.id] == nil else { return false }
        return (bot.chatTitle ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && bot.lastMessage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// True when a list shows the chat: a chat that the person wrote in, a starred agent, or the Coconut chat.
    static func isListed(_ bot: Bot, latest: [String: Date]) -> Bool {
        !bot.archived && (bot.pinned || !isBlank(bot, latest: latest))
    }

    /// The chat of an agent that nobody wrote in, newest first. A first message goes into it, in place of a new chat.
    static func spare(of agentId: String, in bots: [Bot], latest: [String: Date]) -> Bot? {
        bots.filter { $0.chatOf == agentId && !$0.archived && isBlank($0, latest: latest) }
            .max { ($0.createdAt.apiDate ?? .distantPast, $0.id) < ($1.createdAt.apiDate ?? .distantPast, $1.id) }
    }

    /// The newest activity of each agent with its chats, so an agent keeps its place in a list by its newest chat.
    /// An archived chat does not count, and neither does a chat that nobody wrote in.
    static func activity(bots: [Bot], latest: [String: Date]) -> [String: Date] {
        var result = latest
        for chat in bots where chat.isChat && !chat.archived && !isBlank(chat, latest: latest) {
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

/// New project makes a project at once: a random letter that no project holds, as its face and its name. The first
/// message of the person names it.
enum NewAgent {
    /// A random free letter, or nil when all 26 letters have an agent.
    static func letter(bots: [Bot]) -> String? {
        let held = AgentLetter.taken(by: bots)
        return AgentLetter.all.filter { held[$0] == nil }.randomElement()
    }
}

/// What a way to start a chat asks of the message box: the project that it sends to, and the text that waits in it.
/// A request without a project keeps the project that the box shows.
struct NewChatRequest: Equatable {
    var project: String?
    var text = ""
}

extension AppStore {
    /// Every way to start a chat ends here: the New chat of a project page, of a chat, of a menu and of the Mac, and
    /// New project. The window shows the message box (Home) with the project chosen and the cursor in its field, ready
    /// to type. Nothing is asked of the Mac yet: the first message makes the chat (sendNewChat), so a chat that the
    /// person never writes in is never made. Returns false, with the reason in the error notice, when no chat can start.
    @discardableResult
    func startChat(in bot: Bot? = nil, text: String = "") -> Bool {
        var project: String?
        if let bot {
            guard let agent = bots.first(where: { $0.id == bot.agentId }) else {
                error = "This project is no longer on your Mac."
                return false
            }
            guard !agent.archived else {
                error = "Restore this project before starting a new chat."
                return false
            }
            project = agent.id
        }
        newChatRequest = NewChatRequest(project: project, text: text)
        chatStarts += 1
        return true
    }

    /// New project: makes a project with a random free letter, and starts a chat in it. Returns the project, or nil
    /// when the Mac did not make it; the error notice says why.
    @discardableResult
    func newProject() async -> Bot? {
        guard let letter = NewAgent.letter(bots: bots) else {
            error = "All 26 letters have a project. Delete a project to free its letter."
            return nil
        }
        guard let bot = await createBot(name: letter, role: "", avatar: AgentLetter.symbol(letter),
                                        color: AgentLetter.colors(letter).fill, instructions: "") else { return nil }
        // The update of the Mac brings the project too. The app shows it at once.
        if !bots.contains(where: { $0.id == bot.id }) { bots.append(bot) }
        startChat(in: bot)
        return bot
    }

    /// The chat for the first message of a new chat in `project`: the Coconut chat, which has one chat, or the chat of
    /// the project that nobody wrote in (the first chat of a new project, or a spare chat), else a new chat of the
    /// project. Returns nil, with the reason in the error notice, when the Mac did not make it.
    func chatForFirstMessage(in project: Bot) async -> Bot? {
        guard let agent = bots.first(where: { $0.id == project.agentId }) else {
            error = "This project is no longer on your Mac."
            return nil
        }
        guard !agent.archived else {
            error = "Restore this project before starting a new chat."
            return nil
        }
        let latest = chatSummaries.newestActivity
        if agent.coconut || AgentChats.isBlank(agent, latest: latest) { return agent }
        if let spare = AgentChats.spare(of: agent.id, in: bots, latest: latest) { return spare }
        return await makeChat(of: agent)
    }

    /// The Send of the message box: the first message of a new chat in `project`. Returns the chat that got the
    /// message, or nil when none did; the error notice says why.
    func sendNewChat(in project: Bot, text: String) async -> Bot? {
        guard let chat = await chatForFirstMessage(in: project) else { return nil }
        return await send(bot: chat, text: text) ? chat : nil
    }

    /// Asks the Mac for a new chat of an agent: a fresh conversation with the same Hermes profile. Returns the chat, or
    /// nil when the Mac did not make it.
    private func makeChat(of agent: Bot) async -> Bot? {
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
            chat = await createChat(of: agent.id)
        }
        guard let chat else { return nil }
        // The update of the Mac brings the chat too. The app shows it at once.
        if !bots.contains(where: { $0.id == chat.id }) { bots.append(chat) }
        return chat
    }

    /// The first message of the person names a chat. The Mac does this for a paired app; the demo does it here.
    func nameDemoChat(_ botId: String, from text: String) {
        guard isDemo, let index = bots.firstIndex(where: { $0.id == botId }), bots[index].isChat,
              (bots[index].chatTitle ?? "").isEmpty else { return }
        bots[index].chatTitle = AgentChats.title(from: text)
    }
}

/// The menu item "New chat" of a project or of one of its chats: the message box with that project chosen. The Coconut
/// chat has one chat, so it has no item.
struct NewChatButton: View {
    let bot: Bot
    /// Runs after the chat starts, for example to close the deck that the menu is in.
    var done: () -> Void = {}
    @Environment(AppStore.self) private var store

    var body: some View {
        if !bot.archived && !bot.coconut {
            // A Mac menu has titles only, as the other items of these menus. An iPhone menu shows the symbol too.
            #if os(macOS)
            Button("New chat", action: start).accessibilityIdentifier("new-chat-\(bot.id)")
            #else
            Button("New chat", systemImage: "square.and.pencil", action: start).accessibilityIdentifier("new-chat-\(bot.id)")
            #endif
        }
    }

    private func start() {
        if store.startChat(in: bot) { done() }
    }
}

/// The control in the header of a chat that starts a new chat in the same project: the message box with that project
/// chosen. On the Mac it sits next to the project's title, and the menu File has the same command with
/// Option-Command-N (Command-N makes a project, and Shift-Command-N belongs to notices).
struct NewChatHeaderButton: View {
    let bot: Bot
    @Environment(AppStore.self) private var store

    var body: some View {
        if !bot.archived && !bot.coconut {
            Button { store.startChat(in: bot) } label: {
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
            .help("New chat in \(bot.name) (Option-Command-N)")
            #endif
            .accessibilityLabel("New chat in \(bot.name)")
            .accessibilityIdentifier("new-chat")
        }
    }
}
