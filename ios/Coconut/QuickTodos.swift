import SwiftUI

/// A quick to-do is a chat of an agent with `todo` set (backend agent_chats.py): one line of text starts a new chat of
/// the agent with that line as its first message, so the agent works on it at once. Its state follows the tasks of the
/// chat, a comment is a message in the chat, and archiving the chat checks the to-do off. Nothing else stores it.
@MainActor
enum QuickTodos {
    /// The agent of the person's last to-do on this device.
    static let agentKey = "todoAgent"
    /// A chat with no agent output and no step of a turn for this long takes a comment at once (commentMode).
    static let quietSeconds: TimeInterval = 180

    /// The projects that can take a to-do, Coconut, Gumroad and Personal, newest activity first. The Coconut chat of an older Mac
    /// is no project and has one chat, so it takes no to-do.
    static func choices(in bots: [Bot], latest: [String: Date]) -> [Bot] {
        ChatProject.choices(in: bots, latest: latest).filter(\.isProject)
    }

    /// The default assignee of a new to-do: the agent of the person's last to-do on this device, else the first pinned
    /// agent, else the agent with the newest activity. `choices` comes newest activity first.
    static func defaultAgent(last: String, in choices: [Bot]) -> Bot? {
        choices.first { $0.id == last } ?? choices.first(where: \.pinned) ?? choices.first
    }

    /// The open to-dos: the working ones first, then the rest, newest first.
    static func list(_ store: AppStore) -> [Bot] {
        store.bots.filter { $0.todo && !$0.archived }
            .map { ($0, state(of: $0, in: store)) }
            .sorted { first, second in
                let a = order.firstIndex(of: first.1) ?? order.count, b = order.firstIndex(of: second.1) ?? order.count
                return a != b ? a < b : (first.0.createdAt, first.0.id) > (second.0.createdAt, second.0.id)
            }
            .map(\.0)
    }

    static let order = ["waiting", "working", "blocked", "ready"]

    /// The state of a to-do from its chat: "waiting" while a card of the chat waits for the person, "working" while a
    /// turn of the chat runs or waits to run (and before the first one starts), "blocked" after a failed turn, and
    /// "ready" once the agent finished its turn.
    static func state(of chat: Bot, in store: AppStore) -> String {
        if store.pendingDecisions.contains(where: { store.conversationBotId(for: $0) == chat.id }) { return "waiting" }
        let tasks = store.tasks.filter { $0.chatBotId == chat.id }
        if let active = tasks.last(where: \.isActive) { return active.status == "needs_you" ? "waiting" : "working" }
        if store.outgoingMessages.contains(where: { $0.botId == chat.id && $0.expectsResponse }) { return "working" }
        guard let last = tasks.last else { return "working" }
        return last.status == "failed" ? "blocked" : "ready"
    }

    static func label(_ state: String) -> String {
        switch state {
        case "waiting": "Waits for you"
        case "working": "Working"
        case "blocked": "Failed"
        default: "Done"
        }
    }

    /// The text of the to-do: the first message of the person in the chat, else the title of the chat.
    static func text(of chat: Bot, in store: AppStore) -> String {
        messages(of: chat, in: store).first ?? chat.chatLabel
    }

    /// The comments of a to-do: the messages of the person in its chat after the first one, oldest first. A comment
    /// on its way shows too.
    static func comments(of chat: Bot, in store: AppStore) -> [String] {
        Array(messages(of: chat, in: store).dropFirst())
    }

    private static func messages(of chat: Bot, in store: AppStore) -> [String] {
        let saved = store.messages.filter { $0.botId == chat.id && $0.role == "user" }
        let confirmed = Set(saved.compactMap(\.clientRequestId))
        let sending = store.outgoingMessages.filter { $0.botId == chat.id && !confirmed.contains($0.id) && $0.status != .rejected }
        return (saved.map { ($0.createdAt, $0.text) } + sending.map { ($0.createdAt, $0.request.text) })
            .sorted { $0.0 < $1.0 }.map(\.1)
    }

