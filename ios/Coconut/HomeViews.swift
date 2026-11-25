import SwiftUI

/// Two tabs of the iPhone, and the page of the archived chats. Product has the rules that breach for the agents and the
/// cards that wait, then its to-dos, the issues of gumroad-private with the label product (TodoLists.swift), and the
/// open pull requests (ProductWork). Chats has the sections of Slack: one for each project, the chats with people,
/// Personal at the bottom, then one row Archived that opens the archived chats on a page of their own
/// (ChatList.sections), with the message box under the list (NewChatBar). A folded section shows only the real work.
/// Its top right has Working, which lists only the chats that work, and the grid of the agents that work (ChatGrid).
struct HomeLaunchView: View {
    /// What the view shows: Product, Chats, or the archived chats, which the last row of Chats opens.
    enum Part { case product, chats, archived }
    var part = Part.chats
    @Environment(AppStore.self) private var store
    /// The sections of Chats that the person opened (ChatListSection). The phone keeps them.
    @AppStorage(ChatListSection.key) private var openSections = ""
    /// The Working filter at the top right of Chats (WorkingFilterButton). The phone keeps it.
    @AppStorage(WorkingFilterButton.key) private var workingOnly = false
    /// The row of Chats that the person opened last, or the chat that a Send just made. A folded section keeps it, so
    /// the row is still there when the person comes back from the chat, as "Unread" keeps the open chat on the Mac.
    @State private var keptChat: String?
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
        case .chats: chatList(sections)
        case .archived: archivedList(sections.first { $0.kind == .archived })
        }
    }

    /// The sections of Chats (ChatList.sections). The page of the archived chats shows the rows of Archived.
    private var sections: [ChatListSection] {
        ChatList.sections(bots: store.bots, latest: store.chatSummaries.newestActivity,
                          rooms: store.shownTeamRooms, local: store.newestLocalMessages)
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
        // Brainstorm in the place of New to-do (the person's request): a talk by voice with the model of a project that
        // ends as the next steps in the message box of Product, to send to an agent that builds them (Brainstorm.swift).
        // Finance and Support have no + either: agents add the to-dos of the three lists.
        .brainstormButton()
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

    /// The list of Chats in the sections of Slack (the person, 2026-11-25: "Chats should look/be more like slack and
    /// almost everything should be archived / hidden to focus on real work. Sections by project and personal agents at
    /// bottom"): a section for each project, Coconut first, then the chats with people, the agents of an earlier release,
    /// Personal at the bottom, and Archived (ChatList.sections). A tap on the title of a section opens or folds it, and
    /// the phone keeps the sections that the person opened, so every section starts folded. A folded section still shows
    /// its real work: a chat that works or whose card waits, an unread chat, a starred project, a pinned chat of a team,
    /// the Coconut chat and the chat that the person opened last (stays). An idle chat is one tap away, and Archived is
    /// one row at the end with the number of its chats, which opens them on a page of their own. Every row has the form
    /// of a chat row: the name, the last message or the step, the age and the badges. The list has no tabs or toggles
    /// inside it, because the person found that nobody taps them (2026-11-24), and a chat archives itself after the time
    /// that You sets (AutoArchiveMenu, 14 hours unless the person picks another). The top right has
    /// the two buttons that the person asked for that evening: Working, which lists only the chats that work in one
    /// list without section titles (WorkingFilterButton), and the grid of the agents that work (ChatGridButton).
    private func chatList(_ sections: [ChatListSection]) -> some View {
        let now = Date.now
        let open = ChatListSection.open(openSections)
        // The chats that work, in the order of the sections, for the Working filter.
        let working = workingOnly ? sections.flatMap(\.items).filter { works($0, at: now) } : []
        // A list, not a stack, so that a row of a chat has the swipe of the system: swipe left to archive.
        return List {
            if workingOnly {
                if working.isEmpty {
                    Text("No agent is working.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                        .accessibilityIdentifier("chats-working-empty")
                        .homeRow()
                }
                ForEach(working) { item in chatRow(item, in: nil) }
            } else {
                if sections.isEmpty {
                    Text("No chats yet.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                        .accessibilityIdentifier("chats-empty")
                        .homeRow()
                }
                // Archived is no section here: it is one row at the end, and a tap opens its chats on a page of their
                // own (the person: "Archived should be behind a tap just one row with number at bottom of chats").
                let listed = sections.filter { $0.kind != .archived }
                ForEach(listed) { section in
                    let isOpen = section.alwaysOpen || open.contains(section.id)
                    ChatSectionHeader(section: section, open: isOpen, first: section.id == listed.first?.id) {
                        withAnimation(.snappy) { openSections = ChatListSection.toggle(section.id, openSections) }
                    }
                    .homeRow()
                    ForEach(section.shown(open: isOpen) { stays($0, at: now) }) { item in
                        chatRow(item, in: section)
                    }
                }
                if let archived = sections.first(where: { $0.kind == .archived }) {
                    ArchivedChatsRow(count: archived.items.count, first: listed.isEmpty) {
                        pushRoute?(ArchivedChatsRoute())
                    }
                    .homeRow()
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.s, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        .navigationTitle(workingOnly ? "Working" : "Chats")
        .toolbar {
            // One tap reads every chat, as Read All in Telegram. It shows while a chat of the list has an unread
            // count. A folded section shows its unread chats, so each one has a row.
            if sections.contains(where: { $0.items.contains { $0.unreadCount > 0 } }) {
                ToolbarItem(placement: .primaryAction) {
                    MarkAllReadButton().accessibilityIdentifier("chats-mark-all-read")
                }
            }
            ToolbarItem(placement: .primaryAction) { WorkingFilterButton(on: $workingOnly) }
            ToolbarItem(placement: .primaryAction) { ChatGridButton() }
        }
        .refreshable { await store.reconnect() }
        // The message box under the list gives the keyboard back when the list moves.
        .scrollDismissesKeyboard(.interactively)
        // A new chat shows in its section, also a folded one, and the list scrolls to it.
        .modifier(NewChatReveal { bot in keptChat = ChatListItem.bot(bot).id })
    }

    /// The archived chats, newest first, on the page that the last row of Chats opens (ArchivedChatsRow). Each row is
    /// the row of a chat with its agent's name: a tap opens the chat, and a swipe offers Unarchive, which takes the chat
    /// back to its section, and Mute. A message of the person brings an archived chat back too (agent_chats.py).
    private func archivedList(_ section: ChatListSection?) -> some View {
        List {
            if let section {
                ForEach(section.items) { item in chatRow(item, in: section) }
            } else {
                Text("No archived chats.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .accessibilityIdentifier("archived-empty")
                    .homeRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.s, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        .navigationTitle("Archived")
        .refreshable { await store.reconnect() }
    }

    /// A chat that a folded section still shows: the real work, and what the person keeps at hand.
    private func stays(_ item: ChatListItem, at date: Date) -> Bool {
        if item.id == keptChat || item.unreadCount > 0 || item.isPinnedChat || works(item, at: date) { return true }
        if case .bot(let bot) = item { return ChatList.isCoconut(bot) || ChatList.isStarred(bot) }
        return false
    }

    /// One chat of the list. The title of a project's section names the project, so a further chat of the project
    /// shows its own title (ChatListSection.title). The Working list has no section titles, so there a chat names its
    /// agent too (Bot.listName). A tap opens the chat, and the folded section keeps its row.
    @ViewBuilder
    private func chatRow(_ item: ChatListItem, in section: ChatListSection?) -> some View {
        switch item {
        case .bot(let bot):
            BotContactRow(bot: bot, title: section?.title(of: bot) ?? bot.listName, pinned: ChatList.isStarred(bot))
                .padding(.vertical, BoardStyle.Space.m)
                .background(BoardStyle.paper)
                .modifier(ChatDeckRow(botId: bot.id) {
                    keptChat = item.id
                    pushRoute?(ConversationRoute(botId: bot.id))
                })
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
                Button {
                    keptChat = item.id
                    pushRoute?(TeamChatRoute(chat: chat))
                } label: {
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

    /// A chat works while its row says so (AppStore.chatWorks), as in the sidebar of the Mac.
    private func works(_ item: ChatListItem, at date: Date) -> Bool { store.chatWorks(item, at: date) }

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

    /// Archive, or Unarchive for a chat that is archived. An archived chat moves to Archived, the page that the last row
    /// of the list opens, and Unarchive takes it back to its section. It runs no routine and sends no notification until
    /// a message of the person brings it back (agent_chats.py).
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

/// A section of Chats, as a section of the sidebar of Slack: the chats of one project, the chats with people, the
/// agents of an earlier release, or Archived (ChatList.sections). The title folds and opens it (ChatSectionHeader).
/// Every section starts folded, and a folded section shows only the chats that the list keeps in sight
/// (HomeLaunchView.stays). The phone keeps the sections that the person opened: one section on each line. Archived
/// never unfolds in the list: it is one row that opens its chats on a page of their own (ArchivedChatsRow).
struct ChatListSection: Identifiable, Equatable {
    enum Kind: Equatable {
        /// The chats that the person starred, as the Starred section of Slack: the starred projects and the pinned
        /// chats of the teams. It comes first, and it stays open: the person chose these chats to have them at hand.
        case starred
        /// The chats of a project, its own chat first: the ID of the project.
        case project(String)
        /// The teams, their named chats, and the direct chats with a person or with a bot of a team.
        case people
        /// The agents of an earlier release, which are no project, with their chats, and the chats whose project is
        /// gone (AgentChats.orphans).
        case others
        /// The archived chats and agents, newest activity first. Chats has them on their own page behind one row
        /// (ArchivedChatsRow), and the Mac sidebar in its folded Archived (ChatSections.archived).
        case archived
    }

    static let key = "chat-sections-open"

    let kind: Kind
    let title: String
    let items: [ChatListItem]

    /// The section holds the chats that the person chose, so it shows them without a tap, as the Starred of Slack.
    var alwaysOpen: Bool { kind == .starred }

    var id: String {
        switch kind {
        case .starred: "starred"
        case .project(let id): "project-" + id
        case .people: "people"
        case .others: "others"
        case .archived: "archived"
        }
    }

    /// The rows of the section: every chat while it is open, and the chats that `stays` keeps while it is folded.
    func shown(open: Bool, stays: (ChatListItem) -> Bool) -> [ChatListItem] {
        open ? items : items.filter(stays)
    }

    /// The name of the row of a chat. The title of a project's section names the project, so its own chat shows the
    /// name and a further chat its title. Elsewhere a chat names its agent too (Bot.listName).
    func title(of bot: Bot) -> String {
        if case .project = kind { return bot.isChat ? bot.chatLabel : bot.name }
        return bot.listName
    }

    /// The sections that the person opened, from the kept value.
    static func open(_ kept: String) -> Set<String> {
        Set(kept.split(separator: "\n").map(String.init))
    }

    /// The kept value with the section `id` opened or folded.
    static func toggle(_ id: String, _ kept: String) -> String {
        var lines = kept.split(separator: "\n").map(String.init).filter { $0 != id }
        if !open(kept).contains(id) { lines.append(id) }
        return lines.joined(separator: "\n")
    }
}

/// The title of a section of Chats, as in the sidebar of Slack: a chevron that turns down while the section is open,
/// and the name in the quiet color of a title. A tap opens or folds the section. A folded section says nothing about
/// what it holds: its unread and working chats show under it all the same. A section that holds the chats the person
/// chose (ChatListSection.alwaysOpen) keeps its rows in sight and has nothing to fold.
struct ChatSectionHeader: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var chevron: CGFloat = 14
    let section: ChatListSection
    let open: Bool
    /// The first section starts close under the title of the page.
    var first = false
    let toggle: () -> Void

    var body: some View {
        if section.alwaysOpen {
            label
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("chat-section-" + section.id)
        } else {
            Button(action: toggle) { label }
                .buttonStyle(HomePressStyle())
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                .accessibilityValue(open ? "Expanded" : "Collapsed")
                .accessibilityIdentifier("chat-section-" + section.id)
        }
    }

    private var label: some View {
        HStack(spacing: BoardStyle.Space.s) {
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.secondary)
                .rotationEffect(.degrees(open ? 90 : 0))
                .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: open)
                .frame(width: chevron)
            Text(section.title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.top, first ? BoardStyle.Space.s : BoardStyle.Space.l)
        .padding(.bottom, BoardStyle.Space.s)
        .background(BoardStyle.paper)
        .contentShape(Rectangle())
    }

    /// What VoiceOver reads for the number of Archived, for example "12 chats".
    static func chats(_ count: Int) -> String { count == 1 ? "1 chat" : "\(count) chats" }
}

/// The last row of Chats: Archived, with the number of the archived chats on the right. A tap opens them on a page of
/// their own (ArchivedChatsRoute), so they never fill the list (the person: "Archived should be behind a tap just one
/// row with number at bottom of chats"). The row has the line of a section title, with a box where a section has its
/// chevron, because it opens a page and folds nothing.
struct ArchivedChatsRow: View {
    @ScaledMetric(relativeTo: .subheadline) private var symbol: CGFloat = 14
    let count: Int
    /// The row starts close under the title of the page when no section is above it.
    var first = false
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: BoardStyle.Space.s) {
                Image(systemName: "archivebox")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: symbol)
                    .accessibilityHidden(true)
                Text("Archived")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                Text(count, format: .number)
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .accessibilityLabel(ChatSectionHeader.chats(count))
            }
            .padding(.top, first ? BoardStyle.Space.s : BoardStyle.Space.l)
            .padding(.bottom, BoardStyle.Space.s)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("chats-archived")
    }
}

/// A Send that starts a chat leaves the chat closed (sendNewChat): the Send of the message box, and Send to a new agent
/// in a chat, which keeps the person in that chat. The Chats list keeps the new chat in sight in its section, also a
/// folded one, and scrolls its row into view: the chat is the newest of its project, so it is the first row under the
/// project's own chat.
private struct NewChatReveal: ViewModifier {
    @Environment(AppStore.self) private var store
    /// Keeps the chat in sight in its section.
    let keep: (Bot) -> Void

    func body(content: Content) -> some View {
        ScrollViewReader { proxy in
            content.onChange(of: store.newChatSend) { _, send in
                guard let send, let bot = store.bots.first(where: { $0.id == send.chat }) else { return }
                keep(bot)
                Task {
                    // The row is in the list after the update that keeping it makes.
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

    /// VoiceOver reads what the row shows: its name (`name`, the title of a further chat in its project's section), and
    /// the unread count only when there is one.
    func accessibilityLabel(bot: Bot, name: String? = nil, preview: String, queued: Int = 0) -> String {
        var parts = [name ?? bot.name]
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
    /// The line under the name: never empty.
    private var preview: String { Self.preview(of: bot, in: store) }

    /// The newest message of a chat in one line, with "You: " or the name of another project that wrote it. A card of
    /// the grid of the working agents (ChatGridLabel) shows it too, once its agent stops.
    static func preview(of bot: Bot, in store: AppStore) -> String {
        let summary = store.chatSummaries[bot.id]
        guard let message = summary.latestMessage else {
            return ChatRowText.line(TranscriptPresentation.previewText(
                TranscriptPresentation.previewFallback(bot.lastMessage, streaming: summary.streaming)))
        }
        let sender = message.senderBotId.flatMap { id in id == bot.id ? nil : store.bots.first { $0.id == id }?.name ?? "Deleted project" }
        let prefix = message.role == "user" ? "You: " : sender.map { $0 + ": " } ?? ""
        let body = AttachmentOnly.isMarker(message.text) ? "Photo" : TranscriptPresentation.previewText(message.text)
        return ChatRowText.line(prefix + (message.photo?.preview ?? body))
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
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, name: title,
                                                        preview: subtitle == .preview ? preview : "",
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
    /// time since it started (23s, 4m 5s), as the working line of the chat does.
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
