import SwiftUI

/// Two tabs of the iPhone. Product has the rules that breach for the agents and the cards that wait, then its to-dos,
/// the issues of gumroad-private with the label product (TodoLists.swift), and the open pull requests (ProductWork).
/// Chats is one list, as Telegram has it: the agents of the projects, the teams, their named chats and the direct
/// chats, working, idle and archived together, with the message box under the list (NewChatBar). Many idle agents of
/// one project fold into a tree under its name (ChatTree).
struct HomeLaunchView: View {
    enum Part { case product, chats }
    var part = Part.chats
    @Environment(AppStore.self) private var store
    /// The projects whose tree is open on Chats (ChatTree). The phone keeps them.
    @AppStorage(ChatTree.key) private var openTrees = ""
    /// A chat in the tree of its project starts where the name of the project starts, after the chevron (ChatTreeRow).
    @ScaledMetric(relativeTo: .body) private var treeChevron = ChatTreeRow.chevronWidth
    private var treeIndent: CGFloat { treeChevron + BoardStyle.Space.m }
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openURL) private var openURL
    /// Opens the place of a row of Needs attention, or the cards that wait.
    let open: (HomeTarget) -> Void
    @ScaledMetric(relativeTo: .title3) private var todoIconWidth = 24
    /// The reports of the product work as they loaded here. A demo has no copy in the store (businessReports).
    @State private var productReports: [String: BusinessReport] = [:]
    @State private var productError: String?

    var body: some View {
        switch part {
        case .product: productList
        case .chats: chatList(ChatList.all(bots: store.bots, latest: store.chatSummaries.newestActivity,
                                           rooms: store.shownTeamRooms, local: store.newestLocalMessages))
        }
    }

    private var productList: some View {
        let cards = store.pendingCardCount
        let pulls = ProductWork.pulls(productReport("engineering")?.data)
        return ScrollViewReader { proxy in
            List {
                attention(cards: cards)
                // The to-dos of Product, which its badge counts (TodoCounts). They have no title, as on Finance and
                // Support: the rows say what they are (TodoListSection). The cards and the alerts of the agents above
                // them are no to-dos of gumroad-private, so they add nothing to the count.
                TodoIssueRows(area: .product)
                // The pull requests are the state of the product, not to-dos. A section without rows does not show.
                if !pulls.isEmpty {
                    productHeader("Pull requests", count: pulls.count)
                    ForEach(pulls) { productRow($0) }
                }
                if let productError {
                    BoardLoadNotice(message: productError) { Task { await loadProductWork() } }
                        .padding(.vertical, BoardStyle.Space.l)
                        .accessibilityIdentifier("product-work-error")
                        .homeRow()
                }
                Color.clear.frame(height: 1).id(Self.productEnd).homeRow()
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .environment(\.defaultMinListRowHeight, 0)
            .contentMargins(.top, BoardStyle.Space.l, for: .scrollContent)
            .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
            .background(BoardStyle.paper)
            // A screenshot of the end of the page starts the app with `--bottom` (PreviewLaunch).
            .task {
                guard PreviewLaunch.startsAtBottom else { return }
                for wait in [1.0, 2, 3] {
                    try? await Task.sleep(for: .seconds(wait))
                    proxy.scrollTo(Self.productEnd, anchor: .bottom)
                }
            }
        }
        .navigationTitle("Product")
        .task(id: "\(store.connection)/\(store.businessChanges)") { await loadProductWork() }
        // One tap and one line add a to-do to Product, which an agent starts on at once (NewTodoIssueSheet).
        .newTodoButton(.product)
        // The root of the app loads the to-dos and the overview for the counts of the tabs (CoconutHome), so a pull only
        // loads them again.
        .refreshable {
            await store.reconnect()
            await store.loadTodoIssues(fresh: true)
            await store.loadBusinessAttention()
            await loadProductWork()
        }
    }

    private static let productEnd = "product-end"

    private func productReport(_ key: String) -> BusinessReport? { store.businessReports[key] ?? productReports[key] }

    /// Loads the reports of the product work. The first failure shows under the rows, with the one way to try again.
    private func loadProductWork() async {
        var failure: String?
        for key in ProductWork.reports {
            do {
                let report = try await store.businessReport(key)
                guard !Task.isCancelled else { return }
                productReports[key] = report
            } catch is CancellationError { return }
            catch { failure = failure ?? error.localizedDescription }
        }
        guard !Task.isCancelled else { return }
        productError = failure
    }

    /// The title of a section of the product work, as the title of a BoardSection.
    private func productHeader(_ title: String, count: Int) -> some View {
        HStack(spacing: BoardStyle.Space.s) {
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            CountPill(count: count)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .padding(.top, BoardStyle.Space.xl).padding(.bottom, BoardStyle.Space.xs)
        .homeRow()
    }

    /// An issue or a pull request. A tap opens it in Coconut (GitHubItemView): the stack opens the link of GitHub as a
    /// page of its own (stackRoutes), never in the browser.
    private func productRow(_ item: ProductWork.Item) -> some View {
        Button { if let url = item.url { openURL(url) } } label: {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: item.symbol)
                    .font(.title3)
                    .foregroundStyle(item.tint)
                    .frame(width: todoIconWidth)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(item.title).font(.body).foregroundStyle(.primary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                    Text(item.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
                if let change = item.change {
                    HStack(spacing: BoardStyle.Space.xs) {
                        Text(change.additions).foregroundStyle(BoardStyle.positive)
                        Text(change.deletions).foregroundStyle(BoardStyle.attention)
                    }
                    .font(.caption.weight(.medium)).monospacedDigit().lineLimit(1)
                }
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("product-" + item.id)
        .homeRow()
    }

    /// The list of Chats, as Telegram has it: the pinned chats first, then every other chat newest activity first
    /// (ChatList.all), the archived chats too. Every row has the same form: the name, the last message, the age and the
    /// badges. A starred row only adds the star mark. The list has no tabs or toggles, because the person found that
    /// nobody taps them (2026-11-24): working, idle and archived chats share the one list, and a chat archives itself
    /// after the time that You sets (AutoArchiveMenu). Many idle agents of one project fold into one row with its name,
    /// which opens into a tree of them (ChatTree). A chat whose row shows work never folds, so each one stays in sight.
    private func chatList(_ rows: [ChatListItem]) -> some View {
        let now = Date.now
        let working = Set(rows.filter { works($0, at: now) }.map(\.id))
        let entries = ChatTree.entries(rows, open: ChatTree.open(openTrees), loose: working)
        // A list, not a stack, so that a row of a chat has the swipe of the system: swipe left to archive.
        return List {
            if rows.isEmpty {
                Text("No chats yet.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .accessibilityIdentifier("chats-empty")
                    .homeRow()
            }
            ForEach(entries) { entry in
                switch entry {
                case .group(let group):
                    ChatTreeRow(group: group) {
                        withAnimation(.snappy) { openTrees = ChatTree.toggle(group.agentId, openTrees) }
                    }
                    .homeRow()
                case .chat(let item, let nested):
                    chatRow(item, nested: nested)
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.s, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        .navigationTitle("Chats")
        .toolbar {
            // One tap reads every chat, as Read All in Telegram. It shows while a chat of the list has an unread
            // count.
            if rows.contains(where: { $0.unreadCount > 0 }) {
                ToolbarItem(placement: .primaryAction) {
                    MarkAllReadButton().accessibilityIdentifier("chats-mark-all-read")
                }
            }
        }
        .refreshable { await store.reconnect() }
        // The message box under the list gives the keyboard back when the list moves.
        .scrollDismissesKeyboard(.interactively)
        // A new chat shows at its place in the list, with the tree of its project open.
        .modifier(NewChatReveal { bot in openTrees = ChatTree.opening(bot.agentId, openTrees) })
    }

    /// One chat of the list. A chat in the tree of its project is set in under the name of the project and shows its own
    /// title, because the row above names the project.
    @ViewBuilder
    private func chatRow(_ item: ChatListItem, nested: Bool) -> some View {
        switch item {
        case .bot(let bot):
            BotContactRow(bot: bot, title: nested ? ChatTree.title(bot) : bot.isChat ? bot.listName : nil,
                          pinned: !nested && ChatList.isStarred(bot))
                .padding(.vertical, BoardStyle.Space.m)
                .padding(.leading, nested ? treeIndent : 0)
                .background(BoardStyle.paper)
                .modifier(ChatDeckRow(botId: bot.id) { pushRoute?(ConversationRoute(botId: bot.id)) })
                .accessibilityIdentifier(ChatList.isCoconut(bot) ? "coconut-chat" : "bot-conversation-\(bot.id)")
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    // A project stays, and so does the Coconut chat (Bot.canRemove). An archived chat offers
                    // Unarchive in the same place.
                    if bot.canRemove { archiveButton(bot) }
                    muteButton(bot)
                }
                .homeRow()
        case .team, .direct, .botDirect:
            // A team chat has Star and Mute in its menu, and a named chat has Mute in its swipe too. The Team
            // screen has the leave action.
            if let chat = item.chat {
                Button { pushRoute?(TeamChatRoute(chat: chat)) } label: {
                    PeopleRow(item: item, pinMark: item.isPinnedChat)
                        .padding(.vertical, BoardStyle.Space.m)
                        .background(BoardStyle.paper)
                        .contentShape(Rectangle())
                        .modifier(TeamChatMuteMenu(item: item))
                }
                    .buttonStyle(HomePressStyle())
                    .accessibilityIdentifier(Self.identifier(chat))
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        if let mute = TeamChatMute.listAction(item) { teamMuteButton(mute) }
                    }
                    .homeRow()
            }
        }
    }

    /// A chat works while its row says so: a working line, or a card that waits for the person. A direct chat with a
    /// bot of a team works while the bot types in it. Every other chat is idle, also a chat with people.
    private func works(_ item: ChatListItem, at date: Date) -> Bool {
        switch item {
        case .bot(let bot): store.botPhase(bot) != nil || store.botNeedsYou(bot)
        case .botDirect(_, let direct): !(direct.typing?.active(at: date).isEmpty ?? true)
        case .team, .direct: false
        }
    }

    /// What asks for the person comes first on Product: the rules that breach now, then the cards that wait. Each row
    /// draws nothing when there is nothing to show. The breaches of the business are not here: Support past SLA is a
    /// row of the list of Support, and the product issues past SLA and of high priority are the to-dos themselves.
    @ViewBuilder
    private func attention(cards: Int) -> some View {
        // The rules read the clock, so the card checks them again each minute.
        TimelineView(.periodic(from: .now, by: 60)) { timeline in
            let items = store.attentionItems(now: timeline.date).filter { $0.kind != .business }
            AttentionCard(items: items, open: open)
                .padding(.bottom, items.isEmpty ? 0 : BoardStyle.Space.m)
        }
        .homeRow(top: BoardStyle.Space.s)
        if cards > 0 {
            Button { open(.cards(nil)) } label: {
                HStack(spacing: BoardStyle.Space.m) {
                    // The count is what the person came for, so it is the largest thing under the title.
                    Text("\(cards)").font(.largeTitle.weight(.bold).monospacedDigit()).foregroundStyle(BoardStyle.attention)
                    Text(cards == 1 ? "card waits for you" : "cards wait for you")
                        .font(.body.weight(.semibold)).foregroundStyle(.primary)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
                .padding(.vertical, BoardStyle.Space.m)
                .contentShape(Rectangle())
            }
            .buttonStyle(HomePressStyle())
            .accessibilityIdentifier("home-review-cards")
            .homeRow()
        }
    }

    /// Archive, or Unarchive for a chat that is archived. The row stays in Idle either way: an archived chat runs no
    /// routine and sends no notification until a message of the person brings it back (agent_chats.py).
    private func archiveButton(_ bot: Bot) -> some View {
        Button { Task { await store.archiveBot(bot, archived: !bot.archived) } } label: {
            Label(bot.archived ? "Unarchive" : "Archive", systemImage: bot.archived ? "tray.and.arrow.up" : "archivebox")
        }
        .tint(bot.archived ? BoardStyle.husk : .gray)
        .accessibilityIdentifier((bot.archived ? "unarchive-bot-" : "archive-bot-") + bot.id)
    }

    /// Mute or Unmute, beside Archive, as "Hide Alerts" in Messages and Mute in Telegram (BotChatMute).
    private func muteButton(_ bot: Bot) -> some View {
        Button { Task { await store.muteBot(bot, muted: bot.notifications) } } label: {
            Label(BotChatMute.label(bot), systemImage: BotChatMute.symbol(bot))
        }
        .tint(BoardStyle.husk)
        .accessibilityIdentifier("mute-bot-\(bot.id)")
    }

    /// Mute or Unmute for a named chat of a team: the same choice as its menu (TeamChatMuteMenu).
    private func teamMuteButton(_ mute: TeamChatMute.ListAction) -> some View {
        Button {
            store.error = nil
            Task { await store.setTeamChatNotifications(mute.room, chat: mute.chat, enabled: mute.enabled) }
        } label: {
            Label(mute.label, systemImage: mute.symbol)
        }
        .tint(BoardStyle.husk)
        .disabled(TeamChatMute.waits(mute.chat, store.teamChatMuteChoices))
        .accessibilityIdentifier("chat-row-mute-swipe")
    }

    /// "team-chat-<room>" for a team, "team-chat-<room>-<chat>" for a named chat, "direct-chat-<member>" for a direct
    /// chat, "bot-direct-chat-<team bot ID>" for a direct chat with a bot.
    static func identifier(_ chat: TeamChatKey) -> String {
        if let bot = chat.bot { return "bot-direct-chat-" + bot }
        return chat.member.map { "direct-chat-" + $0 } ?? "team-chat-" + chat.room + (chat.chat.map { "-" + $0 } ?? "")
    }

}

/// The two tabs of Chats. Working has the agents and chats that work or wait for the person, and Idle has the
/// others, the archived chats among them, so the person finds a free agent for the next task in one tap.
/// The tree of Chats: many agents of one project fold into one row with the name of the project (the person: "tree to
/// group if many with one type of agent"). A project with `minimum` idle agents or more has that row at the place of
/// the first of them, and they show under it, in the order of the list, while the row is open. A project with fewer
/// keeps its agents as rows of their own. A working agent and the chats with people never fold, so a tree never hides
/// a chat that works.
enum ChatTree {
    /// "Many": the fewest agents of one project that fold. Two rows read fine without a tree.
    static let minimum = 3
    static let key = "chat-tree-open"

    /// The row of a project and the agents under it, in the order of the list.
    struct Group: Identifiable, Equatable {
        let agentId: String
        let name: String
        let bots: [Bot]
        let open: Bool
        var id: String { "tree-" + agentId }
    }

    /// One row of the list: a chat, set in when it is in an open tree, or the row of a project.
    enum Entry: Identifiable, Equatable {
        case chat(ChatListItem, nested: Bool)
        case group(Group)

        var id: String {
            switch self {
            case .chat(let item, _): item.id
            case .group(let group): group.id
            }
        }
    }

    /// The rows of the list, `items` in its order. `open` has the projects whose tree is open, and `loose` the chats
    /// that never fold: the chats that work.
    static func entries(_ items: [ChatListItem], open: Set<String>, loose: Set<String> = []) -> [Entry] {
        var members: [String: [Bot]] = [:]
        for item in items where !loose.contains(item.id) {
            if case .bot(let bot) = item { members[bot.agentId, default: []].append(bot) }
        }
        var placed = Set<String>()
        var entries: [Entry] = []
        for item in items {
            guard !loose.contains(item.id), case .bot(let bot) = item, let bots = members[bot.agentId],
                  bots.count >= minimum else {
                entries.append(.chat(item, nested: false))
                continue
            }
            guard placed.insert(bot.agentId).inserted else { continue }
            let isOpen = open.contains(bot.agentId)
            // Each chat has the name of its project, also a chat whose project is gone (AgentChats.orphans).
            let name = bots.first { $0.id == bot.agentId }?.name ?? bots[0].name
            entries.append(.group(Group(agentId: bot.agentId, name: name, bots: bots, open: isOpen)))
            if isOpen { entries += bots.map { .chat(.bot($0), nested: true) } }
        }
        return entries
    }

    /// The name of an agent in the tree of its project: the title of a further chat. The first chat of the project
    /// has the name of the project.
    static func title(_ bot: Bot) -> String { bot.isChat ? bot.chatLabel : bot.name }

    /// The projects whose tree is open, from the kept value: one project on each line. A line of an older build names
    /// its tab first ("idle/<project>"), and a tree that was open in either tab stays open.
    static func open(_ kept: String) -> Set<String> {
        Set(kept.split(separator: "\n").map { project($0) })
    }

    /// The kept value with the tree of `agentId` opened or closed.
    static func toggle(_ agentId: String, _ kept: String) -> String {
        setting(agentId, open: !open(kept).contains(agentId), kept)
    }

    /// The kept value with the tree of `agentId` open.
    static func opening(_ agentId: String, _ kept: String) -> String {
        setting(agentId, open: true, kept)
    }

    private static func setting(_ agentId: String, open: Bool, _ kept: String) -> String {
        var lines = kept.split(separator: "\n").filter { project($0) != agentId }.map(String.init)
        if open { lines.append(agentId) }
        return lines.joined(separator: "\n")
    }

    /// The project of a kept line, without the tab of an older build.
    private static func project(_ line: Substring) -> String {
        String(line.split(separator: "/").last ?? line)
    }
}

/// The row of a project whose agents fold on Chats (ChatTree), in the form of every other row: a chevron where a face
/// would be, the name of the project, how many of its agents fold, and the age of the newest with their unread
/// messages. A tap opens or closes the tree. A starred project keeps its star mark.
struct ChatTreeRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The width of the chevron. A chat in the tree starts after it.
    static let chevronWidth: CGFloat = 16
    @ScaledMetric(relativeTo: .body) private var chevron = ChatTreeRow.chevronWidth
    let group: ChatTree.Group
    let toggle: () -> Void

    var body: some View {
        let unread = group.bots.reduce(0) { $0 + BotChatMute.unread($1) }
        let newest = group.bots.compactMap { store.chatSummaries[$0.id].newestDate ?? $0.createdAt.apiDate }.max()
        let waiting = group.bots.filter { store.botNeedsYou($0) }.count
        let starred = store.bots.first { $0.id == group.agentId }.map(ChatList.isStarred) ?? false
        let count = group.bots.count == 1 ? "1 agent" : "\(group.bots.count) agents"
        Button(action: toggle) {
            ChatRowLayout(name: group.name, unread: unread, date: newest, pinned: starred) {
                Image(systemName: "chevron.right")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(group.open ? 90 : 0))
                    .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: group.open)
                    .frame(width: chevron)
            } subtitle: {
                // A card that waits in the tree says so, as the row of the chat would.
                (Text(count) + Text(waiting > 0 ? " · " + (waiting == 1 ? "1 needs you" : "\(waiting) need you") : "")
                    .foregroundStyle(BoardStyle.attention))
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            .padding(.vertical, BoardStyle.Space.m)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel([group.name, count, waiting > 0 ? "\(waiting) need you" : nil,
                             unread > 0 ? UnreadBadge.spoken(unread) : nil].compactMap { $0 }.joined(separator: ", "))
        .accessibilityValue(group.open ? "Expanded" : "Collapsed")
        .accessibilityIdentifier("chat-tree-" + group.agentId)
    }
}

/// A Send that starts a chat leaves the chat closed (sendNewChat): the Send of the message box, and Send to a new agent
/// in a chat, which keeps the person in that chat. The Chats list then opens the tree of its project (ChatTree), for a
/// chat that does not work yet, and scrolls its row into view: the chat is the newest, so it is the first row under the
/// starred chats or the first row of its tree.
private struct NewChatReveal: ViewModifier {
    @Environment(AppStore.self) private var store
    /// Opens the tree of the project of the chat.
    let open: (Bot) -> Void

    func body(content: Content) -> some View {
        ScrollViewReader { proxy in
            content.onChange(of: store.newChatSend) { _, send in
                guard let send, let bot = store.bots.first(where: { $0.id == send.chat }) else { return }
                open(bot)
                Task {
                    // The row is in the list after the update that the open tree makes.
                    try? await Task.sleep(for: .milliseconds(150))
                    withAnimation { proxy.scrollTo(ChatListItem.bot(bot).id) }
                }
            }
        }
    }
}

extension View {
    /// A row of the Chats list: the paper behind it, no line under it, and the side space of the page.
    func homeRow(inset: CGFloat = BoardStyle.Space.l, top: CGFloat = 0) -> some View {
        listRowInsets(EdgeInsets(top: top, leading: inset, bottom: 0, trailing: inset))
            .listRowSeparator(.hidden)
            .listRowBackground(BoardStyle.paper)
    }
}

struct HomePressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.55 : 1)
    }
}

/// The pull requests on Product: the open pull requests of the engineering report, which the dashboards repository
/// publishes. Product is their one tab. They add nothing to the badge of the tab, which counts the to-dos of Product
/// (TodoCounts): a pull request is the state of the product, not a to-do. The issues of the product are its to-dos.
enum ProductWork {
    /// The reports that Product reads.
    static let reports = ["engineering"]

    struct Change: Equatable {
        let additions: String
        let deletions: String
    }

    /// One row: an issue or a pull request.
    struct Item: Identifiable, Equatable {
        let id: String
        let title: String
        /// The number, the state, the person and the day, in one line.
        let detail: String
        let symbol: String
        let tint: Color
        let url: URL?
        /// The lines that a pull request adds and removes.
        var change: Change? = nil
    }

    /// The open pull requests of every repository of the report, newest change first. The symbol is the state of the
    /// checks. A row names its repository when more than one repository has a pull request open.
    static func pulls(_ data: BusinessValue?, now: Date = .now) -> [Item] {
        guard let data else { return [] }
        let repositories = data["data"].object.filter { !$0.value["pullRequests"]["nodes"].array.isEmpty }
        let named = repositories.count > 1
        let pulls = repositories.flatMap { key, repository in
            repository["pullRequests"]["nodes"].array.map { (key: key, name: repository["name"].text.isEmpty ? key : repository["name"].text, pull: $0) }
        }
        return pulls.sorted { ($0.pull["updatedAt"].text, $0.pull["url"].text) > ($1.pull["updatedAt"].text, $1.pull["url"].text) }
            .map { key, name, node in
                let pull = PullRequestRow(node)
                let number = node["number"].number.map { "#\(Int($0))" }
                let author = node["author"]["login"].text
                let day = node["updatedAt"].text.apiDate.map { BusinessFormat.date($0, now: now) }
                let detail = [named ? [name, number].compactMap { $0 }.joined(separator: " ") : number,
                              author.isEmpty ? nil : author, pull.state, day].compactMap { $0 }.joined(separator: " · ")
                let tint: Color = switch pull.checks {
                case "SUCCESS": BoardStyle.positive
                case "FAILURE", "ERROR": BoardStyle.attention
                default: .secondary
                }
                return Item(id: "pull-" + key + "-" + (number ?? node["url"].text), title: pull.title, detail: detail,
                            symbol: pull.checkSymbol, tint: tint, url: pull.url,
                            change: Change(additions: pull.additions, deletions: pull.deletions))
            }
    }
}

func latestConversationMessage(in messages: [Message]) -> Message? {
    let textMessages = messages.filter(isPreviewText)
    let content = textMessages.filter { !isCompletionLabel($0) }
    return (content.isEmpty ? textMessages : content).max {
        ($0.createdAt.apiDate ?? .distantPast) < ($1.createdAt.apiDate ?? .distantPast)
    }
}

/// The second line of a home row. A pending card comes first, because the face already shows work.
enum BotRowSubtitle: Equatable {
    case needsYou
    case phase(BotPhase)
    case health(BotHealth)
    case preview

    init(needsYou: Bool, phase: BotPhase?, health: BotHealth? = nil) {
        if needsYou { self = .needsYou } else if let phase { self = .phase(phase) } else if let health { self = .health(health) } else { self = .preview }
    }

    /// VoiceOver reads what the row shows. It reads the unread count only when there is one.
    func accessibilityLabel(bot: Bot, preview: String, queued: Int = 0) -> String {
        var parts = [bot.name]
        switch self {
        case .needsYou: parts.append(StatusLabel.label(for: "needs_you"))
        case .phase(let phase): parts += [phase.label] + [MessageQueue.label(queued)].compactMap { $0 }
        case .health(let health): parts.append(health.label)
        case .preview: parts += [bot.status.replacingOccurrences(of: "_", with: " "), preview]
        }
        if !bot.notifications { parts.append(TeamChatMute.spoken) }
        if BotChatMute.unread(bot) > 0 { parts.append(UnreadBadge.spoken(bot.unreadCount)) }
        return parts.joined(separator: ", ")
    }
}

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot
    /// The name of the row. A further chat of a project shows its title next to the name of the project.
    var title: String? = nil
    /// The star mark of a starred project at the top of the Chats list.
    var pinned = false
    /// The row reads the summary of its bot, not the messages, so a streamed word does not redraw it.
    private var summary: BotChatSummary { store.chatSummaries[bot.id] }
    private var latestMessage: Message? { summary.latestMessage }
    /// The line under the name: never empty.
    private var preview: String { ChatRowText.line(newestText) }
    private var newestText: String {
        guard let message = latestMessage else {
            return TranscriptPresentation.previewText(
                TranscriptPresentation.previewFallback(bot.lastMessage, streaming: summary.streaming))
        }
        let sender = message.senderBotId.flatMap { id in id == bot.id ? nil : store.bots.first { $0.id == id }?.name ?? "Deleted project" }
        let prefix = message.role == "user" ? "You: " : sender.map { $0 + ": " } ?? ""
        let body = AttachmentOnly.isMarker(message.text) ? "Photo" : TranscriptPresentation.previewText(message.text)
        return prefix + (message.photo?.preview ?? body)
    }
    private var lastMessageDate: Date? { summary.newestDate }
    private var phase: BotPhase? { store.botPhase(bot) }

    var body: some View {
        let subtitle = BotRowSubtitle(needsYou: store.botNeedsYou(bot), phase: phase, health: bot.health)
        // A working chat dates its row by its last action, which the line under the name names, not by its newest
        // message: a web search that began 23 seconds ago shows 23s, as the working line of the chat does.
        let step: Date? = if case .phase = subtitle { store.botStepStart(bot) } else { nil }
        // A project has no face: its name and its state are the row.
        ChatRowLayout(name: title ?? bot.name, unread: BotChatMute.unread(bot), date: lastMessageDate, step: step,
                      muted: !bot.notifications, pinned: pinned) {
            EmptyView()
        } subtitle: {
            Group {
                // A pending card or a working phase takes the place of the last message, like a typing notice.
                switch subtitle {
                case .needsYou:
                    StatusLabel(status: "needs_you", compact: true)
                        .transition(.opacity)
                        .accessibilityIdentifier("bot-needs-you-\(bot.id)")
                case .phase(let phase):
                    HStack(spacing: BoardStyle.Space.xs) {
                        ShimmerText(text: phase.label)
                        // The messages of the person that wait for the next turn.
                        if let queued = MessageQueue.label(store.queuedCount(bot.id)) {
                            Text("· " + queued).foregroundStyle(.secondary).lineLimit(1)
                                .accessibilityIdentifier("bot-queued-\(bot.id)")
                        }
                    }
                    .font(.subheadline)
                    .transition(.opacity)
                case .health(let health):
                    Text(health.label)
                        .font(.subheadline).foregroundStyle(BoardStyle.attention)
                        .lineLimit(1)
                        .transition(.opacity)
                        .accessibilityIdentifier("bot-health-\(bot.id)")
                case .preview:
                    Text(preview)
                        .font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                        .transition(.opacity)
                }
            }
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: subtitle)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, preview: subtitle == .preview ? preview : "",
                                                        queued: store.queuedCount(bot.id)))
    }
}

