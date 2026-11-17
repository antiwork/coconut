import CoconutKit
import SwiftUI

/// What the main window shows in its detail column.
enum MacSelection: Hashable {
    /// Home: what needs attention, what is unread, and what is in progress.
    case home
    /// The cards that wait for an answer. A card row of Home opens them.
    case inbox
    /// The agents as projects. The page of an agent and its pages open on the stack of this place.
    case agents
    /// The teams, their people and their notice boards.
    case people
    /// The daily, monthly and yearly figures of the business.
    case reports
    /// The person and this device.
    case profile
    case bot(String)
    /// A To-do of an agent: its chat, with the work item attached.
    case work(String)
    /// A page of the Library, by its document ID.
    case document(String)
    /// The Team chat of a team, a named chat, or a direct chat with one person or one bot of a team.
    case team(TeamChatKey)
}

/// The tabs of the details next to a chat: the settings of the agent and its pages.
enum InspectorTab: Hashable {
    case details, pages
}

/// The main window of the Mac client: the bots in a sidebar, the chat or the Inbox in the middle, and the details of the
/// bot in a column on the right. It shows the pairing page until this Mac has a pairing. A Mac that runs Coconut for a
/// team only shows its own page instead, and never the pairing page.
struct MacMainWindow: View {
    @Environment(AppStore.self) private var store
    @ObservedObject var model: AppModel
    @State private var selection: MacSelection? = .home
    // The card that the Inbox shows first: the card of the row of Home that opened it.
    @State private var inboxCard: String?
    @State private var inspectorShown = true
    @State private var inspectorTab = InspectorTab.details
    @State private var creatingAgent = false
    @State private var pairingOpen = false
    // The pages that a chat opened. A row of the chat list pushes onto it (pushRoute); another bot starts at its chat.
    @State private var chatPath = NavigationPath()
    // The pages to push when the next choice shows: the page of an agent, or a page of an agent over it.
    @State private var pendingRoutes: [any Hashable] = []
    // The right edge of the sidebar in the window. The notice centers right of it while the sidebar shows.
    @State private var sidebarRight: CGFloat = 0
    @State private var sidebarShown = true

    var body: some View {
        if model.mode == .team {
            TeamOnlyNote()
        } else {
            clientWindow
        }
    }

