import SwiftUI

/// One row of "Unread" on Home: a chat with unread messages, or a card that waits for an answer.
enum HomeUnread: Identifiable, Equatable {
    case chat(ChatListItem, Date)
    case card(Decision)

    var id: String {
        switch self {
        case .chat(let item, _): item.id
        case .card(let decision): "card-" + decision.id
        }
    }

    /// The time that sorts the row: the newest activity of a chat, or the time when the agent made the card.
    var date: Date {
        switch self {
        case .chat(_, let date): date
        case .card(let decision): decision.createdAt.apiDate ?? .distantPast
        }
    }

    var target: HomeTarget {
        switch self {
        case .chat(.bot(let bot), _): .conversation(ConversationRoute(botId: bot.id))
        case .chat(let item, _): item.chat.map(HomeTarget.teamChat) ?? .agents
        case .card(let decision): .cards(decision.id)
        }
    }
}

/// One row of "In progress" on Home: a turn that runs now, a To-do that is open, or an active notice of a team.
enum HomeProgress: Identifiable, Equatable {
    case turn(AgentTask, Bot)
    case todo(Artifact, Bot)
    case notice(TeamRoom, WorkspaceNotice)

    var id: String {
        switch self {
        case .turn(let task, _): "turn-" + task.id
        case .todo(let item, _): "todo-" + item.id
        case .notice(let room, let notice): "notice-" + room.room + "-" + notice.id
        }
    }

    var target: HomeTarget {
        switch self {
        case .turn(let task, _): .conversation(ConversationRoute(botId: task.chatBotId))
        case .todo(let item, _):
            .conversation(ConversationRoute(botId: item.botId, workContext: WorkReference(id: item.id, title: item.title)))
        case .notice(let room, _): .notices(room.room)
        }
    }
}

/// The lists of Home. They read only their arguments, so a test can call them.
enum HomeFeed {
    /// "Unread": each chat with unread messages (an agent, the Coconut chat, a team, a named chat, a direct chat) and
    /// each pending card, newest first. A starred chat counts too: Home is the one place that has them all. An
    /// archived agent and a muted chat do not count.
    static func unread(bots: [Bot], latest: [String: Date], rooms: [TeamRoom],
                       local: [TeamChatKey: TeamLastMessage] = [:], decisions: [Decision],
                       answering: Set<String> = []) -> [HomeUnread] {
        let chats = bots.filter { !$0.archived }.map(ChatListItem.bot)
            + ChatList.pinnedChats(rooms) + ChatList.people(rooms, local: local) + ChatList.botDirects(rooms, local: local)
        let rows = chats.filter { $0.unreadCount > 0 }.map { HomeUnread.chat($0, $0.activity(latest: latest)) }
            + decisions.filter { $0.isPending && !answering.contains($0.id) }.map(HomeUnread.card)
        return rows.sorted { ($0.date, $1.id) > ($1.date, $0.id) }
    }

    /// The count of the Home tab and of the Home row of the sidebar: the unread messages and the pending cards.
    static func badge(_ unread: [HomeUnread]) -> Int {
        unread.reduce(0) { sum, row in
            if case .chat(let item, _) = row { return sum + item.unreadCount }
            return sum + 1
        }
    }

    /// "In progress": the turns that run now, then the open To-dos (AgentTodos), then the active notices of the
    /// teams. A turn whose To-do shows already does not show again, and an agent shows one turn.
    static func inProgress(bots: [Bot], tasks: [AgentTask], artifacts: [Artifact], rooms: [TeamRoom]) -> [HomeProgress] {
        let active = Dictionary(bots.filter { !$0.archived }.map { ($0.id, $0) }) { first, _ in first }
        let todos = AgentTodos.make(artifacts: artifacts, bots: bots)
        let covered = Set(todos.compactMap(\.taskId))
        var seen = Set<String>()
        let turns = tasks.reversed().filter { task in
            task.isRunning && !covered.contains(task.id) && active[task.chatBotId] != nil && seen.insert(task.chatBotId).inserted
        }
        return turns.compactMap { task in active[task.chatBotId].map { HomeProgress.turn(task, $0) } }
            + todos.compactMap { item in active[item.botId].map { HomeProgress.todo(item, $0) } }
            + rooms.filter { $0.status == .active }.flatMap { room in
                room.noticeList.filter(\.active).map { HomeProgress.notice(room, $0) }
            }
    }
}

extension AppStore {
    /// The rows of "Unread" on Home.
    var homeUnread: [HomeUnread] {
        HomeFeed.unread(bots: bots, latest: chatSummaries.newestActivity, rooms: shownTeamRooms,
                        local: newestLocalMessages, decisions: decisions, answering: answeringDecisionIds)
    }
}

