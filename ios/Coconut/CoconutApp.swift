import SwiftUI

@main
struct CoconutApp: App {
    @UIApplicationDelegateAdaptor(CoconutAppDelegate.self) private var appDelegate
    @State private var store = AppStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            CoconutHome()
                .environment(store)
                .tint(.primary)
                .task { store.start() }
                .onOpenURL { url in
                    // A Hermes agent sends this link. Coconut shows the Mac address before it pairs.
                    if PairingSetup.isLink(url) { store.setupLink = url.absoluteString }
                }
                .onChange(of: store.reconciledBadgeCount, initial: true) { _, _ in
                    if !store.isDemo { store.reconcileBadge() }
                }
                .onChange(of: PushNotifications.shared.deviceToken) { _, _ in
                    Task { await store.syncPushRegistration() }
                }
                .onChange(of: PushNotifications.shared.choices) { _, _ in
                    Task { await store.syncPushRegistration() }
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        store.setForeground(true)
                        if !store.isDemo { store.reconcileBadge() }
                    }
                    else if phase == .background { store.setForeground(false) }
                }
        }
    }
}

enum HomeTab: Hashable { case bots, inbox, library }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var settingsOpen = false
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var createOpen = false
    @State private var searchOpen = false
    @State private var searchText = ""
    @State private var selectedTab: HomeTab = .inbox
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    @State private var botsPath = NavigationPath()
    @State private var inboxPath = NavigationPath()
    @State private var libraryPath = NavigationPath()
    @State private var keyboardVisible = false
    @State private var conversationFooters: [HomeTab: Bool] = [:]
    @FocusState private var searchFocused: Bool

    private var conversationCanMinimize: Bool? { conversationFooters[selectedTab] }

    private var tabBarVisibility: Visibility {
        let activePath = switch selectedTab { case .inbox: inboxPath; case .bots: botsPath; case .library: libraryPath }
        return keyboardVisible || (!activePath.isEmpty && conversationCanMinimize == nil) ? .hidden : .automatic
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.inbox) {
                root(tab: .inbox, path: $inboxPath)
            } label: {
                Label { Text("Inbox") } icon: {
                    Image(systemName: "tray").environment(\.symbolVariants, .none)
                }
            }.badge(store.inboxBadgeCount)
                .accessibilityIdentifier("home-inbox")
            Tab(value: HomeTab.bots) {
                root(tab: .bots, path: $botsPath)
            } label: {
                Label { Text("Bots") } icon: {
                    Image(systemName: "bubble.left.and.bubble.right").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-bots")
            Tab(value: HomeTab.library) {
                root(tab: .library, path: $libraryPath)
            } label: {
                Label { Text("Library") } icon: {
                    Image(systemName: "books.vertical").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-library")
        }
        .modifier(NativeTabCollapse(enabled: conversationCanMinimize == true && !keyboardVisible))
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
        .onChange(of: selectedTab) { _, tab in
            searchFocused = false
            if previewedNotification?.decisionId != nil && tab != .inbox {
                PushNotifications.shared.target = nil
                previewedNotification = nil
            }
        }
        .sheet(isPresented: $settingsOpen) { AppSettingsView() }
        .fullScreenCover(isPresented: $onboardingOpen) { OnboardingView(firstRun: onboardingFirstRun) }
        .onAppear { if OnboardingView.showsOnLaunch(store) { onboardingFirstRun = true; onboardingOpen = true } }
        .onChange(of: store.setupLink, initial: true) { _, link in
            // A tapped pairing link opens the pairing screen. A pairing screen that shows, also one from Settings, takes the link itself.
            guard link != nil, !onboardingOpen, !OnboardingView.isOpen else { return }
            onboardingFirstRun = false
            let sheetOpen = settingsOpen || createOpen
            settingsOpen = false
            createOpen = false
            Task {
                // UIKit presents the full-screen page only after the open sheet has gone.
                if sheetOpen { try? await Task.sleep(for: .milliseconds(450)) }
                onboardingOpen = true
            }
        }
        .sheet(isPresented: $createOpen) { BotEditorView() }
        .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
        .onChange(of: store.lastSync) { _, _ in openNotification() }
        .onChange(of: store.connection) { _, _ in openNotification() }
        .onAppear { openNotification() }
    }

    private func root(tab: HomeTab, path: Binding<NavigationPath>) -> some View {
        NavigationStack(path: path) {
            Group {
                switch tab {
                case .inbox: DecisionInboxView(focusDecisionId: $notificationDecisionId, refreshingDecisionId: PushNotifications.shared.target?.decisionId)
                case .bots: HomeLaunchView(searchText: searchText)
                case .library: LibraryView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { rootToolbar(inbox: tab != .bots) }
                .safeAreaInset(edge: .top, spacing: 0) {
                    if searchOpen && tab == .bots {
                        HStack(spacing: 10) {
                            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                            TextField("Search conversations", text: $searchText)
                                .focused($searchFocused).textInputAutocapitalization(.never)
                                .autocorrectionDisabled().accessibilityIdentifier("home-search-input")
                            Button { searchFocused = false; searchOpen = false; searchText = "" } label: {
                                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                            }.accessibilityLabel("Close search").accessibilityIdentifier("close-search")
                        }
                        .padding(.leading, 16).padding(.trailing, 4)
                        .background(.regularMaterial, in: Capsule())
                        .padding(.horizontal, 16).padding(.bottom, 8)
                    }
                }
                .safeAreaInset(edge: .top, spacing: 0) {
                    if !store.isDemo, let notice = store.connection.notice {
                        ConnectionBanner(notice: notice) {
                            onboardingFirstRun = false
                            onboardingOpen = true
                        }
                        .padding(.horizontal, 16).padding(.bottom, 8)
                    }
                }
                .navigationDestination(for: HomeDestination.self) { destination in
                    switch destination {
                    case .inbox:
                        DecisionInboxView().navigationTitle("Inbox").navigationBarTitleDisplayMode(.inline)
                    case .archived:
                        ArchivedBotsView()
                    }
                }
                .navigationDestination(for: ConversationRoute.self) { route in
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId, focusMessageId: route.messageId, attachedWork: route.workContext)
                }
                .navigationDestination(for: BotExchangeRoute.self) { route in
                    BotExchangeView(route: route)
                }
                .navigationDestination(for: BotSettingsRoute.self) { route in
                    BotSettingsView(botId: route.botId)
                }
                .navigationDestination(for: DocumentRoute.self) { route in
                    DocumentView(documentId: route.id)
                }
                .background(BoardStyle.paper)
                .overlay(alignment: .top) {
                    if let error = store.error {
                        ErrorBanner(message: error) { store.error = nil }.padding(.horizontal, 16)
                    }
                }
        }
        .onPreferenceChange(ConversationFooterPreference.self) { allowed in
            conversationFooters[tab] = allowed
        }
    }

    @ToolbarContentBuilder
    private func rootToolbar(inbox: Bool) -> some ToolbarContent {
        // Settings is the one app-wide action, so both tabs show it.
        ToolbarItem(placement: .topBarLeading) {
            Button { settingsOpen = true } label: {
                Image(systemName: "gearshape")
            }.accessibilityLabel("App settings").accessibilityIdentifier("app-settings")
        }
        if !inbox {
            ToolbarItem(placement: .topBarTrailing) {
                Button { searchOpen = true; searchFocused = true } label: {
                    Image(systemName: "magnifyingglass")
                }.accessibilityLabel("Search conversations").accessibilityIdentifier("search-conversations")
            }
            if #available(iOS 26.0, *) { ToolbarSpacer(.fixed, placement: .topBarTrailing) }
            ToolbarItem(placement: .topBarTrailing) {
                Button { createOpen = true } label: {
                    Image(systemName: "plus")
                }.accessibilityLabel("Create bot").accessibilityIdentifier("create-bot")
            }
        }
    }

    private func openNotification() {
        guard let target = PushNotifications.shared.target else { return }
        guard store.acceptsNotification(target) else {
            PushNotifications.shared.target = nil
            return
        }
        if target.group || target.decisionId == nil {
            PushNotifications.shared.target = nil
            previewedNotification = nil
            showNotification(target.group ? .inboxList : .conversation(target.conversationRoute))
            return
        }
        if previewedNotification != target {
            previewedNotification = target
            showNotification(target.decisionId.map(NotificationDestination.inbox) ?? .conversation(target.conversationRoute))
        }
        guard !openingNotification else { return }
        openingNotification = true
        Task {
            let destination = await store.notificationDestination(target)
            openingNotification = false
            guard PushNotifications.shared.target == target else { openNotification(); return }
            guard let destination else { return }
            PushNotifications.shared.target = nil
            previewedNotification = nil
            showNotification(destination)
        }
    }

    private func showNotification(_ destination: NotificationDestination) {
        settingsOpen = false; createOpen = false; searchOpen = false; searchFocused = false
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        switch destination {
        case .inbox(let decisionId):
            inboxPath = NavigationPath()
            notificationDecisionId = decisionId
            selectedTab = .inbox
        case .inboxList:
            inboxPath = NavigationPath()
            notificationDecisionId = nil
            selectedTab = .inbox
        case .conversation(let route):
            notificationDecisionId = nil
            selectedTab = .bots
            botsPath = NavigationPath()
            botsPath.append(route)
        }
    }

}

private struct NativeTabCollapse: ViewModifier {
    let enabled: Bool

    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.tabBarMinimizeBehavior(enabled ? .onScrollUp : .never)
        } else {
            content
        }
    }
}