enum ChatRowText {
    /// The line under the name. A chat without a message says so, for a bot as for a team, so no row has an empty line.
    static func line(_ preview: String) -> String {
        preview.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? TeamChatPreview.empty : preview
    }
}

/// The layout of one row of the Chats list and of the Mac sidebar, for a bot and a team, as the chat list of WhatsApp.
/// The face comes first. A grid of two lines follows. The name and the age of the newest message share the first line.
/// A working chat shows the time of its newest step there instead, to the second and live (`step`).
/// The line under the name and the badges share the second line. The badges are the mark of a muted chat
/// (`bell.slash`), the round "@" mark of a team whose unread message names this person, and then the unread count. WhatsApp and Telegram have the same mark. The age
/// and the badges end at one right edge. No line is empty, so a row of one line has its text centered on the face.
/// At the accessibility text sizes the name, the age with the badges, and the line under the name each have their
/// own line.
struct ChatRowLayout<Face: View, Subtitle: View>: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let name: String
    let unread: Int
    let date: Date?
    /// The start of the step that a working chat does now, which the line under the name names. The row then shows the
    /// time since it started (23s, 4m 05s), as the working line of the chat does.
    var step: Date? = nil
    var mentioned = false
    /// The person muted the chat (TeamChatMute).
    var muted = false
    /// The chat is pinned and shows at the top of the list (TeamChatPin).
    var pinned = false
    @ViewBuilder let face: () -> Face
    @ViewBuilder let subtitle: () -> Subtitle

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            face()
            if dynamicTypeSize.isAccessibilitySize {
                // The badge grows with the text and stands on its own line, so it covers no name.
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    nameText
                    if hasAge || hasBadges {
                        HStack(spacing: BoardStyle.Space.s) {
                            if hasAge { ageText }
                            badges
                        }
                    }
                    subtitle()
                }
            } else {
                // The name and the age share the first baseline. The badges fill the second line: a badge is as high
                // as the line under the name. The gap between the columns keeps the text clear of the wider of the
                // age and the badges.
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: BoardStyle.Space.l,
                     verticalSpacing: BoardStyle.Space.line) {
                    GridRow {
                        nameText.frame(maxWidth: .infinity, alignment: .leading)
                        if hasAge {
                            ageText.fixedSize().gridColumnAlignment(.trailing)
                        }
                    }
                    GridRow(alignment: .center) {
                        subtitle()
                        if hasBadges {
                            // The row centers its cells. A frame of no height adds no height to the line. A badge thus
                            // does not move the text and does not make the row taller.
                            badges.fixedSize().frame(height: 0).gridColumnAlignment(.trailing)
                        }
                    }
                }
            }
        }
        .frame(minHeight: 52)
    }

    private var hasBadges: Bool { unread > 0 || mentioned || muted || pinned }
    private var hasAge: Bool { step != nil || date != nil }

    private var nameText: some View {
        Text(name).font(.body.weight(unread > 0 ? .semibold : .regular)).lineLimit(1)
    }

    private var ageText: some View {
        Group {
            if let step {
                StepTimeLabel(start: step)
            } else if let date {
                CompactAgeLabel(date: date)
            }
        }
        .font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
    }

    /// The mark of a muted chat, the "@" mark, then the unread count.
    private var badges: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if muted { MutedMark() }
            if pinned { MutedMark(symbol: "star") }
            if mentioned { MentionBadge() }
            if unread > 0 { UnreadBadge(count: unread) }
        }
    }
}

