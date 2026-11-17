import SwiftUI

/// The page of one agent.
struct AgentRoute: Hashable {
    let botId: String
}

/// Every page of every agent in one list. The agents list links to it.
struct AllPagesRoute: Hashable {}

/// The sections of the page of an agent, in the order of the page. Each one shows always, so each agent reads the
/// same way. A section without rows has one quiet line.
enum AgentSection: String, CaseIterable, Identifiable {
    case chat, todos, pages, routines, settings

    var id: String { rawValue }
    var title: String {
        switch self {
        case .chat: "Chat"
        case .todos: "To-dos"
        case .pages: "Pages"
        case .routines: "Routines"
        case .settings: "Settings"
        }
    }
}

/// An agent as a project: the agent with its state, its open work, its pages and its routines. The agents list and the
/// page of an agent read it. It reads only its arguments, so a test can make one.
struct AgentProject: Identifiable, Equatable {
    /// What the agent does now. One state shows, in this order of importance.
    enum Status: Equatable {
        case archived
        /// A provider limit or a provider error, with its label.
        case cannotRun(String)
        case needsYou
        case working
        case idle

        var label: String {
            switch self {
            case .archived: "Archived"
            case .cannotRun(let label): label
            case .needsYou: "Needs you"
            case .working: "Working"
            case .idle: "Idle"
            }
        }

        /// The state in two words at most, for a row of the list. The page of the agent shows the whole label.
        var shortLabel: String {
            if case .cannotRun = self { return "Can't run" }
            return label
        }

        var symbol: String {
            switch self {
            case .archived: "archivebox"
            case .cannotRun: "bolt.slash.fill"
            case .needsYou: "circle.inset.filled"
            case .working: "circle.dotted"
            case .idle: "moon"
            }
        }

        var tint: Color {
            switch self {
            case .cannotRun: BoardStyle.destructive
            case .needsYou: BoardStyle.attention
            case .working: BoardStyle.positive
            case .archived, .idle: .secondary
            }
        }
    }

    let bot: Bot
    let status: Status
    /// The open work of the agent in the order of AgentTodos: working, blocked, waiting, ready.
    let todos: [Artifact]
    /// The pages of the agent: the starred pages first, then the newest update first.
    let pages: [BotDocument]
    /// The routines that repeat, and the one-time checks that still run (RoutineGroups).
    let routines: [Routine]
    let checks: [Routine]
    /// The cards of the agent that wait for an answer.
    let pendingCards: Int
    /// The time of the newest message of the agent's chats. Nil for an agent without a message.
    let lastActivity: Date?
    /// The further chats of the agent that are not archived, newest activity first (AgentChats).
    let chats: [Bot]

    var id: String { bot.id }

    /// What the store knows, for one agent or for the list.
    struct Input {
        var bots: [Bot] = []
        var artifacts: [Artifact] = []
        var documents: [BotDocument] = []
        var routines: [Routine] = []
        var decisions: [Decision] = []
        var answering: Set<String> = []
        var tasks: [AgentTask] = []
        /// The date of the newest message of each agent (ChatList.lastMessageDates).
        var latest: [String: Date] = [:]
    }

    init(bot: Bot, input: Input, now: Date = Date()) {
        self.bot = bot
        chats = AgentChats.chats(of: bot.id, in: input.bots, latest: input.latest)
        // The work, the cards and the state of the further chats are those of the agent.
        let ids = Set([bot.id] + chats.map(\.id))
        todos = AgentTodos.make(artifacts: input.artifacts.filter { ids.contains($0.botId) }, bots: [bot] + chats)
        pages = input.documents.filter { $0.botId == bot.id }
            .sorted { ($0.isPinned ? 1 : 0, $0.updatedAt, $1.id) > ($1.isPinned ? 1 : 0, $1.updatedAt, $0.id) }
        let groups = RoutineGroups(input.routines.filter { $0.botId == bot.id }, now: now)
        routines = groups.recurring
        checks = groups.scheduledChecks
        let chatOf = Dictionary(input.tasks.map { ($0.id, $0.chatBotId) }) { _, latest in latest }
        pendingCards = input.decisions.filter {
            $0.isPending && !input.answering.contains($0.id) && ids.contains(chatOf[$0.taskId] ?? $0.botId)
        }.count
        lastActivity = ([bot] + chats).compactMap { input.latest[$0.id] }.max()
        let runs = ["working", "running"].contains(bot.status)
            || chats.contains { ["working", "running"].contains($0.status) }
            || input.tasks.contains { ids.contains($0.botId) && $0.isRunning }
            || todos.contains { $0.status == "working" }
        if bot.archived { status = .archived }
        else if let health = bot.health { status = .cannotRun(health.label) }
        else if pendingCards > 0 { status = .needsYou }
        else if runs { status = .working }
        else { status = .idle }
    }

