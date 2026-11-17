import SwiftUI

/// The project of the message box and of the Chats list. An empty project is all projects.
enum ChatProject {
    static let projectKey = "chatProject"
    static let lastKey = "chatProjectLast"

    /// The places that a chat can start in: the agents with the newest chat first, then the Coconut chat.
    static func choices(in bots: [Bot], latest: [String: Date]) -> [Bot] {
        let open = bots.filter { !$0.archived && !$0.isChat }
        let agents = open.filter { !$0.coconut }.sorted {
            (latest[$0.id] ?? $0.createdAt.apiDate ?? .distantPast) > (latest[$1.id] ?? $1.createdAt.apiDate ?? .distantPast)
        }
        return agents + open.filter(\.coconut)
    }

    /// The agent that a message goes to: the chosen project, else the last one used, else the first agent.
    static func target(project: String, last: String, in choices: [Bot]) -> Bot? {
        choices.first { $0.id == project } ?? choices.first { $0.id == last } ?? choices.first { !$0.coconut } ?? choices.first
    }

    /// A project that left the list no longer filters it.
    static func effective(_ project: String, in bots: [Bot]) -> String {
        bots.contains { $0.id == project && !$0.archived && !$0.isChat } ? project : ""
    }
}

#if os(iOS)
/// The message box at the bottom of Home and Chats, above the tab bar. A message starts a new chat with the agent of
/// the menu above the box, as Send does in a new chat. The same menu filters the Chats list.
struct NewChatBar: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @AppStorage(ChatProject.projectKey) private var chosen = ""
    @AppStorage(ChatProject.lastKey) private var last = ""
    @State private var text = ""
    @State private var sending = false
    @FocusState private var focused: Bool

    var body: some View {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let choices = ChatProject.choices(in: store.bots, latest: latest)
        let project = ChatProject.effective(chosen, in: store.bots)
        // Without an agent there is nobody to message, so the box stays out.
        if let target = ChatProject.target(project: project, last: last, in: choices) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                projectMenu(choices, project: project)
                field(target)
            }
            .padding(.horizontal, BoardStyle.Space.l).padding(.top, BoardStyle.Space.s).padding(.bottom, BoardStyle.Space.s)
            .background(.bar)
        }
    }

    private func projectMenu(_ choices: [Bot], project: String) -> some View {
        Menu {
            Button { chosen = "" } label: {
                if project.isEmpty { Label("All projects", systemImage: "checkmark") } else { Text("All projects") }
            }
            ForEach(choices) { agent in
                Button {
                    chosen = agent.id
                    last = agent.id
                } label: {
                    if project == agent.id { Label(agent.name, systemImage: "checkmark") } else { Text(agent.name) }
                }
            }
        } label: {
            HStack(spacing: 6) {
                if let shown = choices.first(where: { $0.id == project }) {
                    LiveBotAvatar(bot: shown, size: 20)
                    Text(shown.name).lineLimit(1)
                } else {
                    Image(systemName: "square.stack.3d.up.fill").font(.caption)
                    Text("All projects")
                }
                Image(systemName: "chevron.up.chevron.down").font(.caption2.weight(.bold))
            }
            .font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            .frame(minHeight: 28)
            .contentShape(Rectangle())
        }
        // The first item stays first, also when the menu opens above the button.
        .menuOrder(.fixed)
        .accessibilityLabel("Project").accessibilityIdentifier("new-chat-project")
    }

    private func field(_ target: Bot) -> some View {
        let canSend = !sending && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return HStack(alignment: .bottom, spacing: BoardStyle.Space.s) {
            TextField("Message \(target.name)", text: $text, axis: .vertical)
                .lineLimit(1...5)
                .focused($focused)
                .padding(.leading, 14).padding(.vertical, 10)
                .accessibilityIdentifier("new-chat-input")
            Button { send(to: target) } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 32))
                    .foregroundStyle(canSend ? Color.accentColor : Color.secondary.opacity(0.4))
            }
            .disabled(!canSend)
            .padding(.trailing, 4).padding(.bottom, 4)
            .accessibilityLabel("Send").accessibilityIdentifier("new-chat-send")
        }
        .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    private func send(to agent: Bot) {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, !sending else { return }
        sending = true
        text = ""
        focused = false
        last = agent.id
        Task {
            defer { sending = false }
            guard let chat = await store.newChat(with: agent) else {
                if text.isEmpty { text = message }
                return
            }
            pushRoute?(ConversationRoute(botId: chat.id))
            let sent = await store.send(bot: chat, text: message)
            if !sent, text.isEmpty { text = message }
        }
    }
}
#endif