    /// How a comment reaches the agent. A chat without a turn takes it as a new turn at once ("auto"). A turn that
    /// showed no output and no new step for `quietSeconds` gets it at once inside the turn ("steer"). A turn that is
    /// busy gets it in the queue of its task, after the current turn ("auto" routes it there).
    static func commentMode(chat: Bot, in store: AppStore, now: Date = Date()) -> (mode: String, taskId: String?) {
        guard let running = store.tasks.last(where: { $0.chatBotId == chat.id && $0.isRunning }) else { return ("auto", nil) }
        let output = store.messages.last { $0.botId == chat.id && $0.role != "user" }?.createdAt
        let activity = [output, running.phaseStartedAt, running.turnStartedAt, running.updatedAt]
            .compactMap { $0?.apiDate }.max() ?? .distantPast
        return now.timeIntervalSince(activity) >= quietSeconds ? ("steer", running.id) : ("auto", nil)
    }
}

/// A to-do that the person added and the Mac has not made yet. It shows at once, and stays with Retry when it fails.
struct PendingTodo: Identifiable, Equatable {
    let id = UUID()
    let text: String
    let agentId: String
    var failed = false
}

/// The to-dos that wait for their chat, and the sheets of the to-dos. They live in memory only: the chat holds the
/// to-do once the Mac made it.
@MainActor @Observable
final class PendingTodos {
    static let shared = PendingTodos()
    var items: [PendingTodo] = []
    /// True while the sheet of a new to-do shows.
    var adding = false
    /// The chat of the to-do whose comments show.
    var commenting: String?
}

extension AppStore {
    /// Adds a to-do: a new chat of `agent` whose first message is `text`, so the agent starts on it at once. The to-do
    /// shows at once (PendingTodos) and becomes the chat when the Mac made it. Returns false when it failed; the error
    /// notice says why and the to-do stays with Retry.
    @discardableResult
    func addTodo(_ text: String, agent: Bot, retrying: PendingTodo? = nil) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return false }
        let pending = PendingTodos.shared
        var item = retrying ?? PendingTodo(text: text, agentId: agent.id)
        item.failed = false
        if let index = pending.items.firstIndex(where: { $0.id == item.id }) { pending.items[index] = item }
        else { pending.items.insert(item, at: 0) }
        UserDefaults.standard.set(agent.id, forKey: QuickTodos.agentKey)
        guard var chat = await makeChat(of: agent, todo: true) else {
            if let index = pending.items.firstIndex(where: { $0.id == item.id }) { pending.items[index].failed = true }
            return false
        }
        // The Mac names the chat from its first message. The to-do shows its text before that.
        if let index = bots.firstIndex(where: { $0.id == chat.id }) {
            bots[index].todo = true
            if (bots[index].chatTitle ?? "").isEmpty { bots[index].chatTitle = AgentChats.title(from: text) }
            chat = bots[index]
        }
        pending.items.removeAll { $0.id == item.id }
        return await send(bot: chat, text: text)
    }

    /// A comment on a to-do: a message in its chat, at once or after the current turn (QuickTodos.commentMode).
    @discardableResult
    func commentOnTodo(_ chat: Bot, text: String) async -> Bool {
        let route = QuickTodos.commentMode(chat: chat, in: self)
        return await send(bot: chat, text: text, mode: route.mode, taskId: route.taskId)
    }
}

/// The sheet of a new to-do: one line of text, and the project that takes it. Return adds it.
struct NewTodoSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @AppStorage(QuickTodos.agentKey) private var lastAgent = ""
    @State private var chosen: String?
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let choices = QuickTodos.choices(in: store.bots, latest: latest)
        let agent = chosen.flatMap { id in choices.first { $0.id == id } } ?? QuickTodos.defaultAgent(last: lastAgent, in: choices)
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            TextField(agent == nil ? "No project yet" : "New to-do", text: $text)
                .font(.title3)
                .focused($focused)
                .submitLabel(.done)
                .onSubmit { add(agent) }
                .disabled(agent == nil)
                .accessibilityIdentifier("new-todo-input")
            HStack(spacing: BoardStyle.Space.s) {
                if let agent {
                    Menu {
                        Picker("Project", selection: Binding(get: { agent.id }, set: { chosen = $0 })) {
                            ForEach(choices) { Text($0.name).tag($0.id) }
                        }
                        .pickerStyle(.inline)
                    } label: {
                        ComposerChipLabel(title: agent.name)
                    }
                    .menuOrder(.fixed)
                    .fixedSize()
                    .accessibilityLabel("Project, \(agent.name)")
                    .accessibilityIdentifier("new-todo-agent")
                }
                Spacer(minLength: 0)
                Button("Add") { add(agent) }
                    .buttonStyle(.borderedProminent)
                    .disabled(agent == nil || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("new-todo-add")
            }
        }
        .padding(BoardStyle.Space.l)
        #if os(iOS)
        .presentationDetents([.height(150)])
        #else
        .frame(width: 420)
        #endif
        .onAppear { focused = true }
    }

    private func add(_ agent: Bot?) {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let agent, !line.isEmpty else { return }
        dismiss()
        Task { await store.addTodo(line, agent: agent) }
    }
}