    private var clientWindow: some View {
        Group {
            if store.hasPairing || store.isDemo {
                NavigationSplitView {
                    MacSidebar(selection: $selection)
                        .onGeometryChange(for: CGFloat.self) { $0.frame(in: .global).maxX } action: { sidebarRight = max(0, $0) }
                        // AppKit hides a collapsed sidebar but keeps its frame, also when the window opens with the
                        // sidebar that the person hid. So the right edge counts only while AppKit shows the sidebar.
                        .background(OnScreenProbe { sidebarShown = $0 })
                        .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 300)
                        .toolbar {
                            ToolbarItem {
                                Button(action: createAgent) { Label("New agent", systemImage: "plus") }
                                    .disabled(creatingAgent)
                                    .keyboardShortcut("n", modifiers: .command)
                                    .help("Create an agent")
                            }
                        }
                } detail: {
                    detail
                }
                // Each page of the detail column gets the notice from here, also a page that a stack pushes: a push
                // replaces the whole content of the column, with each modifier inside the detail closure.
                .modifier(MacNoticeArea(sidebarEdge: sidebarShown ? sidebarRight : 0))
            } else {
                MacPairingView(model: model)
            }
        }
        // The columns at their widest sidebar (300) fit side by side: the Inbox list (220) and its card (300), and a
        // chat (300) and its details (320). When they did not fit, the sidebar moved out to the left and the right side
        // was cut off.
        .environment(\.openChat) { id in selection = .bot(id) }
        .environment(\.showTeamChat) { chat in selection = .team(chat) }
        // The Team screen of the Settings window asks for a direct chat. The window shows it.
        .onChange(of: model.shownChat, initial: true) { _, chat in
            guard let chat else { return }
            selection = .team(chat)
            model.shownChat = nil
        }
        // A share from the share menu went to an agent. The window shows its chat.
        .onChange(of: model.shownBot, initial: true) { _, bot in
            guard let bot else { return }
            selection = .bot(bot)
            model.shownBot = nil
        }
        // A document card in a chat selects the pinned row of its page, else it opens the page over its agent.
        .environment(\.openDocument) { id in show(MacPagePlace.card(id, documents: store.documents)) }
        // The page of a pinned row stays open when its pin goes: it shows over its agent. A deleted page leaves the
        // list of the agents.
        .onChange(of: store.documents) { _, documents in
            guard case .document(let id) = selection,
                  let place = MacPagePlace.afterChange(open: id, documents: documents) else { return }
            show(place)
        }
        .frame(minWidth: 920, minHeight: 480)
        .background(BoardStyle.paper)
        .announcesErrors(store.error)
        // A sheet of the window shows the error itself. While it is open, the notice of the window shows none.
        .sheet(isPresented: $pairingOpen) { MacPairingView(model: model).frame(width: 520, height: 600).sheetShowsError() }
        .onChange(of: store.setupLink) { _, link in if link != nil { pairingOpen = true } }
        // The sheet waits for each pairing page: the sheet of a link, and the page of a window without a pairing.
        .sheet(item: Binding(get: {
            TeamJoinGate.canShow(store.teamJoin, showing: nil, pairingOpen: pairingOpen || PairingPages.shared.isOpen)
                ? store.teamJoin : nil
        }, set: { store.teamJoin = $0 })) {
            TeamJoinView(link: $0.link).sheetShowsError()
        }
        // Each choice in the sidebar starts at its own root, so another bot never shows the last bot's pages.
        .onChange(of: selection) { old, selected in
            // The menu of an open pinned page deletes it and then dismisses it, which clears the selection. The
            // agents show, not Home.
            if selected == nil, case .document(let id)? = old,
               MacPagePlace.afterChange(open: id, documents: store.documents) == .libraryList {
                show(.libraryList)
                return
            }
            // A place starts at its root, or at the pages that the choice asked for (go).
            var path = NavigationPath()
            for route in pendingRoutes { path.append(route) }
            pendingRoutes = []
            chatPath = path
        }
        // Outside each sheet of the window, so that each sheet and each page gets it.
        .errorSheetRoot()
    }

    private func show(_ place: MacPagePlace) {
        switch place {
        case .pinnedRow(let id): selection = .document(id)
        case .inLibrary(let id): go(.agents, pushing: store.documentPath(id))
        case .libraryList: go(.agents)
        }
    }

    /// Shows a place with pages on its stack. The change of the choice clears the stack, so the pages wait for it.
    private func go(_ place: MacSelection, pushing routes: [any Hashable] = []) {
        if selection == place {
            var path = NavigationPath()
            for route in routes { path.append(route) }
            chatPath = path
        } else {
            pendingRoutes = routes
            selection = place
        }
    }

    /// The stack of a place that is no chat: Home, the agents, People or Profile, with each page that they open.
    private func placeStack(@ViewBuilder _ root: () -> some View) -> some View {
        NavigationStack(path: $chatPath) {
            root()
                .modifier(AgentDestinations())
                .modifier(ProfileDestinations())
                .navigationDestination(for: ConversationRoute.self) { route in
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                     focusMessageId: route.messageId, attachedWork: route.workContext)
                }
                .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
                .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
                .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
                .navigationDestination(for: ArchivedAgentsRoute.self) { _ in ArchivedAgentsView() }
                .navigationDestination(for: NoticeBoardRoute.self) { NoticeBoardView(roomId: $0.roomId) }
                .navigationDestination(for: TeamScreenRoute.self) { TeamRoomView(roomId: $0.roomId, pushed: true) }
        }
        .environment(\.pushRoute) { route in chatPath.append(route) }
        // A grouped Mac form keeps each section in a box, as the iPhone settings show them.
        .formStyle(.grouped)
    }

    /// Opens the place of a row of Home. Each place is a choice of the window, so the sidebar shows where it is.
    private func open(_ target: HomeTarget) {
        switch target {
        case .conversation(let route):
            // A To-do opens the chat with the work attached.
            if let work = route.workContext, store.artifacts.contains(where: { $0.id == work.id }) {
                selection = .work(work.id)
            } else {
                selection = .bot(route.botId)
            }
        case .teamChat(let chat): selection = .team(chat)
        case .cards(let decisionId):
            // A card opens in the chat of its agent. Only "Review cards" opens the list of cards.
            if let decisionId, let decision = store.decisions.first(where: { $0.id == decisionId }) {
                selection = .bot(store.decisionChatRoute(for: decision).botId)
            } else {
                inboxCard = decisionId
                selection = .inbox
            }
        case .agent(let botId): go(.agents, pushing: [AgentRoute(botId: botId)])
        case .agents: go(.agents)
        case .connection: go(.profile, pushing: [ProfileRoute.mac])
        case .notices(let room): go(.people, pushing: [NoticeBoardRoute(roomId: room)])
        }
    }

    /// Makes an agent with a random letter and selects its chat (AppStore.newAgent).
    private func createAgent() {
        creatingAgent = true
        Task {
            defer { creatingAgent = false }
            if let bot = await store.newAgent() { selection = .bot(bot.id) }
        }
    }

    // A new case, with or without a stack of its own, needs no code for the notice of the window (MacNoticeArea).
    @ViewBuilder private var detail: some View {
        switch selection {
        case .home, nil:
            placeStack { HomeView { open($0) }.navigationTitle("Home") }
        case .inbox:
            MacInboxView(selection: $selection, first: inboxCard)
        case .agents:
            placeStack { AgentsView(create: createAgent).navigationTitle("Agents") }
        case .people:
            placeStack { PeopleView(openTeamChat: { selection = .team($0) }).navigationTitle("People") }
        case .reports:
            placeStack { BusinessReportsView().navigationTitle("Reports") }
        case .profile:
            placeStack { ProfileView().navigationTitle("Profile") }
        case .document(let documentId):
            // A page has no links to other pages, so its stack has only the page.
            NavigationStack { DocumentView(documentId: documentId).id(documentId) }
        case .work(let workId):
            if let item = store.artifacts.first(where: { $0.id == workId }) {
                NavigationStack(path: $chatPath) {
                    ConversationView(botId: item.botId, attachedWork: WorkReference(id: item.id, title: item.title))
                        .id(workId)
                        .navigationDestination(for: ConversationRoute.self) { route in
                            ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                             focusMessageId: route.messageId, attachedWork: route.workContext)
                        }
                        .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
                }
                .environment(\.pushRoute) { route in chatPath.append(route) }
                .ignoresSafeArea(.container, edges: .top)
                .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
                .toolbar(removing: .title)
            } else {
                placeStack { HomeView { open($0) }.navigationTitle("Home") }
            }
        case .bot(let botId):
            // The details are a column of fixed width next to the chat, not an inspector. Next to a chat, the
            // inspector's split view did not settle on a layout and stopped the app, below about 1,300 points of
            // window width. The column also keeps its tabs in one row with the bot's title over the chat.
            HStack(spacing: 0) {
                NavigationStack(path: $chatPath) {
                    ConversationView(botId: botId)
                        .id(botId)
                        // The bot's title over the chat shows and hides the details next to it.
                        .environment(\.toggleBotDetails) { withAnimation(.snappy) { inspectorShown.toggle() } }
                        .navigationDestination(for: ConversationRoute.self) { route in
                            ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                             focusMessageId: route.messageId, attachedWork: route.workContext)
                        }
                        .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                        .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
                }
                .environment(\.pushRoute) { route in chatPath.append(route) }
                .frame(minWidth: 300, maxWidth: .infinity)
                if inspectorShown {
                    Divider()
                    // The settings and the pages are those of the agent, also next to one of its further chats.
                    MacBotDetails(botId: AgentChats.agentId(of: botId, in: store.bots), tab: $inspectorTab)
                        .frame(width: 320)
                        .transition(.move(edge: .trailing))
                }
            }
            // The chat and the details run up into the toolbar, so the bot's title and the tabs sit in its row, next to the
            // window buttons. The toolbar draws no band of its own there.
            .ignoresSafeArea(.container, edges: .top)
            .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
            // The pill over the chat names the bot, so the window shows no title of its own. Without this, the title of
            // the bot settings in the details repeats the bot name.
            .toolbar(removing: .title)
        case .team(let chat):
            // A team has no details column, so the chat takes the whole detail column and has no control for one.
            NavigationStack(path: $chatPath) {
                TeamChatView(chatKey: chat)
                    .id(chat)
            }
            .ignoresSafeArea(.container, edges: .top)
            .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
            .toolbar(removing: .title)
        }
    }
}

