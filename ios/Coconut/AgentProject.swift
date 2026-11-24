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
    case chat, todos, pages, routines, manage

    var id: String { rawValue }
    var title: String {
        switch self {
        case .chat: "Agent"
        case .todos: "To-dos"
        case .pages: "Pages"
        case .routines: "Routines"
        case .manage: "Manage"
        }
    }
}

/// An agent as a project: the agent with its state, its open work, its pages and its routines. The agents list and the
/// page of an agent read it. It reads only its arguments.
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
        /// The date of the newest message of each agent (ChatSummaries).
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

    /// The projects, Coconut, Gumroad and Personal, newest activity first. A project without a message sorts by the time when the
    /// Mac made it.
    static func list(_ input: Input, now: Date = Date()) -> [AgentProject] {
        let latest = AgentChats.activity(bots: input.bots, latest: input.latest)
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return input.bots.projects.filter { !$0.archived }
            .sorted { (activity($0), $1.id) > (activity($1), $0.id) }
            .map { AgentProject(bot: $0, input: input, now: now) }
    }

    /// The quiet line of a row of the list: "2 to-dos · 3 pages · 1 routine". Nil when the agent has none of them.
    var summary: String? {
        func part(_ count: Int, _ one: String, _ many: String) -> String? {
            count == 0 ? nil : "\(count) \(count == 1 ? one : many)"
        }
        let parts = [part(todos.count, "to-do", "to-dos"), part(pages.count, "page", "pages"),
                     part(routines.count + checks.count, "routine", "routines")].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// The count after the title of a section: the number of its rows, so Agents counts the agents. A row has the unread
    /// messages of its chat. One chat needs no count, and Manage has none.
    func count(_ section: AgentSection) -> Int {
        switch section {
        case .chat: chats.isEmpty ? 0 : chats.count + 1
        case .todos: todos.count
        case .pages: pages.count
        case .routines: routines.count + checks.count
        case .manage: 0
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

/// The list of the projects: each one with its role, its state, its open work and its last activity, then the pages of
/// all of them. The Mac window shows it as a place. The iPhone opens it from the project menu of the message box.
struct AgentsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute

    var body: some View {
        let projects = AgentProject.list(store.agentProjectInput)
        BoardPage {
            // The Mac window shows the title of this place in the page, with the counts. An iPhone has the title in the
            // bar, as each pushed page.
            #if os(macOS)
            BoardPageTitle(title: "Projects", detail: Self.detail(projects))
            #endif
            if projects.isEmpty {
                QuietEmptyState(symbol: "folder", title: "No projects yet",
                                message: "Coconut, Gumroad and Personal show here when this Mac finds their Hermes profiles.")
            } else {
                // The bar of the iPhone and the title of the Mac page name the list, and the projects are always three.
                BoardSection {
                    ForEach(projects) { project in
                        AgentProjectRow(project: project)
                            .boardButton("agent-" + project.id) { show(project.id) }
                            .contextMenu { NewChatButton(bot: project.bot) }
                    }
                }
            }
            // What the projects keep: their pages. The archived chats have no page of their own: the person merged
            // Archived into the list of Chats, where they sit among the idle chats (on the Mac, in Archived of the
            // sidebar).
            BoardSection {
                BoardRow(symbol: "rectangle.3.group", title: "All pages",
                         value: store.documents.map { "\($0.count)" })
                    .boardButton("agents-all-pages") { pushRoute?(AllPagesRoute()) }
            }
        }
        #if os(iOS)
        .navigationTitle("Projects").inlineNavigationTitle()
        #endif
        // The page counts need the list of the pages, also before any page opens. A row has the spend of its project.
        .task(id: store.connection == .connected) {
            if store.documents == nil { await store.loadDocuments(quiet: true) }
            await store.loadUsage()
        }
        .refreshable { await store.reconnect() }
    }

    private func show(_ id: String) { pushRoute?(AgentRoute(botId: id)) }

    /// "3 projects · 2 working · 1 needs you".
    static func detail(_ projects: [AgentProject]) -> String {
        let working = projects.filter { $0.status == .working }.count
        let waiting = projects.filter { $0.status == .needsYou }.count
        let halted = projects.filter { if case .cannotRun = $0.status { true } else { false } }.count
        return (["\(projects.count) \(projects.count == 1 ? "project" : "projects")"]
            + (working > 0 ? ["\(working) working"] : [])
            + (waiting > 0 ? ["\(waiting) \(waiting == 1 ? "needs" : "need") you"] : [])
            + (halted > 0 ? ["\(halted) can't run"] : [])).joined(separator: " · ")
    }
}

/// One agent in the list: its name and role, its state, its open work, and its last activity.
struct AgentProjectRow: View {
    @Environment(AppStore.self) private var store
    let project: AgentProject

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                HStack(spacing: BoardStyle.Space.s) {
                    Text(project.bot.name).font(.body.weight(.semibold)).lineLimit(1)
                    if project.bot.unreadCount > 0 && project.bot.notifications { UnreadBadge(count: project.bot.unreadCount) }
                    if !project.bot.notifications { MutedMark() }
                }
                // The role and what the project has where both fit. A narrow row leaves the counts out, so no count
                // is cut in half ("1 p…").
                let role = project.bot.role.trimmingCharacters(in: .whitespacesAndNewlines)
                ViewThatFits(in: .horizontal) {
                    Text(Self.line(project))
                    Text(role.isEmpty ? Self.line(project) : role)
                }
                .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: BoardStyle.Space.s)
            VStack(alignment: .trailing, spacing: BoardStyle.Space.line) {
                AgentStatusLabel(status: project.status, short: true)
                HStack(spacing: BoardStyle.Space.xs) {
                    if let date = project.lastActivity { CompactAgeLabel(date: date) }
                    // What the agent cost today.
                    if let spend = store.spendToday(agent: project.bot.id) {
                        if project.lastActivity != nil { Text("·") }
                        Text(spend)
                            .accessibilityLabel("\(spend) today")
                            .accessibilityIdentifier("agent-spend-" + project.bot.id)
                    }
                }
                .font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                // The numbers keep one line: the role next to them is the text that gets cut.
                .lineLimit(1).fixedSize()
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

/// The page of a project: a header with the role and the state, then Agents, To-dos, Queue, Pages, Routines and Usage.
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
            QuietEmptyState(symbol: "person.crop.square", title: "Project unavailable",
                            message: "This project is no longer on your Mac.")
        }
    }

    private func page(_ project: AgentProject) -> some View {
        let bot = project.bot
        return BoardPage {
            header(project)
            #if os(iOS)
            if bot.isProject { chatButton(bot) }
            #endif
            BoardSection(project.chats.isEmpty ? AgentSection.chat.title : "Agents", count: project.count(.chat)) {
                // The newest chats come first. The first chat of the agent, with its name, is the last row.
                ForEach(project.chats) { extra in
                    BotContactRow(bot: extra, title: extra.chatLabel)
                        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                        .contentShape(Rectangle())
                        // A long press on the iPhone shows the deck of all chats. Its menu has what this menu had.
                        .chatRowButton("agent-chat-" + extra.id, botId: extra.id) { chat(ConversationRoute(botId: extra.id)) }
                        #if os(macOS)
                        .contextMenu {
                            Button("Archive agent", systemImage: "archivebox") { Task { await store.archiveBot(extra, archived: true) } }
                            Button("Delete agent…", systemImage: "trash", role: .destructive) { deleting = extra }
                        }
                        #endif
                }
                BotContactRow(bot: bot)
                    .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                    .contentShape(Rectangle())
                    .chatRowButton("agent-open-chat", botId: bot.id) { chat(ConversationRoute(botId: bot.id)) }
                if project.pendingCards > 0 {
                    BoardRow(symbol: "circle.inset.filled", tint: BoardStyle.attention,
                             title: project.pendingCards == 1 ? "1 card needs you" : "\(project.pendingCards) cards need you")
                        .boardButton("agent-cards") { chat(ConversationRoute(botId: bot.id)) }
                }
            }
            BoardSection(AgentSection.todos.title, count: project.count(.todos)) {
                if project.todos.isEmpty { BoardEmptyRow(text: "No open to-dos.") }
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
            // The agent's queue on the Hermes board of the Mac.
            let queue = store.queues[bot.id] ?? AgentQueue()
            // Four counts and nothing to open, so they share one row of numbers.
            BoardSection("Queue") {
                HStack(alignment: .top, spacing: BoardStyle.Space.s) {
                    UsageStat(title: "Running", value: "\(queue.running)")
                    UsageStat(title: "Waiting", value: "\(queue.waiting)")
                    UsageStat(title: "Needs you", value: "\(queue.needsYou)")
                    UsageStat(title: "Done today", value: "\(queue.doneToday)")
                }
                .padding(BoardStyle.Space.m)
                .accessibilityIdentifier("agent-queue")
            }
            BoardSection(AgentSection.pages.title, count: project.count(.pages)) {
                if project.pages.isEmpty { BoardEmptyRow(text: "No pages.") }
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
                    BoardEmptyRow(text: "No routines.")
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
            // A project stays. Only an agent of an earlier release can go (Bot.canRemove).
            if bot.canRemove {
                BoardSection(AgentSection.manage.title) {
                    BoardRow(symbol: bot.archived ? "tray.and.arrow.up" : "archivebox",
                             title: bot.archived ? "Unarchive" : "Archive", chevron: false)
                        .boardButton("agent-archive") { Task { await store.archiveBot(bot, archived: !bot.archived) } }
                    BoardRow(symbol: "trash", tint: BoardStyle.destructive, title: "Delete agent", chevron: false)
                        .boardButton("agent-delete") { deleting = bot }
                }
            }
        }
        .navigationTitle(bot.name).inlineNavigationTitle()
        // A pushed page covers the banner of Home, so the page shows why a New chat or a change failed.
        .pageErrorBanner()
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
        return HStack(alignment: .center, spacing: BoardStyle.Space.l) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                // The Mac window shows the name in the page. An iPhone has it in the bar, as each pushed page.
                #if os(macOS)
                Text(project.bot.name).font(.title.weight(.bold)).lineLimit(1).minimumScaleFactor(0.7)
                #endif
                if !role.isEmpty { Text(role).font(.subheadline).foregroundStyle(.secondary).lineLimit(2) }
                AgentStatusLabel(status: project.status).accessibilityElement(children: .combine)
                    .accessibilityIdentifier("agent-status")
            }
            Spacer(minLength: 0)
            // A Mac window has room for the button next to the name. An iPhone shows it under the header.
            #if os(macOS)
            if project.bot.isProject { chatButton(project.bot) }
            #endif
        }
    }

    /// A project has many chats, so the button starts a fresh one: the message box with this project chosen. The old
    /// chats are in the list below. It is the one New chat control of this page. An agent of an earlier release takes
    /// no new chat and has no button.
    private func chatButton(_ bot: Bot) -> some View {
        Button { store.startChat(in: bot) } label: {
            Label("New agent", systemImage: "square.and.pencil")
                #if os(iOS)
                .frame(maxWidth: .infinity)
                #endif
        }
        .buttonStyle(CoconutButtonStyle())
        .disabled(bot.archived)
        .accessibilityIdentifier("agent-new-chat-button")
    }

    /// The Mac window shows a chat in its own place. An iPhone pushes it onto the stack of the tab.
    private func chat(_ route: ConversationRoute) {
        if let openChat, route.workContext == nil { openChat(route.botId) } else { pushRoute?(route) }
    }
}