/// The to-do with its comments, and a field for a new comment. A comment wakes the agent when its chat is quiet.
struct TodoCommentsSheet: View {
    let chatId: String
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.pushRoute) private var pushRoute
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        if let chat = store.bots.first(where: { $0.id == chatId }) {
            let comments = QuickTodos.comments(of: chat, in: store)
            let state = QuickTodos.state(of: chat, in: store)
            let agent = store.bots.first { $0.id == chat.agentId }?.name ?? chat.name
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                Text(QuickTodos.text(of: chat, in: store)).font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text("\(agent) · \(QuickTodos.label(state))").font(.subheadline).foregroundStyle(.secondary)
                ScrollView {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                        if comments.isEmpty {
                            Text("No comments yet.").font(.subheadline).foregroundStyle(.secondary)
                        }
                        ForEach(Array(comments.enumerated()), id: \.offset) { _, comment in
                            Text(comment)
                                .padding(BoardStyle.Space.s)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).fill(.quaternary))
                        }
                    }
                }
                .accessibilityIdentifier("todo-comments")
                HStack(spacing: BoardStyle.Space.s) {
                    TextField("Add a comment", text: $text)
                        .textFieldStyle(.roundedBorder)
                        .focused($focused)
                        .submitLabel(.send)
                        .onSubmit { comment(chat) }
                        .accessibilityIdentifier("todo-comment-input")
                    Button("Send") { comment(chat) }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityIdentifier("todo-comment-send")
                }
                HStack {
                    Button("Open chat") { dismiss(); pushRoute?(ConversationRoute(botId: chat.id)) }
                        .accessibilityIdentifier("todo-open-chat")
                    Spacer()
                    Button("Done", systemImage: "checkmark") { dismiss(); Task { await store.archiveBot(chat, archived: true) } }
                        .accessibilityIdentifier("todo-done")
                }
            }
            .padding(BoardStyle.Space.l)
            #if os(iOS)
            .presentationDetents([.medium, .large])
            #else
            .frame(width: 460, height: 420)
            #endif
        }
    }

    private func comment(_ chat: Bot) {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return }
        text = ""
        Task { await store.commentOnTodo(chat, text: line) }
    }
}