/// The notice of an action that failed, at the bottom center of the detail column. It takes its own space at the
/// bottom of the column, so the page above it gets shorter and covers nothing. A chat on the screen shows the notice
/// above its composer instead (NoticeHolders), and the column shows none.
///
/// The area sits on the split view, above each stack of the column, so no pushed page covers it. A new case of the
/// detail column and a new pushed page need no code for the notice. A new chat with a composer puts ChatWindowNotice
/// above its composer, as ConversationView does.
struct MacNoticeArea: ViewModifier {
    /// The right edge of the sidebar in the window, or 0 when the sidebar is hidden.
    let sidebarEdge: CGFloat
    @State private var notice = WindowNotice()
    @State private var area: CGFloat?
    @State private var band: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .bottom) {
                Group {
                    if notice.holders.columnShowsNotice {
                        WindowErrorNotice().padding(.leading, sidebarEdge)
                    }
                }
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { band = $0 }
                .background(DetailColumnBottomInset(height: band))
            }
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { area = $0 }
            .environment(\.noticeArea, area)
            // The notice of the window shows each error, so the pages of the detail column show no inline copy.
            .environment(\.windowShowsError, true)
            .environment(\.windowNotice, notice)
    }
}

/// Gives the detail column of the split view in this window a bottom safe area as high as the notice, so each page of
/// the column gets shorter, also a page that a stack pushes. AppKit hosts the column in a view of its own. A safe area
/// inset of SwiftUI on the split view does not reach that view, but the safe area of AppKit does: AppKit gives the same
/// view its left inset next to the sidebar.
struct DetailColumnBottomInset: NSViewRepresentable {
    let height: CGFloat