/// The Library as one list: every page of every agent and of the team agents, grouped by agent.
struct AllPagesView: View {
    var body: some View {
        LibraryView().navigationTitle("All pages").inlineNavigationTitle()
    }
}

/// The pages that the agents open, for a navigation stack of the iPhone tabs and of the Mac window: the page of an
/// agent and the pages of all agents.
struct AgentDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content
            .navigationDestination(for: AgentRoute.self) { AgentProjectView(botId: $0.botId) }
            .navigationDestination(for: AllPagesRoute.self) { _ in AllPagesView() }
            .navigationDestination(for: UsageRoute.self) { UsageView(botId: $0.botId).id($0.botId) }
            .navigationDestination(for: SpendRoute.self) { _ in SpendView() }
            .navigationDestination(for: LiveRoute.self) { _ in LiveView() }
    }
}

extension AppStore {
    /// The pages to push for a document: the page of its project first, so Back leads to the project. A page of a team
    /// agent has no project page on this Mac.
    func documentPath(_ id: String) -> [any Hashable] {
        let owner = documents?.first { $0.id == id }?.botId
        let agent = bots.withoutChats.first { $0.id == owner }.map { [AgentRoute(botId: $0.id) as any Hashable] } ?? []
        return agent + [DocumentRoute(id: id)]
    }
}