    /// The agents as projects: each agent that is not archived and is not the Coconut chat, newest activity first. An
    /// agent without a message sorts by the time when the person made it.
    static func list(_ input: Input, now: Date = Date()) -> [AgentProject] {
        let latest = AgentChats.activity(bots: input.bots, latest: input.latest)
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return input.bots.agents.filter { !$0.archived }
            .sorted { (activity($0), $1.id) > (activity($1), $0.id) }
            .map { AgentProject(bot: $0, input: input, now: now) }
    }

    /// The quiet line of a row of the list: "2 To-dos · 3 pages · 1 routine". Nil when the agent has none of them.
    var summary: String? {
        func part(_ count: Int, _ one: String, _ many: String) -> String? {
            count == 0 ? nil : "\(count) \(count == 1 ? one : many)"
        }
        let parts = [part(todos.count, "To-do", "To-dos"), part(pages.count, "page", "pages"),
                     part(routines.count + checks.count, "routine", "routines")].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// The count after the title of a section. Chat counts the unread messages. Settings has none.
    func count(_ section: AgentSection) -> Int {
        switch section {
        case .chat: bot.unreadCount + chats.reduce(0) { $0 + $1.unreadCount }
        case .todos: todos.count
        case .pages: pages.count
        case .routines: routines.count + checks.count
        case .settings: 0
        }
    }
}

extension AppStore {
    /// What the agents list and the page of an agent read from the store.
    var agentProjectInput: AgentProject.Input {
        AgentProject.Input(bots: bots, artifacts: artifacts, documents: documents ?? [], routines: routines,
                           decisions: decisions, answering: answeringDecisionIds, tasks: tasks,
                           latest: chatSummaries.newestActivity)
    }
}

/// The Agents tab: each agent as a project, with its role, its state, its open work and its last activity.
struct AgentsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openChat) private var openChat
    /// Opens the page of an agent. The Mac window selects it; without it the page goes onto the stack.
    var open: ((String) -> Void)? = nil
    var create: (() -> Void)? = nil

    var body: some View {
        let projects = AgentProject.list(store.agentProjectInput)
        let archived = store.bots.agents.filter(\.archived).count
        BoardPage {
            HStack(alignment: .firstTextBaseline) {
                BoardPageTitle(title: "Agents", detail: Self.detail(projects))
                Spacer(minLength: 0)
                if let create {
                    Button(action: create) { Label("New agent", systemImage: "plus") }
                        .accessibilityIdentifier("agents-new")
                }
            }
            if projects.isEmpty {
                QuietEmptyState(symbol: "person.crop.square.badge.plus", title: "No agents yet",
                                message: "An agent is a project: a chat, its work, its pages and its routines.")
            } else {
                BoardSection("Projects", count: projects.count) {
                    ForEach(projects) { project in
                        AgentProjectRow(project: project)
                            .boardButton("agent-" + project.id) { show(project.id) }
                            .contextMenu {
                                NewChatButton(bot: project.bot) { chat in
                                    if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
                                }
                            }
                    }
                }
            }
            BoardSection("More") {
                BoardRow(symbol: "rectangle.3.group", title: "All pages",
                         value: store.documents.map { "\($0.count)" })
                    .boardButton("agents-all-pages") { pushRoute?(AllPagesRoute()) }
                BoardRow(symbol: "chart.bar", title: "Usage", detail: "Turns, tokens, cost and time of each agent")
                    .boardButton("agents-usage") { pushRoute?(UsageRoute()) }
                if archived > 0 {
                    BoardRow(symbol: "archivebox", title: "Archived", value: "\(archived)")
                        .boardButton("agents-archived") { pushRoute?(ArchivedAgentsRoute()) }
                }
            }
        }
        // The page counts need the list of the pages, also before any page opens.
        .task(id: store.connection == .connected) {
            if store.documents == nil { await store.loadDocuments(quiet: true) }
        }
        .refreshable { await store.reconnect() }
    }

    private func show(_ id: String) {
        if let open { open(id) } else { pushRoute?(AgentRoute(botId: id)) }
    }

    /// "5 agents · 2 working · 1 needs you".
    static func detail(_ projects: [AgentProject]) -> String {
        let working = projects.filter { $0.status == .working }.count
        let waiting = projects.filter { $0.status == .needsYou }.count
        let halted = projects.filter { if case .cannotRun = $0.status { true } else { false } }.count
        return (["\(projects.count) \(projects.count == 1 ? "agent" : "agents")"]
            + (working > 0 ? ["\(working) working"] : [])
            + (waiting > 0 ? ["\(waiting) \(waiting == 1 ? "needs" : "need") you"] : [])
            + (halted > 0 ? ["\(halted) can't run"] : [])).joined(separator: " · ")
    }
}