    func makeNSView(context: Context) -> InsetView { InsetView() }

    func updateNSView(_ view: InsetView, context: Context) {
        view.height = height
        view.apply()
    }

    final class InsetView: NSView {
        var height: CGFloat = 0

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            apply()
        }

        // The view only finds the column. It takes no click and no wheel event.
        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        func apply() {
            guard let column = Self.detailColumn(in: window), column.additionalSafeAreaInsets.bottom != height else { return }
            column.additionalSafeAreaInsets.bottom = height
        }

        /// The view that hosts the detail column: the view that fills the last item of the first split view of the
        /// window. Nil when the window has no split view.
        static func detailColumn(in window: NSWindow?) -> NSView? {
            var queue = window?.contentView.map { [$0] } ?? []
            while !queue.isEmpty {
                let view = queue.removeFirst()
                if let split = view as? NSSplitView, split.arrangedSubviews.count > 1, let item = split.arrangedSubviews.last {
                    return item.subviews.first { $0.frame == item.bounds } ?? item
                }
                queue += view.subviews
            }
            return nil
        }
    }
}

/// The sidebar: the Coconut chat, then Home, Agents, People and Profile, then the chats: the pinned bots and the pinned pages, and the chats: every other bot and each team, with the
/// last message.
struct MacSidebar: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?
    @State private var deleting: Bot?
    @AppStorage("sidebar-unreads-open") private var unreadsOpen = true
    @AppStorage("sidebar-starred-open") private var starredOpen = true
    @AppStorage("sidebar-people-open") private var peopleOpen = true
    @AppStorage("sidebar-agents-open") private var agentsOpen = true
    @State private var archivedOpen = false

    /// The bots that are not archived, newest activity first, split into the pinned bots and the other bots.
    /// It reads only its arguments, so it needs no main actor, and a test can call it from any context.
    var body: some View {
        // The store keeps the newest activity of each bot (ChatSummaries), so the sidebar reads no message. A pass
        // over the messages for each body cost about 3 ms with 1,500 messages.
        // An agent keeps its place by the newest of its chats, which show under it.
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let sections = ChatSections.make(bots: store.bots.agents, latest: latest,
                                         rooms: store.shownTeamRooms, local: store.newestLocalMessages)
        let pins = SidebarPins(pinnedBots: sections.pinned, documents: store.documents ?? [], bots: store.bots,
                               teamBots: store.teamBots)
        // The cards are a page of Home, so Home stays marked while they show.
        let marked = Binding<MacSelection?>(get: { selection == .inbox ? .home : selection }, set: { selection = $0 })
        return List(selection: marked) {
            // The Coconut chat: where the person changes Coconut. Coconut has no settings window for it.
            if let coconut = store.bots.coconutChat {
                CoconutChatRow(bot: coconut).tag(MacSelection.bot(coconut.id))
                    .contextMenu { NewChatButton(bot: coconut) { selection = .bot($0.id) } }
                chatRows(of: coconut)
            }
            MacSidebarLabel(title: "Home", symbol: "house")
                .badge(HomeFeed.badge(store.homeUnread))
                .tag(MacSelection.home)
                .accessibilityIdentifier("sidebar-home")
            MacSidebarLabel(title: "Agents", symbol: "square.stack.3d.up")
                .tag(MacSelection.agents)
                .accessibilityIdentifier("sidebar-agents")
            MacSidebarLabel(title: "Reports", symbol: "chart.bar.doc.horizontal")
                .tag(MacSelection.reports)
                .accessibilityIdentifier("sidebar-reports")
            MacSidebarLabel(title: "People", symbol: "person.2")
                .badge(store.shownTeamRooms.reduce(0) { $0 + $1.unseenNotices })
                .tag(MacSelection.people)
                .accessibilityIdentifier("sidebar-people")
            MacSidebarLabel(title: "Profile", symbol: "person.crop.circle")
                .tag(MacSelection.profile)
                .accessibilityIdentifier("sidebar-profile")
            // The chats, in the sections of Slack: each one folds with its header, and the Mac keeps the choice. A
            // section without rows does not show. Starred comes first.
            if !pins.isEmpty || !sections.pinnedChats.isEmpty {
                Section("Starred", isExpanded: $starredOpen) {
                    ForEach(pins.bots) { bot in row(bot) }
                    // The pinned chats of the teams, as the same rows that they have in "Team chats".
                    ForEach(sections.pinnedChats) { item in itemRow(item) }
                    ForEach(pins.pages) { page in pageRow(page) }
                }
            }
            if !sections.unread.isEmpty {
                Section("Unreads", isExpanded: $unreadsOpen) {
                    ForEach(sections.unread) { item in itemRow(item) }
                }
            }
            if !sections.people.isEmpty {
                Section("Team chats", isExpanded: $peopleOpen) {
                    ForEach(sections.people) { item in itemRow(item) }
                }
            }
            if !sections.bots.isEmpty {
                Section("Agent chats", isExpanded: $agentsOpen) {
                    ForEach(sections.bots) { item in itemRow(item) }
                }
            }
            // The archived agents, closed until the person opens the section. An archived chat stays readable.
            let archived = store.bots.filter { $0.archived && !$0.coconut }
                .sorted { $0.listName.localizedStandardCompare($1.listName) == .orderedAscending }
            if !archived.isEmpty {
                Section("Archived", isExpanded: $archivedOpen) {
                    ForEach(archived) { bot in row(bot) }
                }
            }
        }
        // Slack's rows: one line each, close together.
        .environment(\.defaultMinListRowHeight, 26)
        .listStyle(.sidebar)
        .modifier(DeleteBotDialog(bot: $deleting))
        // A deleted bot leaves the list. Its chat closes, and Home shows instead.
        .onChange(of: store.bots.map(\.id)) { _, ids in
            if case .bot(let id) = selection, !ids.contains(id) { selection = .home }
        }
        // A team that this Mac removed leaves the list too, and so does a named chat that the admin deleted.
        .onChange(of: store.team) { _, team in
            if case .team(let chat) = selection, let team, !team.has(chat) { selection = .home }
        }
        // The pinned pages need the list without a visit to the Library: one quiet load when the Mac connects and no
        // list is there. After that, the updates load the list again on each change of a page and on a reset.
        .task(id: store.connection == .connected) {
            if store.documents == nil { await store.loadDocuments(quiet: true) }
        }
        .safeAreaInset(edge: .bottom) { MacConnectionFooter() }
    }

    /// A pinned page. Its menu only unpins: the page of its agent and the page itself have the other actions.
    private func pageRow(_ page: BotDocument) -> some View {
        MacPinnedPageRow(page: page)
            .tag(MacSelection.document(page.id))
            .contextMenu {
                Button("Unstar") { Task { await store.pinDocument(page, pinned: false) } }
            }
            .accessibilityIdentifier("sidebar-page-\(page.key)")
    }

    /// The row of an agent, and under it the rows of its further chats. An archived row stands alone.
    @ViewBuilder
    private func row(_ bot: Bot) -> some View {
        botRow(bot)
        if !bot.archived && !bot.isChat { chatRows(of: bot) }
    }

    /// The further chats of an agent, newest first: the title of each chat, set in under the name of the agent.
    private func chatRows(of agent: Bot) -> some View {
        ForEach(AgentChats.chats(of: agent.id, in: store.bots, latest: store.chatSummaries.newestActivity)) { chat in
            botRow(chat)
        }
    }

    private func botRow(_ bot: Bot) -> some View {
        MacSidebarRow(name: bot.archived ? bot.listName : bot.isChat ? bot.chatLabel : bot.name, unread: bot.unreadCount) {
            // A further chat has no face of its own: the face of its agent is in the row above it.
            if bot.isChat && !bot.archived {
                Image(systemName: "bubble.left").font(.caption).foregroundStyle(.tertiary)
            } else {
                LiveBotAvatar(bot: bot, size: 20)
            }
        }
            .padding(.leading, bot.isChat && !bot.archived ? BoardStyle.Space.m : 0)
            .tag(MacSelection.bot(bot.id))
            .contextMenu {
                if !bot.archived {
                    NewChatButton(bot: bot) { selection = .bot($0.id) }
                    Divider()
                    if !bot.isChat {
                        Button(bot.pinned ? "Unstar" : "Star") { Task { await store.pinBot(bot, pinned: !bot.pinned) } }
                    }
                }
                Button(bot.archived ? "Unarchive" : "Archive") { Task { await store.archiveBot(bot, archived: !bot.archived) } }
                Divider()
                Button(bot.isChat ? "Delete Chat…" : "Delete Agent…", role: .destructive) { deleting = bot }
            }
            // A swipe to the left on the trackpad archives the agent, as in the Chats list of the iPhone.
            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                Button { Task { await store.archiveBot(bot, archived: !bot.archived) } } label: {
                    Label(bot.archived ? "Unarchive" : "Archive", systemImage: bot.archived ? "tray.and.arrow.up" : "archivebox")
                }
                .tint(bot.archived ? .blue : .gray)
            }
            .accessibilityIdentifier("sidebar-bot-\(bot.id)")
    }

    /// The row of a chat in any section: an agent, a team, a direct chat with a person, or one with a bot of a team.
    @ViewBuilder
    private func itemRow(_ item: ChatListItem) -> some View {
        switch item {
        case .bot(let bot): row(bot)
        case .team(let room):
            if let chat = item.chat {
                MacSidebarRow(name: room.title, unread: item.unreadCount, muted: room.shownChatMuted,
                              mentioned: room.hasChat && room.showsMentionMark) {
                    TeamFace(room: room, size: 20)
                }
                .tag(MacSelection.team(chat))
                // The Team screen has the leave action.
                .modifier(TeamChatMuteMenu(item: item))
                .accessibilityLabel(TeamRowSpeech.label(room, preview: nil))
                .accessibilityIdentifier("sidebar-team-" + chat.room + (chat.chat.map { "-" + $0 } ?? ""))
            }
        case .direct(let room, let direct):
            let peer = room.directPeer(direct.member)
            MacSidebarRow(name: peer.name, unread: item.unreadCount) { DirectPeerFace(peer: peer, size: 20) }
                .tag(MacSelection.team(TeamChatKey(room: room.room, member: direct.member)))
                .modifier(TeamChatMuteMenu(item: item))
                .accessibilityIdentifier("sidebar-direct-" + direct.member)
        case .botDirect(let room, let direct):
            let peer = room.botPeer(direct.bot)
            MacSidebarRow(name: peer.name, unread: item.unreadCount) { DirectPeerFace(peer: peer, size: 20) }
                .tag(MacSelection.team(TeamChatKey(room: room.room, bot: direct.bot)))
                .accessibilityIdentifier("sidebar-bot-direct-" + direct.bot)
        }
    }
}

