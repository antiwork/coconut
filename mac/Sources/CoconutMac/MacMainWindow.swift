import SwiftUI

/// What the main window shows in its detail column.
enum MacSelection: Hashable {
    case inbox
    case library
    case archived
    case bot(String)
}

/// The tabs of the inspector next to a chat.
private enum InspectorTab: Hashable {
    case details, library
}

/// The main window of the Mac client: the bots in a sidebar, the chat or the Inbox in the middle, and the details of the
/// bot in an inspector on the right. It shows the pairing page until this Mac has a pairing.
struct MacMainWindow: View {
    @Environment(AppStore.self) private var store
    @ObservedObject var model: AppModel
    @State private var selection: MacSelection? = .inbox
    @State private var inspectorShown = true
    @State private var inspectorTab = InspectorTab.details
    @State private var createOpen = false
    @State private var pairingOpen = false

    var body: some View {
        Group {
            if store.hasPairing || store.isDemo {
                NavigationSplitView {
                    MacSidebar(selection: $selection)
                        .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 340)
                        .toolbar {
                            ToolbarItem {
                                Button { createOpen = true } label: { Label("New bot", systemImage: "square.and.pencil") }
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
        .frame(minWidth: 720, minHeight: 480)
        .background(BoardStyle.paper)
        .sheet(isPresented: $createOpen) { BotEditorView() }
        .sheet(isPresented: $pairingOpen) { MacPairingView(model: model).frame(width: 520, height: 600) }
        .onChange(of: store.setupLink) { _, link in if link != nil { pairingOpen = true } }
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
            NavigationStack {
                LibraryView()
                    .navigationTitle("Library")
                    .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
            }
        case .archived:
            NavigationStack {
                ArchivedBotsView()
                    .navigationDestination(for: ConversationRoute.self) { route in
                        ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                         focusMessageId: route.messageId, attachedWork: route.workContext)
                    }
                    .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                    .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
            }
        case .bot(let botId):
            NavigationStack {
                ConversationView(botId: botId)
                    .id(botId)
                    .navigationDestination(for: ConversationRoute.self) { route in
                        ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                         focusMessageId: route.messageId, attachedWork: route.workContext)
                    }
                    .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                    .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
            }
            // The pill over the chat names the bot, so the window shows no title of its own. Without this, the title of
            // the bot settings in the inspector repeats the bot name.
            .toolbar(removing: .title)
            .inspector(isPresented: $inspectorShown) {
                VStack(spacing: 0) {
                    Picker("Show", selection: $inspectorTab) {
                        Text("Details").tag(InspectorTab.details)
                        Text("Library").tag(InspectorTab.library)
                    }
                    .pickerStyle(.segmented).labelsHidden()
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    NavigationStack {
                        // Each bot gets its own settings view. Leaving it saves its edits to that bot, so an unsaved
                        // edit never moves to the next bot.
                        switch inspectorTab {
                        case .details: BotSettingsView(botId: botId).id(botId)
                        case .library:
                            LibraryView(botId: botId)
                                .navigationDestination(for: DocumentRoute.self) { DocumentView(documentId: $0.id) }
                        }
                    }
                }
                // A grouped Mac form keeps each section in a box, as the iPhone settings show them.
                .formStyle(.grouped)
                .inspectorColumnWidth(min: 260, ideal: 320, max: 420)
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { inspectorShown.toggle() } label: { Label("Details", systemImage: "sidebar.trailing") }
                        .keyboardShortcut("i", modifiers: .command)
                        .help("Show or hide the details of the bot")
                }
            }
        }
    }
}

/// The sidebar: the Inbox, the pinned bots, and every other bot with its last message.
struct MacSidebar: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?

    private var activeBots: [Bot] {
        store.bots.filter { !$0.archived }.sorted { lastActivity($0) > lastActivity($1) }
    }
    private var pinned: [Bot] { activeBots.filter(\.pinned) }
    private var others: [Bot] { activeBots.filter { !$0.pinned } }

    var body: some View {
        List(selection: $selection) {
            Label("Inbox", systemImage: "tray")
                .badge(store.inboxBadgeCount)
                .tag(MacSelection.inbox)
                .accessibilityIdentifier("sidebar-inbox")
            Label("Library", systemImage: "books.vertical")
                .badge(store.documents?.count ?? 0)
                .tag(MacSelection.library)
                .accessibilityIdentifier("sidebar-library")
            if !pinned.isEmpty {
                Section("Pinned") {
                    ForEach(pinned) { bot in row(bot) }
                }
            }
            Section("Bots") {
                ForEach(others) { bot in row(bot) }
            }
            let archived = store.bots.filter(\.archived).count
            if archived > 0 {
                Label("Archived", systemImage: "archivebox")
                    .badge(archived)
                    .tag(MacSelection.archived)
                    .accessibilityIdentifier("sidebar-archived")
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) { MacConnectionFooter() }
    }

    private func row(_ bot: Bot) -> some View {
        BotContactRow(bot: bot, avatarSize: 32)
            .padding(.vertical, 2)
            .tag(MacSelection.bot(bot.id))
            .contextMenu {
                Button(bot.pinned ? "Unpin" : "Pin") { Task { await store.pinBot(bot, pinned: !bot.pinned) } }
            }
            .accessibilityIdentifier("sidebar-bot-\(bot.id)")
    }

    private func lastActivity(_ bot: Bot) -> Date {
        store.messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
            ?? bot.createdAt.apiDate ?? .distantPast
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