/// One agent in the list: its icon, its name and role, its state, its open work, and its last activity.
struct AgentProjectRow: View {
    let project: AgentProject

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            LiveBotAvatar(bot: project.bot, size: 36)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                HStack(spacing: BoardStyle.Space.s) {
                    Text(project.bot.name).font(.body.weight(.semibold)).lineLimit(1)
                    if project.bot.unreadCount > 0 { UnreadBadge(count: project.bot.unreadCount) }
                }
                Text(Self.line(project)).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: BoardStyle.Space.s)
            VStack(alignment: .trailing, spacing: BoardStyle.Space.line) {
                AgentStatusLabel(status: project.status, short: true)
                if let date = project.lastActivity {
                    CompactAgeLabel(date: date).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// The role of the agent, then what it has: "Plans the launch · 2 To-dos · 3 pages".
    static func line(_ project: AgentProject) -> String {
        let role = project.bot.role.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = [role.isEmpty ? nil : role, project.summary].compactMap { $0 }
        return parts.isEmpty ? "No work yet" : parts.joined(separator: " · ")
    }
}

/// The state of an agent as a symbol and a word in the color of the state.
struct AgentStatusLabel: View {
    let status: AgentProject.Status
    /// A row of the list shows the short label and never cuts it.
    var short = false

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: status.symbol).imageScale(.small).fontWeight(.semibold)
            Text(short ? status.shortLabel : status.label).lineLimit(1)
        }
        .font(.subheadline).foregroundStyle(status.tint)
        .fixedSize(horizontal: short, vertical: false)
    }
}