/// The mark of a muted chat in the secondary color, as WhatsApp has it: in the font of the line under the name, so it
/// is as high as that line. The label of the row says it to VoiceOver (TeamRowSpeech), so the mark itself is silent.
struct MutedMark: View {
    /// The mark of a pinned chat at the top of the list uses the same form with the line symbol `pin`.
    var symbol = TeamChatMute.mark

    var body: some View {
        Image(systemName: symbol)
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .accessibilityHidden(true)
    }
}

/// The mark of a team whose unread message names this person: an "@" in a circle of the size and the fill of the
/// unread badge. The label of the row says it to VoiceOver (TeamRowSpeech), so the mark itself is silent.
struct MentionBadge: View {
    @ScaledMetric(relativeTo: .subheadline) private var size = BoardStyle.badge

    var body: some View {
        Text("@")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(BoardStyle.controlLabel)
            .frame(width: size, height: size)
            .background(UnreadBadge.fill, in: Circle())
            .accessibilityHidden(true)
    }
}

/// The unread count of a chat: a filled round badge in the green of the New line of a chat, with digits in the paper
/// color. One digit gives a circle, more digits a capsule of the same height, and over 99 shows "99+".
struct UnreadBadge: View {
    let count: Int
    @ScaledMetric(relativeTo: .subheadline) private var size = BoardStyle.badge