/// One row of the sidebar, as in Slack: a small face, the name in one line, and the unread count at the end. An
/// unread chat shows its name in bold and in the text color; a read chat shows it quieter. A muted chat stays quiet.
private struct MacSidebarRow<Face: View>: View {
    let name: String
    let unread: Int
    var muted = false
    var mentioned = false
    @ViewBuilder let face: () -> Face

    var body: some View {
        let loud = unread > 0 && !muted
        HStack(spacing: BoardStyle.Space.s) {
            face().frame(width: 20, height: 20)
            Text(name)
                .font(.body.weight(loud ? .semibold : .regular))
                .foregroundStyle(loud ? .primary : .secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
            if muted { MutedMark() }
            if mentioned { MentionBadge() }
            if loud { UnreadBadge(count: unread) }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
        .accessibilityLabel([name, loud ? UnreadBadge.spoken(unread) : nil].compactMap { $0 }.joined(separator: ", "))
    }
}

/// A pinned page in the sidebar: the symbol of its kind in the place of a face, its title, and the name of its bot.
/// It has no time and no unread mark, so it does not read as a chat. The face column has the width of a bot face, so
/// the edges and the height match the bot rows.
private struct MacPinnedPageRow: View {
    @Environment(AppStore.self) private var store
    let page: BotDocument

    private var owner: String {
        store.bots.first { $0.id == page.botId }?.name ?? store.teamBots.first { $0.id == page.botId }?.name ?? ""
    }

    var body: some View {
        // One line, as the other rows: the symbol of its kind in the place of a face, and its title.
        HStack(spacing: BoardStyle.Space.s) {
            Image(systemName: page.symbol).font(.callout).foregroundStyle(.secondary).frame(width: 20, height: 20)
            Text(page.title).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
        .help(owner)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(page.title), page, \(owner)")
        .accessibilityHint("Opens the page")
    }
}

/// The paired Mac and its connection, at the foot of the sidebar.
private struct MacConnectionFooter: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "desktopcomputer")
            Text(store.hasPairing ? store.pairedMacDescription : "No Mac paired").lineLimit(1)
            Spacer(minLength: 0)
            Circle().fill(store.connection == .connected ? BoardStyle.positive : Color.secondary)
                .frame(width: 7, height: 7)
                .accessibilityLabel(store.connection == .connected ? "Connected" : "Not connected")
        }
        .font(.footnote).foregroundStyle(.secondary)
        // The inset of the rows of the sidebar, so the footer shares their left and right edges.
        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.s)
    }
}

