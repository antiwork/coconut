import CoconutKit
import SwiftUI

/// What the main window shows in its detail column.
enum MacSelection: Hashable {
    case inbox
    case library
    case bot(String)
    /// A To-do of an agent: its chat, with the work item attached.
    case work(String)
    /// A page of the Library, by its document ID.
    case document(String)
    /// The Team chat of a team, a named chat, or a direct chat with one person or one bot of a team.
    case team(TeamChatKey)
}

/// The tabs of the details next to a chat.
enum InspectorTab: Hashable {
    case details, library
}

/// The main window of the Mac client: the bots in a sidebar, the chat or the Inbox in the middle, and the details of the
/// bot in a column on the right. It shows the pairing page until this Mac has a pairing. A Mac that runs Coconut for a
/// team only shows its own page instead, and never the pairing page.
struct MacMainWindow: View {
    @Environment(AppStore.self) private var store
    @ObservedObject var model: AppModel
    @State private var selection: MacSelection? = .inbox
    @State private var inspectorShown = true
    @State private var inspectorTab = InspectorTab.details
    @State private var createOpen = false
    @State private var pairingOpen = false
    // The pages that a chat opened. A row of the chat list pushes onto it (pushRoute); another bot starts at its chat.
    @State private var chatPath = NavigationPath()
    // The pages of the Library. A document card in a chat opens its document here.
    @State private var libraryPath = NavigationPath()
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
                                Button { createOpen = true } label: { Label("New agent", systemImage: "plus") }
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
        // A document card in a chat selects the pinned row of its page, else it opens the page inside the Library.
        .environment(\.openDocument) { id in show(MacPagePlace.card(id, documents: store.documents)) }
        // The page of a pinned row stays open when its pin goes: the Library shows it. A deleted page leaves the
        // Library list.
        .onChange(of: store.documents) { _, documents in
            guard case .document(let id) = selection,
                  let place = MacPagePlace.afterChange(open: id, documents: documents) else { return }
            show(place)
        }
        .frame(minWidth: 920, minHeight: 480)
        .background(BoardStyle.paper)
        .announcesErrors(store.error)
        // A sheet of the window shows the error itself. While it is open, the notice of the window shows none.
        .sheet(isPresented: $createOpen) { BotEditorView().sheetShowsError() }
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
            // Library list shows, not the Inbox.
            if selected == nil, case .document(let id)? = old,
               MacPagePlace.afterChange(open: id, documents: store.documents) == .libraryList {
                show(.libraryList)
                return
            }
            chatPath = NavigationPath()
            // The Library row of the sidebar opens the list. A document card sets the page first and then the row.
            if selected != .library { libraryPath = NavigationPath() }
        }
        // Outside each sheet of the window, so that each sheet and each page gets it.
        .errorSheetRoot()
    }

    private func show(_ place: MacPagePlace) {
        switch place {
        case .pinnedRow(let id): selection = .document(id)
        case .inLibrary(let id):
            libraryPath = NavigationPath([DocumentRoute(id: id)])
            selection = .library
        case .libraryList:
            libraryPath = NavigationPath()
            selection = .library
        }
    }

    // A new case, with or without a stack of its own, needs no code for the notice of the window (MacNoticeArea).
    @ViewBuilder private var detail: some View {
        switch selection {
        case .inbox, nil:
            MacInboxView(selection: $selection)
        case .library:
            NavigationStack(path: $libraryPath) {
                LibraryView()
                    .navigationTitle("Library")
                    .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
            }
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
                MacInboxView(selection: $selection)
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
                    MacBotDetails(botId: botId, tab: $inspectorTab)
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

/// The sidebar: the Inbox, the pinned bots and the pinned pages, and the chats: every other bot and each team, with the
/// last message.
struct MacSidebar: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?
    @State private var deleting: Bot?
    @AppStorage("sidebar-unreads-open") private var unreadsOpen = true
    @AppStorage("sidebar-todos-open") private var todosOpen = true
    @AppStorage("sidebar-starred-open") private var starredOpen = true
    @AppStorage("sidebar-people-open") private var peopleOpen = true
    @AppStorage("sidebar-agents-open") private var agentsOpen = true
    @State private var archivedOpen = false

    /// The bots that are not archived, newest activity first, split into the pinned bots and the other bots.
    /// It reads only its arguments, so it needs no main actor, and a test can call it from any context.
    var body: some View {
        // One pass over the messages for all sections. A comparison of the sort read every message and parsed its
        // date, about 3 ms for each body with 1,500 messages.
        // The choices that wait for the Mac show at once (TeamChatChoices).
        let sections = ChatSections.make(bots: store.bots, messages: store.messages, rooms: store.shownTeamRooms,
                                         local: store.newestLocalMessages)
        let pins = SidebarPins(pinnedBots: sections.pinned, documents: store.documents ?? [], bots: store.bots,
                               teamBots: store.teamBots)
        return List(selection: $selection) {
            MacSidebarLabel(title: "Inbox", symbol: "tray")
                .badge(store.inboxBadgeCount)
                .tag(MacSelection.inbox)
                .accessibilityIdentifier("sidebar-inbox")
            MacSidebarLabel(title: "Library", symbol: "rectangle.3.group")
                .badge(store.documents?.count ?? 0)
                .tag(MacSelection.library)
                .accessibilityIdentifier("sidebar-library")
            // The sections of Slack: each one folds with its header, and the Mac keeps the choice. A section without
            // rows does not show.
            if !sections.unread.isEmpty {
                Section("Unreads", isExpanded: $unreadsOpen) {
                    ForEach(sections.unread) { item in itemRow(item) }
                }
            }
            // What the agents work on now and next. A To-do leaves the list when its agent finishes it.
            let todos = AgentTodos.make(artifacts: store.artifacts, bots: store.bots)
            if !todos.isEmpty {
                Section("To-dos", isExpanded: $todosOpen) {
                    ForEach(todos) { item in todoRow(item) }
                }
            }
            if !pins.isEmpty || !sections.pinnedChats.isEmpty {
                Section("Starred", isExpanded: $starredOpen) {
                    ForEach(pins.bots) { bot in row(bot) }
                    // The pinned chats of the teams, as the same rows that they have in "People".
                    ForEach(sections.pinnedChats) { item in itemRow(item) }
                    ForEach(pins.pages) { page in pageRow(page) }
                }
            }
            if !sections.people.isEmpty {
                Section("People", isExpanded: $peopleOpen) {
                    ForEach(sections.people) { item in itemRow(item) }
                }
            }
            if !sections.bots.isEmpty {
                Section("Agents", isExpanded: $agentsOpen) {
                    ForEach(sections.bots) { item in itemRow(item) }
                }
            }
            // The archived agents, closed until the person opens the section. An archived chat stays readable.
            let archived = store.bots.filter(\.archived).sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
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
        // A deleted bot leaves the list. Its chat closes, and the Inbox shows instead.
        .onChange(of: store.bots.map(\.id)) { _, ids in
            if case .bot(let id) = selection, !ids.contains(id) { selection = .inbox }
        }
        // A team that this Mac removed leaves the list too, and so does a named chat that the admin deleted.
        .onChange(of: store.team) { _, team in
            if case .team(let chat) = selection, let team, !team.has(chat) { selection = .inbox }
        }
        // The pinned pages need the list without a visit to the Library: one quiet load when the Mac connects and no
        // list is there. After that, the updates load the list again on each change of a page and on a reset.
        .task(id: store.connection == .connected) {
            if store.documents == nil { await store.loadDocuments(quiet: true) }
        }
        .safeAreaInset(edge: .bottom) { MacConnectionFooter() }
    }

    /// The row of a chat of a team. A chat of a team has Pin and Mute in its menu (TeamChatMuteMenu). A direct chat
    /// has no pin and no delete here. The Team screen has the leave action.
    @ViewBuilder private func chatRow(_ item: ChatListItem) -> some View {
        if let chat = item.chat {
            PeopleRow(item: item, avatarSize: 32)
                .padding(.vertical, BoardStyle.Space.xs)
                .tag(MacSelection.team(chat))
                .modifier(TeamChatMuteMenu(item: item))
                .accessibilityIdentifier(chat.member.map { "sidebar-direct-" + $0 }
                                         ?? "sidebar-team-" + chat.room + (chat.chat.map { "-" + $0 } ?? ""))
        }
    }

    /// A pinned page. Its menu only unpins: the Library and the page itself have the other actions.
    private func pageRow(_ page: BotDocument) -> some View {
        MacPinnedPageRow(page: page)
            .tag(MacSelection.document(page.id))
            .contextMenu {
                Button("Unstar") { Task { await store.pinDocument(page, pinned: false) } }
            }
            .accessibilityIdentifier("sidebar-page-\(page.key)")
    }

    private func row(_ bot: Bot) -> some View {
        MacSidebarRow(name: bot.name, unread: bot.unreadCount) {
            LiveBotAvatar(bot: bot, size: 20)
        }
            .tag(MacSelection.bot(bot.id))
            .contextMenu {
                if !bot.archived {
                    Button(bot.pinned ? "Unstar" : "Star") { Task { await store.pinBot(bot, pinned: !bot.pinned) } }
                }
                Button(bot.archived ? "Unarchive" : "Archive") { Task { await store.archiveBot(bot, archived: !bot.archived) } }
                Divider()
                Button("Delete Agent…", role: .destructive) { deleting = bot }
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

    /// A To-do: its state as a symbol, its title, and the agent that works on it. A click opens the agent's chat with
    /// the work attached.
    private func todoRow(_ item: Artifact) -> some View {
        let agent = store.bots.first { $0.id == item.botId }?.name ?? ""
        return HStack(spacing: BoardStyle.Space.s) {
            Image(systemName: AgentTodos.symbol(item.status))
                .font(.callout).foregroundStyle(item.status == "blocked" ? BoardStyle.destructive : .secondary)
                .frame(width: 20, height: 20)
            Text(item.title).lineLimit(1)
            Spacer(minLength: 0)
            Text(agent).font(.caption).foregroundStyle(.tertiary).lineLimit(1)
        }
        .padding(.vertical, 2)
        .help(item.preview.isEmpty ? item.title : item.preview)
        .tag(MacSelection.work(item.id))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("sidebar-todo-\(item.id)")
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

/// The details of a bot next to its chat: its settings, or its documents in the Library.
private struct MacBotDetails: View {
    let botId: String
    @Binding var tab: InspectorTab

    var body: some View {
        VStack(spacing: 0) {
            // Text tabs. The current one sits on a soft capsule. The same height and top space as the bot's title over
            // the chat, so both sit in one row.
            HStack(spacing: BoardStyle.Space.xs) {
                tabButton("Details", .details)
                tabButton("Library", .library)
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
                case .library:
                    LibraryView(botId: botId)
                        .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
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