/// The page of an agent, as the page of a project: a header with the icon, the name, the role, the state and the
/// model, then Chat, To-dos, Pages, Routines and Settings.
struct AgentProjectView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openChat) private var openChat
    @Environment(\.dismiss) private var dismiss
    let botId: String
    @State private var routineOpen = false
    @State private var editing: Routine?
    @State private var deleting: Bot?

    var body: some View {
        if let bot = store.bots.first(where: { $0.id == botId }) {
            page(AgentProject(bot: bot, input: store.agentProjectInput))
        } else {
            QuietEmptyState(symbol: "person.crop.square", title: "Agent unavailable",
                            message: "This agent is no longer on your Mac.")
        }
    }

    private func page(_ project: AgentProject) -> some View {
        let bot = project.bot
        return BoardPage {
            header(project)
            #if os(iOS)
            chatButton(bot)
            #endif
            BoardSection(project.chats.isEmpty ? AgentSection.chat.title : "Chats", count: project.count(.chat)) {
                // A new chat is a fresh conversation with the same agent: its instructions, model, memory and pages.
                if !bot.archived {
                    Button { newChat(bot) } label: { Label("New Chat", systemImage: "square.and.pencil") }
                        .accessibilityIdentifier("agent-new-chat")
                }
            } rows: {
                // The newest chats come first. The first chat of the agent, with its name, is the last row.
                ForEach(project.chats) { extra in
                    BotContactRow(bot: extra, avatarSize: 32, title: extra.chatLabel)
                        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                        .contentShape(Rectangle())
                        .boardButton("agent-chat-" + extra.id) { chat(ConversationRoute(botId: extra.id)) }
                        .contextMenu {
                            Button("Archive Chat", systemImage: "archivebox") { Task { await store.archiveBot(extra, archived: true) } }
                            Button("Delete Chat…", systemImage: "trash", role: .destructive) { deleting = extra }
                        }
                }
                BotContactRow(bot: bot, avatarSize: 32)
                    .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                    .contentShape(Rectangle())
                    .boardButton("agent-open-chat") { chat(ConversationRoute(botId: bot.id)) }
                if project.pendingCards > 0 {
                    BoardRow(symbol: "circle.inset.filled", tint: BoardStyle.attention,
                             title: project.pendingCards == 1 ? "1 card needs you" : "\(project.pendingCards) cards need you")
                        .boardButton("agent-cards") { chat(ConversationRoute(botId: bot.id)) }
                }
            }
            BoardSection(AgentSection.todos.title, count: project.count(.todos)) {
                if project.todos.isEmpty { BoardEmptyRow(text: "No open work. The agent lists what it works on here.") }
                ForEach(project.todos) { item in
                    BoardRow(symbol: AgentTodos.symbol(item.status),
                             tint: item.status == "blocked" ? BoardStyle.destructive
                                 : item.status == "working" ? BoardStyle.positive : .secondary,
                             title: item.title, detail: item.preview, value: AgentTodos.label(item.status))
                        .boardButton("agent-todo-" + item.id) {
                            chat(ConversationRoute(botId: item.botId, workContext: WorkReference(id: item.id, title: item.title)))
                        }
                }
            }
            BoardSection(AgentSection.pages.title, count: project.count(.pages)) {
                if project.pages.isEmpty { BoardEmptyRow(text: "No pages. The agent keeps notes, checklists and charts here.") }
                ForEach(project.pages) { page in
                    BoardRow(symbol: page.isPinned ? "star.fill" : page.symbol, title: page.title, detail: page.detail,
                             value: page.endText)
                        .boardButton("agent-page-" + page.key) { pushRoute?(DocumentRoute(id: page.id)) }
                        .contextMenu { PinDocumentButton(document: page) }
                }
            }
            BoardSection(AgentSection.routines.title, count: project.count(.routines)) {
                Button { routineOpen = true } label: { Label("Add", systemImage: "plus") }
                    .accessibilityIdentifier("agent-add-routine")
            } rows: {
                if project.routines.isEmpty && project.checks.isEmpty {
                    BoardEmptyRow(text: "No routines. A routine runs the agent on a schedule.")
                }
                ForEach(project.routines + project.checks) { routine in
                    BoardRow(symbol: routine.error != nil ? "exclamationmark.circle" : routine.enabled ? "repeat" : "pause",
                             tint: routine.error != nil ? BoardStyle.destructive : .secondary,
                             title: routine.name, detail: routine.error ?? routine.scheduleLabel,
                             value: routine.nextRunAt?.apiDate.map { $0.formatted(date: .omitted, time: .shortened) })
                        .boardButton("agent-routine-" + routine.id) { editing = routine }
                }
            }
            AgentUsageCard(botId: bot.id)
            BoardSection(AgentSection.settings.title) {
                BoardRow(symbol: "cpu", title: "Model", value: store.modelName(bot.id) ?? "Default")
                    .boardButton("agent-model") { pushRoute?(AgentSettingRoute.model(bot.id)) }
                BoardRow(symbol: "lock.shield", title: "Access",
                         value: bot.approvalOverride.map { store.approvals?.label(for: $0) ?? $0 } ?? "Default")
                    .boardButton("agent-access") { pushRoute?(AgentSettingRoute.access(bot.id)) }
                BoardRow(symbol: bot.notifications ? "bell" : "bell.slash", title: "Notifications",
                         value: bot.notifications ? "On" : "Off")
                    .boardButton("agent-notifications") { pushRoute?(BotSettingsRoute(botId: bot.id)) }
                BoardRow(symbol: "slider.horizontal.3", title: "Name, icon and instructions")
                    .boardButton("agent-settings") { pushRoute?(BotSettingsRoute(botId: bot.id)) }
                BoardRow(symbol: bot.archived ? "tray.and.arrow.up" : "archivebox",
                         title: bot.archived ? "Unarchive" : "Archive", chevron: false)
                    .boardButton("agent-archive") { Task { await store.archiveBot(bot, archived: !bot.archived) } }
                BoardRow(symbol: "trash", tint: BoardStyle.destructive, title: "Delete agent", chevron: false)
                    .boardButton("agent-delete") { deleting = bot }
            }
        }
        .navigationTitle(bot.name).inlineNavigationTitle()
        .modifier(DeleteBotDialog(bot: $deleting))
        // The page of a deleted agent closes.
        .onChange(of: store.bots.contains { $0.id == botId }) { _, exists in if !exists { dismiss() } }
        .sheet(isPresented: $routineOpen) {
            NavigationStack { RoutineEditorView(botId: botId) }.sheetShowsError()
        }
        .sheet(item: $editing) { routine in
            NavigationStack { RoutineEditorView(botId: botId, routine: routine) }.sheetShowsError()
        }
        .task(id: store.connection == .connected) {
            if store.documents == nil { await store.loadDocuments(quiet: true) }
        }
    }

    private func header(_ project: AgentProject) -> some View {
        let role = project.bot.role.trimmingCharacters(in: .whitespacesAndNewlines)
        // The model is a menu: a tap, then a model.
        let model = ModelMenu(botId: project.bot.id, settings: { pushRoute?(AgentSettingRoute.model(project.bot.id)) }) {
            ModelMenuLabel(name: $0).font(.subheadline).foregroundStyle(.secondary)
        }
        .fixedSize()
        .accessibilityIdentifier("agent-model-menu")
        // The state has the identifier, not the line: the menu of the model keeps its own.
        let status = AgentStatusLabel(status: project.status).accessibilityElement(children: .combine)
            .accessibilityIdentifier("agent-status")
        return HStack(alignment: .center, spacing: BoardStyle.Space.l) {
            LiveBotAvatar(bot: project.bot, size: 56)
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                Text(project.bot.name).font(.title2.weight(.bold)).lineLimit(1)
                if !role.isEmpty { Text(role).font(.subheadline).foregroundStyle(.secondary).lineLimit(2) }
                // The state and the model share a line where it fits, as in a Mac window. A narrow iPhone shows the
                // model under the state, so neither is cut.
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: BoardStyle.Space.m) { status; model }
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        status
                        model
                    }
                }
            }
            Spacer(minLength: 0)
            // A Mac window has room for the button next to the name. An iPhone shows it under the header.
            #if os(macOS)
            chatButton(project.bot)
            #endif
        }
    }

    private func chatButton(_ bot: Bot) -> some View {
        Button { chat(ConversationRoute(botId: bot.id)) } label: {
            Label("Open chat", systemImage: "bubble.left.and.bubble.right")
                #if os(iOS)
                .frame(maxWidth: .infinity)
                #endif
        }
        .buttonStyle(CoconutButtonStyle())
        .accessibilityIdentifier("agent-chat-button")
    }

    private func newChat(_ bot: Bot) {
        Task { if let new = await store.newChat(with: bot) { chat(ConversationRoute(botId: new.id)) } }
    }

    /// The Mac window shows a chat in its own place. An iPhone pushes it onto the stack of the tab.
    private func chat(_ route: ConversationRoute) {
        if let openChat, route.workContext == nil { openChat(route.botId) } else { pushRoute?(route) }
    }
}