/// The details of an agent next to its chat: its settings or its pages.
struct MacBotDetails: View {
    let botId: String
    @Binding var tab: InspectorTab

    var body: some View {
        VStack(spacing: 0) {
            // Text tabs. The current one sits on a soft capsule. The same height and top space as the bot's title over
            // the chat, so both sit in one row.
            HStack(spacing: BoardStyle.Space.xs) {
                tabButton("Details", .details)
                tabButton("Pages", .pages)
            }
            .frame(height: 34)
            .padding(.horizontal, 12).padding(.top, 8)
            // The background of the form below reaches up into the toolbar area. The tabs stay in front of it.
            .zIndex(1)
            NavigationStack {
                // Each bot gets its own settings view. Leaving it saves its edits to that bot, so an unsaved edit
                // never moves to the next bot.
                switch tab {
                case .details: BotSettingsView(botId: botId).id(botId)
                case .pages:
                    LibraryView(botId: botId)
                        .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
                    .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
                }
            }
        }
        // A grouped Mac form keeps each section in a box, as the iPhone settings show them.
        .formStyle(.grouped)
        .background(BoardStyle.paper)
    }

    private func tabButton(_ title: String, _ value: InspectorTab) -> some View {
        ChipButton(title: title, selected: tab == value) { tab = value }
    }
}

/// A row of the sidebar's top part: a quiet icon in the text color, and room around it.
private struct MacSidebarLabel: View {
    let title: String
    let symbol: String

    var body: some View {
        Label {
            Text(title)
        } icon: {
            Image(systemName: symbol).foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

/// What a window shows on a Mac that runs Coconut for a team only (InstallMode.team), when the system opens one, for
/// example by state restoration. Nobody sits at this Mac: its reader works in a terminal.
struct TeamOnlyNote: View {
    var body: some View {
        Text("This Mac runs Coconut for a team only. Run coconut status in Terminal.")
            .padding(24)
            .frame(minWidth: 480, minHeight: 120)
    }
}
