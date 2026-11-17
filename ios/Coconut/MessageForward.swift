import SwiftUI

/// Forward: a held message goes to a new chat in any project, or to a chat that exists. A new chat is the message box
/// with the project chosen and the message quoted in it, and an old chat opens with the message quoted in its
/// composer, so the person can add a line before sending it.
enum MessageForward {
    /// The text that a forwarded message puts into the composer: where it came from, then the message as a quote.
    static func text(_ message: String, from source: String) -> String {
        let body = message.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.isEmpty ? ">" : "> " + $0 }
            .joined(separator: "\n")
        return "Forwarded from \(source):\n\(body)\n\n"
    }
}

extension AppStore {
    /// Puts a forwarded message into the composer of `botId`. The chat takes it when it opens.
    func forward(_ text: String, to botId: String) {
        forwardedDrafts[botId, default: ""] += text
    }
}

struct ForwardMessageSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    let message: Message
    /// The chat that the message is in. It is not offered.
    let fromBotId: String

    private var source: String {
        let bot = store.bots.first { $0.id == fromBotId }
        let agent = bot.map { $0.isChat ? $0.listName : $0.name } ?? "a chat"
        return message.role == "user" ? "my message to \(agent)" : agent
    }
    private var agents: [Bot] { store.bots.filter { !$0.isChat && !$0.archived } }
    /// The open chats that someone wrote in, newest first. The Coconut chat has one chat, so it is only here.
    private var chats: [Bot] {
        store.bots.filter { $0.id != fromBotId && AgentChats.isListed($0, latest: store.chatSummaries.newestActivity) }
            .sorted { (store.chatSummaries[$0.id].newestDate ?? .distantPast) > (store.chatSummaries[$1.id].newestDate ?? .distantPast) }
    }

    var body: some View {
        NavigationStack {
            List {
                Section("New chat") {
                    ForEach(agents.filter { !$0.coconut }) { agent in
                        row(agent, title: agent.name, detail: "New chat") {
                            if store.startChat(in: agent, text: MessageForward.text(message.text, from: source)) { dismiss() }
                        }
                    }
                }
                Section("Chats") {
                    ForEach(chats) { chat in
                        row(chat, title: chat.isChat ? chat.chatLabel : chat.name, detail: chat.isChat ? chat.name : nil) {
                            store.forward(MessageForward.text(message.text, from: source), to: chat.id)
                            dismiss()
                            if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
                        }
                    }
                }
            }
            .navigationTitle("Forward").inlineNavigationTitle()
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            #if os(macOS)
            .frame(width: 400, height: 520)
            #endif
        }
    }

    private func row(_ bot: Bot, title: String, detail: String?, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: BoardStyle.Space.m) {
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(title).lineLimit(1)
                    if let detail { Text(detail).font(.footnote).foregroundStyle(.secondary).lineLimit(1) }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("forward-to-\(bot.id)\(detail == "New chat" ? "-new" : "")")
    }
}