/// Home: the start of the day. "Needs attention" shows first and only when a rule breaches. "Unread" has each chat
/// with unread messages and each pending card. "In progress" has what the agents do now and the active notices.
struct HomeView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    let open: (HomeTarget) -> Void

    /// The rows of "In progress" that show before "Show all".
    static let progressLimit = 8
    @State private var allProgress = false

    #if os(macOS)
    private let face: CGFloat = 32
    #else
    private let face: CGFloat = 40
    #endif

    var body: some View {
        let unread = store.homeUnread
        let progress = HomeFeed.inProgress(bots: store.bots, tasks: store.tasks, artifacts: store.artifacts,
                                           rooms: store.shownTeamRooms)
        BoardPage {
            BoardPageTitle(title: "Home", detail: Date().formatted(.dateTime.weekday(.wide).month(.wide).day()))
            // The rules read the clock, so the banner checks them again each minute.
            TimelineView(.periodic(from: .now, by: 60)) { timeline in
                AttentionCard(items: store.attentionItems(now: timeline.date), open: open)
            }
            liveSpend
            let cards = unread.filter { if case .card = $0 { true } else { false } }.count
            BoardSection("Unread", count: HomeFeed.badge(unread)) {
                // The cards one after the other in their recommended order, as the Inbox showed them.
                if cards > 0 {
                    Button(cards == 1 ? "Review 1 card" : "Review \(cards) cards") { open(.cards(nil)) }
                        .accessibilityIdentifier("home-review-cards")
                }
            } rows: {
                if unread.isEmpty {
                    BoardEmptyRow(text: "Nothing unread. New messages and cards from your agents show here.")
                        .accessibilityIdentifier("home-unread-empty")
                } else {
                    ForEach(unread) { unreadRow($0) }
                }
            }
            if !progress.isEmpty {
                let shown = allProgress ? progress : Array(progress.prefix(Self.progressLimit))
                BoardSection("In progress", count: progress.count) {
                    ForEach(shown) { progressRow($0) }
                    if progress.count > shown.count {
                        Text("Show all \(progress.count)").font(.subheadline).foregroundStyle(.secondary)
                            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
                            .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            .boardButton("home-progress-all") { allProgress = true }
                    }
                }
            }
        }
        .refreshable { await store.reconnect() }
    }

    /// Two tiles that open the Live page and the Spend page: how many agents work now, and what the agents cost today.
    private var liveSpend: some View {
        let working = LiveFeed.agents(bots: store.bots, tasks: store.tasks).count
        return BentoBoard {
            BentoStat(title: "Live", value: "\(working)", unit: "working", size: .medium, chevron: true)
                .contentShape(Rectangle())
                .onTapGesture { pushRoute?(LiveRoute()) }
                .accessibilityAddTraits(.isButton)
                .accessibilityIdentifier("home-live")
                .bento(6, 1, compact: (1, 1))
            BentoStat(title: "Spend", value: store.spendTodayTotal ?? "\u{2014}", unit: "today", size: .medium, chevron: true)
                .contentShape(Rectangle())
                .onTapGesture { pushRoute?(SpendRoute()) }
                .accessibilityAddTraits(.isButton)
                .accessibilityIdentifier("home-spend")
                .bento(6, 1, compact: (1, 1))
        }
        .task(id: store.connection == .connected) { await store.loadUsage() }
    }

    private func unreadRow(_ row: HomeUnread) -> some View {
        Button { open(row.target) } label: {
            Group {
                switch row {
                case .chat(.bot(let bot), _): BotContactRow(bot: bot, avatarSize: face)
                case .chat(let item, _): PeopleRow(item: item, avatarSize: face)
                case .card(let decision): cardRow(decision)
                }
            }
            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityIdentifier("home-unread-" + row.id)
    }

    /// A card that waits: its question, the agent that asks, and its age.
    private func cardRow(_ decision: Decision) -> some View {
        let bot = store.bots.first { $0.id == store.conversationBotId(for: decision) }
        return ChatRowLayout(name: decision.title, unread: 0, date: decision.createdAt.apiDate) {
            if let bot { LiveBotAvatar(bot: bot, size: face) }
        } subtitle: {
            HStack(spacing: BoardStyle.Space.xs) {
                StatusLabel(status: "needs_you", compact: true)
                Text("· " + (bot?.name ?? "Agent")).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func progressRow(_ row: HomeProgress) -> some View {
        Button { open(row.target) } label: {
            HStack(spacing: BoardStyle.Space.m) {
                switch row {
                case .turn(let task, let bot):
                    progressSymbol("circle.dotted.circle", tint: BoardStyle.positive)
                    Text(task.title.isEmpty ? (store.botPhase(bot)?.label ?? "Working") : task.title).lineLimit(1)
                    Spacer(minLength: BoardStyle.Space.s)
                    progressDetail(bot.name)
                case .todo(let item, let bot):
                    progressSymbol(AgentTodos.symbol(item.status),
                                   tint: item.status == "blocked" ? BoardStyle.destructive
                                       : item.status == "working" ? BoardStyle.positive : .secondary)
                    Text(item.title).lineLimit(1)
                    Spacer(minLength: BoardStyle.Space.s)
                    progressDetail(item.status == "working" ? bot.name : AgentTodos.label(item.status) + " · " + bot.name)
                case .notice(let room, let notice):
                    progressSymbol(notice.kind.symbol, tint: notice.kind.tint)
                    Text(notice.text).lineLimit(1)
                    Spacer(minLength: BoardStyle.Space.s)
                    progressDetail(notice.kind.label + " · " + room.name)
                }
            }
            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("home-progress-" + row.id)
    }

    private func progressSymbol(_ name: String, tint: Color) -> some View {
        Image(systemName: name).font(.callout).foregroundStyle(tint).frame(width: 20)
    }

    private func progressDetail(_ text: String) -> some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary).lineLimit(1).layoutPriority(-1)
    }
}
