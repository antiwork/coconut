import SwiftUI

/// The project of the message box. The box sends to one project and always names it.
enum ChatProject {
    static let projectKey = "chatProject"

    /// The places that a chat can start in: the projects with the newest chat first, then the Coconut chat. An archived
    /// project is not one of them.
    static func choices(in bots: [Bot], latest: [String: Date]) -> [Bot] {
        let open = bots.filter { !$0.archived && !$0.isChat }
        let agents = open.filter { !$0.coconut }.sorted {
            (latest[$0.id] ?? $0.createdAt.apiDate ?? .distantPast) > (latest[$1.id] ?? $1.createdAt.apiDate ?? .distantPast)
        }
        return agents + open.filter(\.coconut)
    }

    /// The project that the box sends to: the chosen one while it is in the list, else the newest. Never nil while a
    /// project exists.
    static func target(chosen: String, in choices: [Bot]) -> Bot? {
        choices.first { $0.id == chosen } ?? choices.first
    }
}

/// The message box of Home, in the iPhone and in the Mac window, as the box of the Codex app: always there, with the
/// cursor ready. The chip under the text names the project that the message goes to and lists the projects, newest
/// first, then the Coconut chat. It ends with the list of all projects (on the iPhone) and New project. The first message
/// starts a new chat in that project and opens it. The same box is where every New chat leads (AppStore.startChat).
struct NewChatBar: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    @AppStorage(ChatProject.projectKey) private var chosen = ""
    @State private var text = ComposerDrafts.read(NewChatBar.draftKey)
    @State private var sending = false
    /// A new request puts the cursor in the field when the page is settled: a pop of the stack takes a moment.
    @State private var focusRequest: UUID?
    @FocusState private var focused: Bool

    /// The unsent text of the box stays when the app closes, as the text of each chat.
    private static let draftKey = "new-chat"

    var body: some View {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let choices = ChatProject.choices(in: store.bots, latest: latest)
        let target = ChatProject.target(chosen: chosen, in: choices)
        VStack(spacing: 0) {
            box(target, choices: choices)
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.top, BoardStyle.Space.xs).padding(.bottom, BoardStyle.Space.s)
        .frame(maxWidth: 760)
        .frame(maxWidth: .infinity)
        .modifier(ComposerBackdrop())
        // The choice stays what the chip shows: a project that left the list, or a first start, gives the newest
        // project, and the newest project moving on does not move the box under the person who types.
        .onChange(of: target?.id, initial: true) { _, id in
            if let id, id != chosen { chosen = id }
        }
        .onChange(of: store.newChatRequest, initial: true) { _, request in apply(request) }
        .onChange(of: text) { _, draft in ComposerDrafts.write(draft, chat: Self.draftKey) }
        .task(id: focusRequest) {
            guard focusRequest != nil else { return }
            try? await Task.sleep(for: Self.focusDelay)
            guard !Task.isCancelled else { return }
            focused = true
        }
        #if os(macOS)
        // A click on Home puts the cursor in the box, as a click on a chat puts it in the field of the chat.
        .task {
            try? await Task.sleep(for: .milliseconds(150))
            guard !Task.isCancelled else { return }
            focused = true
        }
        #endif
    }

    #if os(macOS)
    private static let focusDelay = Duration.milliseconds(150)
    #else
    private static let focusDelay = Duration.milliseconds(450)
    #endif

    /// The text on top, and under it the chip with the project and Send, as the box of a chat that opens.
    private func box(_ target: Bot?, choices: [Bot]) -> some View {
        let shape = RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)
        let canSend = target != nil && !sending && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return VStack(alignment: .leading, spacing: 0) {
            TextField(target.map { "New chat with \($0.name)" } ?? "Create a project to start a chat", text: $text, axis: .vertical)
                .focused($focused)
                .modifier(ComposerFieldStyle(send: { if let target { send(to: target) } }, stacked: true))
                .padding(.horizontal, BoardStyle.Space.m)
                .disabled(target == nil)
                .accessibilityIdentifier("new-chat-input")
            HStack(spacing: 0) {
                projectChip(target, choices: choices)
                Spacer(minLength: 0)
                ComposerSendButton(symbol: "arrow.up", enabled: canSend, label: "Send",
                                   action: { if let target { send(to: target) } }, identifier: "new-chat-send")
            }
            .padding(.horizontal, BoardStyle.Space.xs)
        }
        .modifier(ComposerClickArea(shape: shape) { focused = true })
        .modifier(ComposerSurface(shape: shape))
        .modifier(ComposerGlassGroup())
    }

    /// The project of the box, as a chip that opens the list of projects. Without a project, the chip makes the first.
    @ViewBuilder
    private func projectChip(_ target: Bot?, choices: [Bot]) -> some View {
        if let target {
            Menu {
                Picker("Project", selection: Binding(get: { target.id }, set: { chosen = $0 })) {
                    ForEach(choices) { project in Text(project.name).tag(project.id) }
                }
                .pickerStyle(.inline)
                Divider()
                #if os(iOS)
                // The page of the projects: the live work, the pages, the spend and the archive. The Mac sidebar has it.
                Button("All projects", systemImage: "list.bullet") { pushRoute?(AgentListRoute()) }
                    .accessibilityIdentifier("new-chat-all-projects")
                #endif
                newProjectButton
            } label: {
                HStack(spacing: BoardStyle.Space.xs) {
                    Text(target.name).lineLimit(1)
                    Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                }
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .padding(.horizontal, BoardStyle.Space.s)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .modifier(ComposerChipMenuStyle())
            // The first item stays first, also when the menu opens above the chip.
            .menuOrder(.fixed)
            .accessibilityLabel("Project, \(target.name)")
            .accessibilityIdentifier("new-chat-project")
        } else {
            Button { Task { await store.newProject() } } label: {
                ComposerChipLabel(symbol: "plus", title: "New project")
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("new-chat-project")
        }
    }

    private var newProjectButton: some View {
        Button("New project", systemImage: "plus") { Task { await store.newProject() } }
            .accessibilityIdentifier("new-chat-new-project")
    }

    /// A start of a chat names the project and brings text, for example a forwarded message. The text joins what the
    /// person typed already, so nothing typed goes away.
    private func apply(_ request: NewChatRequest?) {
        guard let request else { return }
        store.newChatRequest = nil
        if let project = request.project { chosen = project }
        if !request.text.isEmpty { text += (text.isEmpty ? "" : "\n\n") + request.text }
        focusRequest = UUID()
    }

    /// Sends the text as the first message of a new chat in the project and opens the chat. When the Mac does not make
    /// the chat, the notice says why and the text comes back.
    private func send(to project: Bot) {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, !sending else { return }
        sending = true
        text = ""
        focused = false
        Task {
            defer { sending = false }
            guard let chat = await store.sendNewChat(in: project, text: message) else {
                if text.isEmpty { text = message }
                return
            }
            if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
        }
    }
}