/// The rows of the quick to-dos, on Product of the iPhone and on Home of the Mac. A row opens the chat of the
/// to-do. Swipe or hold it to comment or to check it off.
struct QuickTodoRows: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @ScaledMetric(relativeTo: .title3) private var iconWidth = 24
    var inList = true

    var body: some View {
        let pending = PendingTodos.shared.items
        let todos = QuickTodos.list(store)
        Group {
            if !inList && pending.isEmpty && todos.isEmpty {
                BoardEmptyRow(text: "No to-dos. An agent starts on a new one at once.")
            }
            ForEach(pending) { item in
                let agent = store.bots.first { $0.id == item.agentId }
                row(symbol: item.failed ? "exclamationmark.circle" : "circle.dotted", title: item.text,
                    detail: (agent?.name ?? "") + (item.failed ? " · Not added. Tap to try again" : " · Adding"),
                    tint: item.failed ? BoardStyle.destructive : .secondary) {
                    if item.failed, let agent { Task { await store.addTodo(item.text, agent: agent, retrying: item) } }
                }
                .swipeActions(edge: .trailing) {
                    if item.failed {
                        Button("Remove", systemImage: "trash", role: .destructive) {
                            PendingTodos.shared.items.removeAll { $0.id == item.id }
                        }
                    }
                }
                .modifier(QuickTodoRowInset(inList: inList))
            }
            ForEach(todos) { chat in
                let state = QuickTodos.state(of: chat, in: store)
                let agent = store.bots.first { $0.id == chat.agentId }?.name ?? chat.name
                let comments = QuickTodos.comments(of: chat, in: store).count
                let detail = [agent, QuickTodos.label(state), comments == 0 ? nil : comments == 1 ? "1 comment" : "\(comments) comments"]
                    .compactMap { $0 }.joined(separator: " · ")
                row(symbol: AgentTodos.symbol(state), title: QuickTodos.text(of: chat, in: store), detail: detail,
                    tint: state == "blocked" ? BoardStyle.destructive : state == "working" ? BoardStyle.positive : .secondary) {
                    pushRoute?(ConversationRoute(botId: chat.id))
                }
                .contextMenu {
                    Button("Comment", systemImage: "text.bubble") { PendingTodos.shared.commenting = chat.id }
                    Button("Done", systemImage: "checkmark") { Task { await store.archiveBot(chat, archived: true) } }
                }
                .swipeActions(edge: .leading) {
                    Button("Comment", systemImage: "text.bubble") { PendingTodos.shared.commenting = chat.id }.tint(BoardStyle.husk)
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    Button("Done", systemImage: "checkmark") { Task { await store.archiveBot(chat, archived: true) } }.tint(.green)
                }
                .accessibilityIdentifier("todo-\(chat.id)")
                .modifier(QuickTodoRowInset(inList: inList))
            }
        }
        .sheet(item: Binding(get: { PendingTodos.shared.commenting.map(CommentTarget.init) },
                             set: { PendingTodos.shared.commenting = $0?.id })) {
            // A page that the sheet opens, from Open chat or a link in a comment, shows on the stack behind the sheet, so the
            // sheet closes first.
            TodoCommentsSheet(chatId: $0.id).environment(store).stackRoutes { route in
                PendingTodos.shared.commenting = nil
                pushRoute?(route)
            }
        }
    }

    private struct CommentTarget: Identifiable { let id: String }

    private func row(symbol: String, title: String, detail: String, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: symbol).font(.title3).foregroundStyle(tint).frame(width: iconWidth).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(title).font(.body).foregroundStyle(.primary).lineLimit(2)
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, inList ? BoardStyle.Space.m : BoardStyle.Space.xs)
            .padding(.horizontal, inList ? 0 : BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(inList ? BoardStyle.paper : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
    }
}

/// A row of the To-dos list has the side space of the list. On Home of the Mac the section gives it.
private struct QuickTodoRowInset: ViewModifier {
    let inList: Bool
    func body(content: Content) -> some View {
        if inList { content.homeRow() } else { content }
    }
}

/// The number on the Product tab: each thing on the Product page that asks for the person, once. The cards that wait,
/// the rows of Needs attention, the quick to-dos with the ones on their way, and the To-dos of the agents. The product
/// issues and the pull requests are the state of the product, not to-dos, so they add nothing (ProductWork). A late
/// card is a card that counts already, and stale data is a state of the connection, so neither adds to it. A blocked
/// To-do of an agent is also a row of "Blocked or failed", so it counts one time. The rows of Finance come from the
/// overview, which the root of the app loads for every tab and keeps for the next start (CoconutHome,
/// BusinessReport.overviewCache), so the number stays the same whichever tab shows.
enum TodosCount {
    static func count(cards: Int, attention: [AttentionItem], stuck: [ConversationRoute], quick: Int,
                      agentTodos: [Artifact]) -> Int {
        let rows = attention.filter { $0.kind != .waiting && $0.kind != .stale }.reduce(0) { $0 + $1.count }
        let flagged = Set(stuck.compactMap { $0.workContext?.id })
        return cards + rows + quick + agentTodos.filter { !flagged.contains($0.id) }.count
    }
}

extension AppStore {
    /// The badge of the Product tab (TodosCount).
    func todosCount(now: Date = Date()) -> Int {
        let input = attentionInput
        return TodosCount.count(cards: pendingCardCount, attention: AttentionRules.items(input, now: now),
                                stuck: AttentionRules.stuckWork(input, now: now),
                                quick: QuickTodos.list(self).count + PendingTodos.shared.items.count,
                                agentTodos: AgentTodos.make(artifacts: artifacts, bots: bots))
    }
}