/// A page of the settings of one agent that the agent page opens directly.
enum AgentSettingRoute: Hashable {
    case model(String)
    case access(String)
}

/// The Library as one list: every page of every agent and of the team agents, grouped by agent.
struct AllPagesView: View {
    var body: some View {
        LibraryView().navigationTitle("All pages").inlineNavigationTitle()
    }
}

/// The pages that the agents open, for a navigation stack of the iPhone tabs and of the Mac window: the page of an
/// agent, its model and its access, and the pages of all agents.
struct AgentDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content
            .navigationDestination(for: AgentRoute.self) { AgentProjectView(botId: $0.botId) }
            .navigationDestination(for: AllPagesRoute.self) { _ in AllPagesView() }
            .navigationDestination(for: UsageRoute.self) { UsageView(botId: $0.botId).id($0.botId) }
            .navigationDestination(for: AgentSettingRoute.self) { route in
                switch route {
                case .model(let id): ModelSettingsView(botId: id)
                case .access(let id): ApprovalSettingsView(botId: id)
                }
            }
    }
}

extension AppStore {
    /// The pages to push for a document: the page of its agent first, so Back leads to the agent. A page of a team
    /// agent has no agent page on this Mac.
    func documentPath(_ id: String) -> [any Hashable] {
        let owner = documents?.first { $0.id == id }?.botId
        let agent = bots.agents.first { $0.id == owner }.map { [AgentRoute(botId: $0.id) as any Hashable] } ?? []
        return agent + [DocumentRoute(id: id)]
    }
}