    var body: some View {
        let text = Self.text(count)
        Text(text)
            .font(.caption2.weight(.semibold).monospacedDigit())
            .foregroundStyle(BoardStyle.controlLabel)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, Self.sidePadding(text))
            .frame(minWidth: size, minHeight: size)
            // A plain color: a Mac sidebar draws `Color.primary` with the vibrancy of its material, which would make
            // the fill of the badge pale.
            .background(Self.fill, in: Capsule())
            .accessibilityElement()
            .accessibilityLabel(Self.spoken(count))
            .accessibilityIdentifier("unread-badge")
    }

    /// The green of the New line of a chat (BoardStyle.green), never black. Unread is green everywhere.
    static let fill = BoardStyle.green

    /// The digits of the badge. Over 99 shows "99+".
    nonisolated static func text(_ count: Int) -> String { count > 99 ? "99+" : "\(count)" }

    /// The space at each side of the digits. One character has none, so the badge is a circle of its height. More
    /// characters give a capsule.
    nonisolated static func sidePadding(_ text: String) -> CGFloat { text.count > 1 ? BoardStyle.Space.s : 0 }

    /// What VoiceOver says for the count of a row, a tile and the badge.
    nonisolated static func spoken(_ count: Int) -> String { count == 1 ? "1 unread message" : "\(count) unread messages" }
}

struct BotActivityDot: View {
    @Environment(AppStore.self) private var store
    @ScaledMetric(relativeTo: .footnote) private var size = 10
    let bot: Bot
    var body: some View {
        if store.isBotActive(bot) {
            Circle().fill(BoardStyle.positive).frame(width: size, height: size)
                .overlay(Circle().stroke(BoardStyle.paper, lineWidth: 2))
                .offset(x: 2, y: 2)
                .accessibilityElement().accessibilityLabel("Working")
                .accessibilityIdentifier("bot-active-\(bot.id)")
        }
    }
}
