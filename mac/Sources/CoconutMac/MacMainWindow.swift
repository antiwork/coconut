import CoconutKit
import SwiftUI

/// What the main window shows in its detail column.
enum MacSelection: Hashable {
    case inbox
    case library
    case bot(String)
    /// The chat of a team, by room ID.
    case team(String)
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
                        .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 300)
                        .toolbar {
                            ToolbarItem {
                                Button { createOpen = true } label: { Label("New bot", systemImage: "plus") }
                                    .keyboardShortcut("n", modifiers: .command)
                                    .help("Create a bot")
                            }
                        }
                } detail: {
                    detail
                }
            } else {
                MacPairingView(model: model)
            }
        }
        // The columns at their widest sidebar (300) fit side by side: the Inbox list (220) and its card (300), and a
        // chat (300) and its details (320). When they did not fit, the sidebar moved out to the left and the right side
        // was cut off.
        .environment(\.openChat) { id in selection = .bot(id) }
        .environment(\.openDocument) { id in
            libraryPath = NavigationPath()
            libraryPath.append(DocumentRoute(id: id))
            selection = .library
        }
        .frame(minWidth: 920, minHeight: 480)
        .background(BoardStyle.paper)
        .sheet(isPresented: $createOpen) { BotEditorView() }
        .sheet(isPresented: $pairingOpen) { MacPairingView(model: model).frame(width: 520, height: 600) }
        .onChange(of: store.setupLink) { _, link in if link != nil { pairingOpen = true } }
        // The sheet waits for each pairing page: the sheet of a link, and the page of a window without a pairing.
        .sheet(item: Binding(get: {
            TeamJoinGate.canShow(store.teamJoin, showing: nil, pairingOpen: pairingOpen || PairingPages.shared.isOpen)
                ? store.teamJoin : nil
        }, set: { store.teamJoin = $0 })) {
            TeamJoinView(link: $0.link)
        }
        // Each choice in the sidebar starts at its own root, so another bot never shows the last bot's pages.
        .onChange(of: selection) { _, selected in
            chatPath = NavigationPath()
            // The Library row of the sidebar opens the list. A document card sets the page first and then the row.
            if selected != .library { libraryPath = NavigationPath() }
        }
        .overlay(alignment: .top) {
            if let error = store.error {
                ErrorBanner(message: error) { store.error = nil }.padding(.horizontal, 16).padding(.top, 8)
            }
        }
    }

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
        case .team(let roomId):
            // A team has no details column, so the chat takes the whole detail column and has no control for one.
            NavigationStack(path: $chatPath) {
                TeamChatView(roomId: roomId)
                    .id(roomId)
            }
            .ignoresSafeArea(.container, edges: .top)
            .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
            .toolbar(removing: .title)
        }
    }
}

/// The sidebar: the Inbox, the pinned bots, and the chats: every other bot and each team, with the last message.
struct MacSidebar: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?
    @State private var deleting: Bot?

    /// The bots that are not archived, newest activity first, split into the pinned bots and the other bots.
    /// It reads only its arguments, so it needs no main actor, and a test can call it from any context.
    var body: some View {
        // One pass over the messages for all sections. A comparison of the sort read every message and parsed its
        // date, about 3 ms for each body with 1,500 messages.
        let sections = ChatSections.make(bots: store.bots, messages: store.messages, rooms: store.team?.rooms ?? [])
        return List(selection: $selection) {
            MacSidebarLabel(title: "Inbox", symbol: "tray")
                .badge(store.inboxBadgeCount)
                .tag(MacSelection.inbox)
                .accessibilityIdentifier("sidebar-inbox")
            MacSidebarLabel(title: "Library", symbol: "rectangle.3.group")
                .badge(store.documents?.count ?? 0)
                .tag(MacSelection.library)
                .accessibilityIdentifier("sidebar-library")
            // A section without rows does not show.
            if !sections.pinned.isEmpty {
                Section("Pinned") {
                    ForEach(sections.pinned) { bot in row(bot) }
                }
            }
            if !sections.people.isEmpty {
                Section("People") {
                    ForEach(sections.people) { room in
                        // A team has no pin and no delete here. The Team screen has its leave action.
                        TeamContactRow(room: room, avatarSize: 32)
                            .padding(.vertical, 2)
                            .tag(MacSelection.team(room.room))
                            .accessibilityIdentifier("sidebar-team-\(room.room)")
                    }
                }
            }
            if !sections.bots.isEmpty {
                Section("Bots") {
                    ForEach(sections.bots) { bot in row(bot) }
                }
            }
        }
        .listStyle(.sidebar)
        .modifier(DeleteBotDialog(bot: $deleting))
        // A deleted bot leaves the list. Its chat closes, and the Inbox shows instead.
        .onChange(of: store.bots.map(\.id)) { _, ids in
            if case .bot(let id) = selection, !ids.contains(id) { selection = .inbox }
        }
        // A team that this Mac removed leaves the list too.
        .onChange(of: store.team?.rooms.map(\.room)) { _, ids in
            if case .team(let id) = selection, let ids, !ids.contains(id) { selection = .inbox }
        }
        .safeAreaInset(edge: .bottom) { MacConnectionFooter() }
    }

    private func row(_ bot: Bot) -> some View {
        BotContactRow(bot: bot, avatarSize: 32)
            .padding(.vertical, 2)
            .tag(MacSelection.bot(bot.id))
            .contextMenu {
                Button(bot.pinned ? "Unpin" : "Pin") { Task { await store.pinBot(bot, pinned: !bot.pinned) } }
                Divider()
                Button("Delete Bot…", role: .destructive) { deleting = bot }
            }
            .accessibilityIdentifier("sidebar-bot-\(bot.id)")
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
        .font(.caption).foregroundStyle(.secondary)
        .padding(.horizontal, 12).padding(.vertical, 10)
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
            HStack(spacing: 2) {
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
