import SwiftUI

/// Two tabs of the iPhone, and the page that the row Archived of Chats opens. Product has the rules that breach for the
/// agents and the cards that wait, then its to-dos, the issues of gumroad-private with the label product
/// (TodoLists.swift), and the open pull requests (ProductWork). Chats is a tree (the person, 2026-11-25: "Everything
/// should be nested", then "Nested don't look nested"): Starred with its chats, Unread with the chats that the person
/// has not read, then one row for each project, the chats with people, the agents of an earlier release and Personal at
/// the bottom, which each open in place with their active chats set in under them and Load more for the others, and
/// Archived, which opens its chats on a page of their own (ChatSectionRow, ChatList.sections), with the message box
/// under the list (NewChatBar). Its top
/// right has the filter (ChatsFilterMenu): Unreads, which shows only Starred and Unread, and Working, which lists only
/// the chats that work. Beside it is the grid of the agents that work (ChatGrid).
struct HomeLaunchView: View {
    /// What the view shows: Product, Chats, or the chats of one section on a page, Archived, which its row opens.
    enum Part: Equatable { case product, chats, section(ChatSectionRoute) }
    var part = Part.chats
    /// What the field at the top of Chats searches (OmniSearch). Chats lists its hits in place of its rows.
    var query = ""
    @Environment(AppStore.self) private var store
    /// The filters at the top right of Chats, Working and Unreads, one at a time (ChatsFilterMenu). The phone keeps them.
    @AppStorage(ChatsFilterMenu.workingKey) private var workingOnly = false
    @AppStorage(ChatsFilterMenu.unreadsKey) private var unreadsOnly = false
    /// The sections of Chats that the person opened, one on each line (ChatListSection.open). The phone keeps them.
    @AppStorage(ChatSectionRow.key) private var openSections = ""
    /// The open sections of Chats that show all their chats, after a tap on Load more (ChatSectionMore). A section
    /// that folds leaves it, so it opens again with only its active chats. The phone does not keep it.
    @State private var loadedSections: Set<String> = []
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openURL) private var openURL
    /// Opens the place of a row of Needs attention, or the cards that wait.
    let open: (HomeTarget) -> Void
    @ScaledMetric(relativeTo: .title3) private var todoIconWidth = 24
    /// The reports of the product work as they loaded here. A demo has no copy in the store (businessReports).
    @State private var productReports: [String: BusinessReport] = [:]
    @State private var productError: String?
    /// The messages that have the words of the search, of the phone and then of the Mac.
    @State private var messageHits: [OmniSearch.MessageHit] = []
    /// The search whose messages came from the Mac too. Until then an empty search says nothing.
    @State private var searchedQuery = ""

    var body: some View {
        switch part {
        case .product: productList
        case .chats: chatList(sections)
        case .section(let route): sectionList(route, section: sections.first { $0.id == route.id })
        }
    }

    /// The sections of Chats (ChatList.sections). The page of a section shows the chats of its section.
    private var sections: [ChatListSection] {
        ChatList.sections(bots: store.bots, latest: store.chatSummaries.newestActivity,
                          rooms: store.shownTeamRooms, local: store.newestLocalMessages)
    }

    private var productList: some View {
        let pulls = ProductWork.pulls(productReport("engineering")?.data)
        return ScrollViewReader { proxy in
            List {
                attention
                // The to-dos of Product, which its badge counts (TodoCounts). They have no title, as on Finance and
                // Support: the rows say what they are (TodoTile). The cards and the alerts of the agents above
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

    /// The list of Chats, a tree (the person, 2026-11-25: "Everything should be nested chat would help A TON", after
    /// "Chats should look/be more like slack and almost everything should be archived / hidden to focus on real work.
    /// Sections by project and personal agents at bottom", then 2026-11-26: "Nested don't look nested"). Each section
    /// is one row (ChatSectionRow): a row for each project, Coconut first, then the chats with people, the agents of an
    /// earlier release and Personal at the bottom. A tap opens a row in place, as a folder of Notes or Reminders: its
    /// chevron turns down and its chats show under it, set in to its title with a line down from its symbol
    /// (ChatListRow). Another tap folds it. Every section starts folded, and the phone keeps the ones that the person
    /// opened. An open section shows its active chats, and Load more shows the others (the person, 2026-11-26: "load
    /// only active by default when expanded with load more"). A section with no active chat opens to all its chats.
    /// A folded row shows what works or waits inside, so a live
    /// chat in a folded section still says so. No row counts its chats (the person, 2026-11-26: "Don't need counts of
    /// chats stressful"). Starred comes first and stays open, because the person chose its chats: a swipe to the
    /// right on any chat stars it, as Pin in Messages and Telegram (the person: "Don't see a way to pin a chat to the
    /// top of list"). Unread follows and stays open too (the person, 2026-11-25: "Put unread chats at top"): every chat
    /// with an unread message that is not starred, newest activity first. Such a chat leaves its section until the
    /// person reads it, as in the Mac sidebar, so each chat shows once (ChatList.tree). Starred and Unread show all
    /// their chats. They have the rows of the sections, so their chats are set in as the chats of a project are, and
    /// the rows under them read as rows of their own. Archived is the last row and opens its chats on a page of their
    /// own (the person: "Archived should be behind a tap just one row with number at bottom of chats"). Its number
    /// went with the others. The list has no tabs or filters inside it, because the person found that nobody taps them
    /// (2026-11-24), and a chat archives itself after the time that You sets (AutoArchiveMenu, 14 hours unless the
    /// person picks another). The top right has the filter (ChatsFilterMenu) and the grid of the agents that work
    /// (ChatGridButton), which the person asked for that evening. The filter has Working, which lists every chat that
    /// works in one list without the rows of the sections, and Unreads (the person, 2026-11-27: "Starred and only show
    /// unread option. Mirror slack almost exactly"), which shows Starred and Unread alone, as Slack shows "Unreads
    /// only" along with everything the person starred, and says so when no chat is unread.
    private func chatList(_ sections: [ChatListSection]) -> some View {
        let now = Date.now
        // The chats that work, in the order of the sections, for the Working filter.
        let working = workingOnly ? sections.flatMap(\.items).filter { works($0, at: now) } : []
        let opened = ChatListSection.open(openSections)
        // Unreads keeps Starred, with all its chats, and Unread.
        let tree = ChatList.tree(sections, latest: store.chatSummaries.newestActivity)
            .filter { !unreadsOnly || $0.kind == .starred || $0.kind == .unread }
        let words = OmniSearch.words(query)
        // A list, not a stack, so that a row of a chat has the swipe of the system: swipe left to archive.
        return List {
            if !words.isEmpty {
                searchRows(words)
            } else if workingOnly {
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
                ForEach(tree) { section in
                    // Archived opens a page, so it never unfolds here.
                    let open = section.alwaysOpen || (section.kind != .archived && opened.contains(section.id))
                    ChatSectionRow(section: section, summary: summary(of: section, at: now), open: open) {
                        if section.kind == .archived {
                            pushRoute?(ChatSectionRoute(section))
                        } else {
                            withAnimation(.snappy) {
                                openSections = ChatListSection.toggle(section.id, openSections)
                                loadedSections.remove(section.id)
                            }
                        }
                    }
                    .id(section.id)
                    .homeRow()
                    if open {
                        // Starred and Unread show every chat: the person chose them, or has not read them. A section
                        // with no active chat shows them all, because Load more alone read as an empty section
                        // (Tastelint on #598).
                        let isActive = { (item: ChatListItem) in active(item, at: now) }
                        let all = section.alwaysOpen || loadedSections.contains(section.id)
                            || !section.items.contains(where: isActive)
                        let shown = section.shown(all: all, stays: isActive)
                        ForEach(shown) { item in chatRow(item, in: section, nested: true) }
                        if shown.count < section.items.count {
                            ChatSectionMore(section: section) {
                                withAnimation(.snappy) { _ = loadedSections.insert(section.id) }
                            }
                            .modifier(ChatListRow(nested: true))
                        }
                    }
                }
                // Under Starred while every chat is read, so the filter does not read as a list that lost its chats.
                if unreadsOnly && !sections.isEmpty && !tree.contains(where: { $0.kind == .unread }) {
                    Text("All caught up.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                        .accessibilityIdentifier("chats-unreads-empty")
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
        .overlay {
            if !words.isEmpty && searchedQuery == query && searchIsEmpty(words) {
                ContentUnavailableView.search(text: query).accessibilityIdentifier("search-empty")
            }
        }
        // On the list and not on the rows of the search: a modifier of a group of rows goes to each row.
        .task(id: query) {
            if await OmniSearch.messages(query, in: store, show: { messageHits = $0 }) { searchedQuery = query }
        }
        .navigationTitle(workingOnly ? "Working" : unreadsOnly ? "Unreads" : "Chats")
        .toolbar {
            // One tap reads every chat, as Read All in Telegram. It shows while a chat of the list has an unread
            // count.
            if sections.contains(where: { $0.items.contains { $0.unreadCount > 0 } }) {
                ToolbarItem(placement: .primaryAction) {
                    MarkAllReadButton().accessibilityIdentifier("chats-mark-all-read")
                }
            }
            ToolbarItem(placement: .primaryAction) { ChatsFilterMenu(working: $workingOnly, unreads: $unreadsOnly) }
            ToolbarItem(placement: .primaryAction) { ChatGridButton() }
        }
        .refreshable { await store.reconnect() }
        // The message box under the list gives the keyboard back when the list moves.
        .scrollDismissesKeyboard(.interactively)
        // A new chat shows in its section, and the list scrolls to it, or to the row of its section while it is folded.
        .modifier(NewChatReveal())
    }

    /// The chats of one section on a page of their own: Archived, newest first, which its row at the bottom of Chats
    /// opens (ChatSectionRow), while every other section opens in place. Each row is the row of a chat: a tap opens the
    /// chat, and a swipe to the left offers Unarchive, which takes the chat back to its section, and Mute. A message of
    /// the person brings a chat back too (agent_chats.py). The page keeps its title when its last chat leaves.
    private func sectionList(_ route: ChatSectionRoute, section: ChatListSection?) -> some View {
        List {
            if let section {
                ForEach(section.items) { item in chatRow(item, in: section) }
            } else {
                Text(route.isArchived ? "No archived chats." : "No chats.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .accessibilityIdentifier(route.isArchived ? "archived-empty" : "chat-section-empty")
                    .homeRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.s, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        .navigationTitle(route.title)
        .refreshable { await store.reconnect() }
    }

    /// What the row of a section shows of its chats: the chats whose card waits and the chats that work. Its unread
    /// chats are in Unread (ChatList.tree). Archived shows nothing: an archived chat runs nothing.
    private func summary(of section: ChatListSection, at date: Date) -> ChatSectionSummary {
        var summary = ChatSectionSummary()
        guard section.kind != .archived else { return summary }
        for item in section.items {
            // A card that waits comes first, as on the row of the chat (BotRowSubtitle).
            if case .bot(let bot) = item, store.botNeedsYou(bot) {
                summary.waiting += 1
            } else if works(item, at: date) {
                summary.working += 1
            }
        }
        return summary
    }

    /// One chat of a list. The row of a project names the project, so a further chat of the project shows its own
    /// title (ChatListSection.title). Starred and the Working list name the agent too (Bot.listName). A chat inside a
    /// section of Chats is `nested`: it sits set in under the row of its section (ChatListRow). A starred chat shows
    /// its star only outside Starred, whose row already says it, as a starred page of the Library does (Tastelint on
    /// #592).
    @ViewBuilder
    private func chatRow(_ item: ChatListItem, in section: ChatListSection?, nested: Bool = false) -> some View {
        let inStarred = section?.kind == .starred
        switch item {
        case .bot(let bot):
            BotContactRow(bot: bot, title: section?.title(of: bot) ?? bot.listName,
                          pinned: ChatList.isStarred(bot) && !inStarred)
                .padding(.vertical, BoardStyle.Space.m)
                .background(BoardStyle.paper)
                .modifier(ChatDeckRow(botId: bot.id) {
                    pushRoute?(ConversationRoute(botId: bot.id))
                })
                .accessibilityIdentifier(ChatList.isCoconut(bot) ? "coconut-chat" : "bot-conversation-\(bot.id)")
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    // A project stays, and so does the Coconut chat (Bot.canRemove). An archived chat offers
                    // Unarchive in the same place. Star or Unstar is here too, because the person looks for it in
                    // this menu, and a swipe to the right still gives it alone.
                    if bot.canRemove { archiveButton(bot) }
                    muteButton(bot)
                    if ChatList.canStar(bot) { starButton(bot) }
                }
                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                    if ChatList.canStar(bot) { starButton(bot) }
                }
                .modifier(ChatListRow(nested: nested))
        case .team, .direct, .botDirect:
            // A team chat has Star and Mute in its menu, and on its swipes as an agent chat has them: Star alone to the
            // right, and to the left Mute for a named chat, then Star. The Team screen has the leave action.
            if let chat = item.chat {
                Button {
                    pushRoute?(TeamChatRoute(chat: chat))
                } label: {
                    PeopleRow(item: item, pinMark: item.isPinnedChat && !inStarred)
                        .padding(.vertical, BoardStyle.Space.m)
                        .background(BoardStyle.paper)
                        .contentShape(Rectangle())
                        .modifier(TeamChatMuteMenu(item: item))
                }
                    .buttonStyle(HomePressStyle())
                    .accessibilityIdentifier(Self.identifier(chat))
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        if let mute = TeamChatMute.listAction(item) { teamMuteButton(mute) }
                        if let pin = TeamChatPin.listAction(item) { teamStarButton(pin) }
                    }
                    .swipeActions(edge: .leading, allowsFullSwipe: true) {
                        if let pin = TeamChatPin.listAction(item) { teamStarButton(pin) }
                    }
                    .modifier(ChatListRow(nested: nested))
            }
        }
    }

    /// The hits of a search of Chats (OmniSearch): the chats by their names, the to-dos, the pages and the cards
    /// that wait, then the messages that have the words, newest first. A group without hits does not show. A row
    /// opens its place on the stack of Chats, and a message opens its chat at the message.
    @ViewBuilder
    private func searchRows(_ words: [String]) -> some View {
        let chats = OmniSearch.chats(words, in: store)
        let todos = OmniSearch.todos(words, in: store)
        let pages = OmniSearch.pages(words, in: store)
        let cards = OmniSearch.cards(words, in: store)
        let first = [(chats.isEmpty, "Chats"), (todos.isEmpty, "To-dos"), (pages.isEmpty, "Pages"),
                     (cards.isEmpty, "Cards"), (messageHits.isEmpty, "Messages")].first { !$0.0 }?.1
        if !chats.isEmpty {
            searchHeader("Chats", first: first == "Chats")
            ForEach(chats) { item in chatRow(item, in: nil) }
        }
        if !todos.isEmpty {
            searchHeader("To-dos", first: first == "To-dos")
            let repository = store.todoIssues?.repository ?? "antiwork/gumroad-private"
            ForEach(todos) { todo in
                searchRow(symbol: todo.symbol, tint: todo.tint, title: todo.title,
                          detail: OmniSearch.detail(of: todo)) {
                    pushRoute?(todo.route(in: repository))
                }
                .accessibilityIdentifier("search-todo-\(todo.number)")
            }
        }
        if !pages.isEmpty {
            searchHeader("Pages", first: first == "Pages")
            ForEach(pages) { page in
                searchRow(symbol: page.symbol, tint: .secondary, title: page.title,
                          detail: OmniSearch.owner(page.botId, in: store) ?? "Library") {
                    pushRoute?(DocumentRoute(id: page.id))
                }
                .accessibilityIdentifier("search-page-\(page.id)")
            }
        }
        if !cards.isEmpty {
            searchHeader("Cards", first: first == "Cards")
            ForEach(cards) { card in
                searchRow(symbol: OmniSearch.cardSymbol, tint: BoardStyle.attention, title: card.title,
                          detail: OmniSearch.owner(card.botId, in: store) ?? "Waiting") {
                    pushRoute?(store.decisionChatRoute(for: card))
                }
                .accessibilityIdentifier("search-card-\(card.id)")
            }
        }
        if !messageHits.isEmpty {
            searchHeader("Messages", first: first == "Messages")
            let continued = OmniSearch.continued(messageHits)
            ForEach(messageHits) { hit in messageRow(hit, words: words, continued: continued.contains(hit.id)) }
        }
    }

    /// Nothing matches the words: no name, and no message on the phone or the Mac.
    private func searchIsEmpty(_ words: [String]) -> Bool {
        messageHits.isEmpty && OmniSearch.chats(words, in: store, limit: 1).isEmpty
            && OmniSearch.todos(words, in: store, limit: 1).isEmpty && OmniSearch.pages(words, in: store, limit: 1).isEmpty
            && OmniSearch.cards(words, in: store, limit: 1).isEmpty
    }

    /// The title of a group of hits, in the quiet color of a title. The first starts close under the field.
    private func searchHeader(_ title: String, first: Bool) -> some View {
        Text(title)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, first ? BoardStyle.Space.s : BoardStyle.Space.l)
            .padding(.bottom, BoardStyle.Space.s)
            .background(BoardStyle.paper)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("search-section-" + title.lowercased())
            .homeRow()
    }

    /// A to-do, a page or a card of the search, as a row of a to-do list (TodoIssueRows).
    private func searchRow(symbol: String, tint: Color, title: String, detail: String,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: symbol).font(.title3).foregroundStyle(tint).frame(width: todoIconWidth)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(title).font(.body).foregroundStyle(.primary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .homeRow()
    }

    /// A message of the search: the chat and the day, then the words around the match with the words in bold. A
    /// message in the chat of the row above shows only its words and its day. A tap opens the chat at the message.
    private func messageRow(_ hit: OmniSearch.MessageHit, words: [String], continued: Bool) -> some View {
        Button {
            pushRoute?(ConversationRoute(botId: hit.botId, messageId: hit.id))
        } label: {
            Group {
                if continued {
                    HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.l) {
                        snippet(of: hit, words: words)
                        if let date = hit.createdAt.apiDate {
                            CompactAgeLabel(date: date).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.bottom, BoardStyle.Space.m)
                } else {
                    ChatRowLayout(name: OmniSearch.owner(hit.botId, in: store) ?? "Chat", unread: 0,
                                  date: hit.createdAt.apiDate) {
                        EmptyView()
                    } subtitle: {
                        snippet(of: hit, words: words)
                    }
                    .padding(.vertical, BoardStyle.Space.m)
                }
            }
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityIdentifier("search-message-\(hit.id)")
        .homeRow()
    }

    /// The words of a message around the match, two lines at most.
    private func snippet(of hit: OmniSearch.MessageHit, words: [String]) -> some View {
        Text(OmniSearch.highlighted((hit.role == "user" ? "You: " : "") + hit.snippet, words))
            .font(.subheadline).foregroundStyle(.secondary)
            .lineLimit(2)
            // The grid of the row gives a cell one line unless the text asks for its height.
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A chat works while its row says so (AppStore.chatWorks), as in the sidebar of the Mac.
    private func works(_ item: ChatListItem, at date: Date) -> Bool { store.chatWorks(item, at: date) }

    /// A chat that an open section of Chats shows before Load more (the person, 2026-11-26: "load only active by
    /// default when expanded with load more"): a chat that works or whose card waits (AppStore.chatWorks), and the
    /// chat that a Send of the message box made last, as a folded section of the Mac sidebar keeps them in sight
    /// (MacSidebar.stays). Its unread chats are in Unread (ChatList.tree).
    private func active(_ item: ChatListItem, at date: Date) -> Bool {
        if case .bot(let bot) = item, bot.id == store.newChatSend?.chat { return true }
        return works(item, at: date)
    }

    /// What asks for the person comes first on Product: the rules that breach now, then the cards that wait. Each row
    /// draws nothing when there is nothing to show. The breaches of the business are not here: Support past SLA is a
    /// row of the list of Support, and the product issues past SLA and of high priority are the to-dos themselves.
    @ViewBuilder
    private var attention: some View {
        // The rules read the clock, so the card checks them again each minute.
        TimelineView(.periodic(from: .now, by: 60)) { timeline in
            let items = store.attentionItems(now: timeline.date).filter { $0.kind != .business }
            AttentionCard(items: items, open: open)
                .padding(.bottom, items.isEmpty ? 0 : BoardStyle.Space.m)
        }
        .homeRow(top: BoardStyle.Space.s)
        // Each card whole, as its chat shows it, in place of a count that opened them (the person, 2026-11-26: "1 card
        // waits for you → just render the whole card or cards inline there").
        ForEach(waitingCards) { waitingCard($0) }
    }

    /// The cards that wait, in the order of Cards (InboxPresentation.entries), and a card put off with Later at the end,
    /// as DecisionQueue puts it. A card stays while its answer waits for its Undo, as in its chat, and leaves once the
    /// answer is sent.
    private var waitingCards: [Decision] {
        let entries = InboxPresentation.entries(decisions: store.decisions)
        return entries.filter { $0.inboxDeferredAt == nil } + entries.filter { $0.inboxDeferredAt != nil }
    }

    /// A card that waits: the name of its chat, which opens the chat at the card, over the card with its options, its
    /// draft, its previews and its X (DecisionCard).
    private func waitingCard(_ card: Decision) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { pushRoute?(store.decisionChatRoute(for: card)) } label: {
                HStack(spacing: BoardStyle.Space.xs) {
                    Text(OmniSearch.owner(store.conversationBotId(for: card), in: store) ?? store.botName(card.botId))
                        .lineLimit(1)
                    Image(systemName: "chevron.right").imageScale(.small)
                }
                .font(.footnote).foregroundStyle(.secondary)
                .frame(minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(HomePressStyle())
            .accessibilityHint("Opens the chat of this card")
            .accessibilityIdentifier("home-card-chat-\(card.id)")
            DecisionCard(decision: card)
        }
        .padding(.bottom, BoardStyle.Space.m)
        .homeRow()
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

    /// Star or Unstar, beside Mute on a swipe to the left and alone on a swipe to the right, as Pin in Messages and
    /// Telegram. A starred chat moves to Starred at the top of the list and stays there while it is idle: the Mac does
    /// not archive it (AgentChats._may_archive).
    private func starButton(_ bot: Bot) -> some View {
        Button { Task { await store.pinBot(bot, pinned: !bot.pinned) } } label: {
            Label(bot.pinned ? TeamChatPin.unpin : TeamChatPin.pin, systemImage: bot.pinned ? "star.slash" : "star")
        }
        .tint(BoardStyle.husk)
        .accessibilityIdentifier((bot.pinned ? "unstar-bot-" : "star-bot-") + bot.id)
    }

    /// Star or Unstar for a chat of a team: the same choice as its menu (TeamChatMuteMenu).
    private func teamStarButton(_ pin: TeamChatPin.ListAction) -> some View {
        Button {
            store.error = nil
            Task { await store.setTeamChatPinned(pin.room, chat: pin.chat, direct: pin.direct, pinned: pin.pinned) }
        } label: {
            Label(pin.label, systemImage: pin.symbol)
        }
        .tint(BoardStyle.husk)
        .disabled(TeamChatPin.waits(TeamChatPin.key(room: pin.room, chat: pin.chat, direct: pin.direct),
                                    store.teamChatPinChoices))
        .accessibilityIdentifier("chat-row-pin-swipe")
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

/// A section of Chats, as a section of the sidebar of Slack: the chats that the person starred, the chats of one
/// project, the chats with people, the agents of an earlier release, or Archived (ChatList.sections). Chats on the
/// iPhone shows them as a tree: each section is one row that opens in place with its chats set in under it, Starred
/// and Unread stay open, and Archived opens a page (ChatSectionRow). The Mac sidebar folds them as Slack does
/// (MacSidebar, ChatSections). Both keep the sections that the person opened: one section on each line (open, toggle).
struct ChatListSection: Identifiable, Equatable {
    enum Kind: Equatable {
        /// The chats that the person starred, as the Starred section of Slack: the starred agent chats and the pinned
        /// chats of the teams. It comes first, and it stays open: the person chose these chats to have them at hand.
        case starred
        /// The chats with an unread message that the person did not star, under Starred in Chats on the iPhone
        /// (ChatList.unreadSection). It stays open too: the person asked for the unread chats at the top.
        case unread
        /// The chats of a project, its own chat first: the ID of the project.
        case project(String)
        /// The teams, their named chats, and the direct chats with a person or with a bot of a team.
        case people
        /// The agents of an earlier release, which are no project, with their chats, and the chats whose project is
        /// gone (AgentChats.orphans).
        case others
        /// The archived chats and agents, newest activity first. Chats has them on their own page behind the last row
        /// (ChatSectionRow), and the Mac sidebar in its folded Archived (ChatSections.archived).
        case archived
    }

    let kind: Kind
    let title: String
    let items: [ChatListItem]

    /// The section holds the chats that the person chose or has not read, so it shows them without a tap, as Starred
    /// and Unreads of Slack.
    var alwaysOpen: Bool { kind == .starred || kind == .unread }

    var id: String {
        switch kind {
        case .starred: "starred"
        case .unread: "unread"
        case .project(let id): "project-" + id
        case .people: "people"
        case .others: "others"
        case .archived: "archived"
        }
    }

    /// The rows of a section that keeps its active chats in sight: every chat when `all`, else the chats that `stays`
    /// keeps. A folded section of the Mac sidebar keeps them, and so does an open section of Chats on the iPhone until
    /// the person taps Load more (ChatSectionMore).
    func shown(all: Bool, stays: (ChatListItem) -> Bool) -> [ChatListItem] {
        all ? items : items.filter(stays)
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

/// What the row of a section of Chats shows of its chats (ChatSectionRow).
struct ChatSectionSummary: Equatable {
    /// The chats whose card waits for the person.
    var waiting = 0
    /// The chats that work, without the chats whose card waits (AppStore.chatWorks).
    var working = 0

    /// The line under the title: what waits for the person, then what works. Nil when nothing does.
    var waitingText: String? { waiting > 0 ? (waiting == 1 ? "1 needs you" : "\(waiting) need you") : nil }
    var workingText: String? { working > 0 ? "\(working) working" : nil }
}

/// A section of Chats as one row of a tree (the person, 2026-11-25: "Everything should be nested chat would help A
/// TON", then 2026-11-26: "Nested don't look nested"), in the form of a folder of Notes or Reminders: the symbol of the
/// section, its title and a chevron. The row does not count its chats (the person, 2026-11-26: "Don't need counts of
/// chats stressful"). Its unread chats are in Unread until the person reads them (ChatList.tree). A tap opens the
/// section in place: the chevron turns down, and its active chats show under the row, set in to its title
/// (ChatListRow), with Load more for the others (ChatSectionMore). A section with no active chat shows them all.
/// Another tap folds it. While it is folded, a line
/// under the title says what waits for the person and what works inside, in the words and colors of the row of a chat,
/// so a live chat in a folded section still shows from the list; Working at the top right lists every chat that works.
/// Starred and Unread have the row without the chevron, because they stay open. Archived is the last row, under
/// Personal, and its chevron opens the archived chats on a page of their own (the person: "Archived should be behind a
/// tap just one row with number at bottom of chats"). That chevron is gray and the chevron of a section has the color
/// of its symbol, because a folded section and Archived looked the same while only one leaves the list (Tastelint on
/// #589).
struct ChatSectionRow: View {
    /// The key of the sections that the person opened on the phone. Every section starts folded.
    static let key = "chats-open"
    /// The width of the symbol of a section at the default text size. A chat inside a section is set in by it.
    static let symbolSize: CGFloat = 28
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .title3) private var symbolWidth = ChatSectionRow.symbolSize
    let section: ChatListSection
    var summary = ChatSectionSummary()
    /// The section shows its chats under the row.
    var open = false
    /// Opens or folds the section, or opens the page of Archived.
    var tap: () -> Void = {}

    var body: some View {
        if section.alwaysOpen {
            label
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(section.title)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("chat-section-" + section.id)
        } else {
            Button(action: tap) { label }
                .buttonStyle(HomePressStyle())
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(spoken)
                .accessibilityValue(section.kind == .archived ? "" : open ? "Expanded" : "Collapsed")
                .accessibilityAddTraits(.isButton)
                .accessibilityIdentifier(section.kind == .archived ? "chats-archived" : "chat-section-" + section.id)
        }
    }

    private var label: some View {
        HStack(spacing: BoardStyle.Space.m) {
            Image(systemName: Self.symbol(section.kind))
                .font(.title3)
                .foregroundStyle(BoardStyle.control)
                .frame(width: symbolWidth)
                .overlay(alignment: .bottom) {
                    // The line of an open section starts under its symbol and goes on beside its chats (ChatListRow).
                    // The row cuts it at its bottom.
                    if open {
                        BoardStyle.line.frame(width: 1, height: 200).offset(y: 200 + BoardStyle.Space.xs)
                    }
                }
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(section.title)
                    .font(.body)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                // An open section shows its chats, so the line would say again what their rows say.
                if !open && (summary.waitingText != nil || summary.workingText != nil) { state }
            }
            Spacer(minLength: 0)
            if !section.alwaysOpen {
                // The arrow of a section has the color of its symbol, as a disclosure arrow of iOS, and turns where the
                // section opens. The gray arrow of Archived leads to a page, as in a list of settings.
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(section.kind == .archived ? AnyShapeStyle(.tertiary)
                                                               : AnyShapeStyle(BoardStyle.control))
                    .rotationEffect(.degrees(open ? 90 : 0))
                    .accessibilityHidden(true)
            }
        }
        .frame(minHeight: 44)
        .padding(.vertical, BoardStyle.Space.m)
        .background(BoardStyle.paper)
        .clipped()
        .contentShape(Rectangle())
    }

    /// What waits and what works, as the line under the name of a chat: a card in the color of attention, the work in
    /// the shimmer of a working step.
    private var state: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if let waiting = summary.waitingText {
                Text(waiting).foregroundStyle(BoardStyle.attention).lineLimit(1)
            }
            if summary.waitingText != nil && summary.workingText != nil {
                Text("·").foregroundStyle(.secondary)
            }
            if let working = summary.workingText {
                // The shimmer has one line, so the largest text sizes show the words without it.
                if dynamicTypeSize.isAccessibilitySize {
                    Text(working).foregroundStyle(.secondary).lineLimit(1)
                } else {
                    ShimmerText(text: working)
                }
            }
        }
        .font(.subheadline)
    }

    /// VoiceOver reads the title, and what waits and works while the section is folded. It reads no number, as the row
    /// shows none.
    private var spoken: String {
        var parts = [section.title]
        if !open { parts += [summary.waitingText, summary.workingText].compactMap { $0 } }
        return parts.joined(separator: ", ")
    }

    /// The symbol of a section: a folder for a project and for the agents of an earlier release, two people for the
    /// chats with people, a star for Starred, a message with a dot for Unread, and the box of Archive in Mail for
    /// Archived.
    static func symbol(_ kind: ChatListSection.Kind) -> String {
        switch kind {
        case .people: "person.2"
        case .archived: "archivebox"
        case .starred: "star"
        case .unread: "message.badge"
        case .project, .others: "folder"
        }
    }
}

/// The last row of an open section of Chats while it hides chats that are not active (the person, 2026-11-26: "load
/// only active by default when expanded with load more"). A tap shows all the chats of the section. It names no
/// number, as the row of the section names none. The section shows only its active chats again after it folds.
struct ChatSectionMore: View {
    let section: ChatListSection
    let tap: () -> Void

    var body: some View {
        Button(action: tap) {
            Text("Load more")
                .font(.subheadline)
                .foregroundStyle(BoardStyle.control)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityHint("Shows all the chats of \(section.title)")
        .accessibilityIdentifier("chat-section-more-" + section.id)
    }
}

/// A Send that starts a chat leaves the chat closed (sendNewChat): the Send of the message box, and Send to a new agent
/// in a chat, which keeps the person in that chat. The new chat is in its section, whose row shows it working, and an
/// open section shows it before Load more (HomeLaunchView.active). The list scrolls to the chat, or to the row of its
/// section while the section is folded.
private struct NewChatReveal: ViewModifier {
    @Environment(AppStore.self) private var store
    @AppStorage(ChatSectionRow.key) private var openSections = ""

    func body(content: Content) -> some View {
        ScrollViewReader { proxy in
            content.onChange(of: store.newChatSend) { _, send in
                guard let send, let bot = store.bots.first(where: { $0.id == send.chat }) else { return }
                let item = ChatListItem.bot(bot).id
                let store = store
                let opened = ChatListSection.open(openSections)
                Task {
                    // The chat is in its section after the update that its message makes.
                    try? await Task.sleep(for: .milliseconds(150))
                    let sections = ChatList.sections(bots: store.bots, latest: store.chatSummaries.newestActivity,
                                                      rooms: store.shownTeamRooms, local: store.newestLocalMessages)
                    let tree = ChatList.tree(sections, latest: store.chatSummaries.newestActivity)
                    guard let section = tree.first(where: { $0.items.contains { $0.id == item } }) else { return }
                    // A chat shows under the row of its section while the section is open, and Starred and Unread
                    // stay open.
                    let shown = section.alwaysOpen || opened.contains(section.id)
                    withAnimation { proxy.scrollTo(shown ? item : section.id) }
                }
            }
        }
    }
}

/// A row of a chat in the Chats list. Inside a section it is set in to the title of the section, and a line runs down
/// from the symbol of the section beside its chats, so that they show as the branches of the section (the person,
/// 2026-11-26: "Nested don't look nested"). The Working list has its chats at the side space of the page.
private struct ChatListRow: ViewModifier {
    let nested: Bool
    @ScaledMetric(relativeTo: .title3) private var symbolWidth = ChatSectionRow.symbolSize

    @ViewBuilder
    func body(content: Content) -> some View {
        if nested {
            content
                .listRowInsets(EdgeInsets(top: 0, leading: BoardStyle.Space.l + symbolWidth + BoardStyle.Space.m,
                                          bottom: 0, trailing: BoardStyle.Space.l))
                .listRowSeparator(.hidden)
                .listRowBackground(BoardStyle.paper.overlay(alignment: .leading) {
                    BoardStyle.line.frame(width: 1).padding(.leading, BoardStyle.Space.l + (symbolWidth - 1) / 2)
                })
        } else {
            content.homeRow()
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
    /// The star mark of a starred chat where no Starred row names it: in Working and in search.
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
