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
                .onChange(of: PushNotifications.shared.deviceToken) { _, _ in
                    Task { await store.syncPushRegistration() }
                }
                .onChange(of: PushNotifications.shared.preferences) { _, _ in
                    Task { await store.syncPushRegistration() }
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { store.setForeground(true) }
                    else if phase == .background { store.setForeground(false) }
                }
        }
    }
}

struct ConversationRoute: Hashable {
    let botId: String
    var decisionId: String? = nil
    var messageId: String? = nil
}

struct BotSettingsRoute: Hashable {
    let botId: String
}

enum HomeDestination: Hashable {
    case inbox, archived
}

enum HomeTab: Hashable { case bots, work, inbox }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var settingsOpen = false
    @State private var createOpen = false
    @State private var searchOpen = false
    @State private var searchText = ""
    @State private var selectedTab: HomeTab = .bots
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var botsPath = NavigationPath()
    @State private var inboxPath = NavigationPath()
    @State private var workPath = NavigationPath()
    @State private var keyboardVisible = false
    @FocusState private var searchFocused: Bool

    private var tabBarVisibility: Visibility {
        let activePath = selectedTab == .inbox ? inboxPath : selectedTab == .work ? workPath : botsPath
        return keyboardVisible || !activePath.isEmpty ? .hidden : .automatic
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.bots) {
                root(tab: .bots, path: $botsPath)
            } label: {
                Label { Text("Bots") } icon: {
                    Image(systemName: "bubble.left.and.bubble.right").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-bots")
            Tab(value: HomeTab.work) {
                root(tab: .work, path: $workPath)
            } label: {
                Label { Text("Work") } icon: {
                    Image(systemName: "checklist").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-work")
            Tab(value: HomeTab.inbox) {
                root(tab: .inbox, path: $inboxPath)
            } label: {
                Label { Text("Inbox") } icon: {
                    Image(systemName: "tray").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-inbox")
        }
        .modifier(NativeTabCollapse())
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
        .onChange(of: selectedTab) { _, _ in searchFocused = false }
        .sheet(isPresented: $settingsOpen) { AppSettingsView() }
        .sheet(isPresented: $createOpen) { BotEditorView() }
        .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
        .onChange(of: store.lastSync) { _, _ in openNotification() }
        .onChange(of: store.connection) { _, _ in openNotification() }
        .onAppear { openNotification() }
    }

    private func root(tab: HomeTab, path: Binding<NavigationPath>) -> some View {
        NavigationStack(path: path) {
            Group {
                if tab == .inbox { DecisionInboxView(focusDecisionId: $notificationDecisionId) }
                else if tab == .work {
                    WorkView(isVisible: selectedTab == .work && workPath.isEmpty) { botId in
                        workPath.append(ConversationRoute(botId: botId))
                    }
                }
                else { HomeLaunchView(searchText: searchText) }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                .navigationTitle(tab == .work ? "Work" : "")
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
                                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(width: 44, height: 44)
                            }.accessibilityLabel("Close search").accessibilityIdentifier("close-search")
                        }
                        .padding(.leading, 16).padding(.trailing, 4)
                        .background(.regularMaterial, in: Capsule())
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
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId, focusMessageId: route.messageId)
                }
                .navigationDestination(for: BotExchangeRoute.self) { route in
                    BotExchangeView(route: route)
                }
                .navigationDestination(for: BotSettingsRoute.self) { route in
                    BotSettingsView(botId: route.botId)
                }
                .background(BoardStyle.paper)
                .overlay(alignment: .top) {
                    if let error = store.error {
                        ErrorBanner(message: error) { store.error = nil }.padding(.horizontal, 16)
                    }
                }
        }
    }

    @ToolbarContentBuilder
    private func rootToolbar(inbox: Bool) -> some ToolbarContent {
        if !inbox {
            ToolbarItem(placement: .topBarLeading) {
                Button { settingsOpen = true } label: {
                    Image(systemName: "person.crop.circle")
                }.accessibilityLabel("App settings").accessibilityIdentifier("app-settings")
            }
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
        guard !openingNotification else { return }
        openingNotification = true
        Task {
            let destination = await store.notificationDestination(target)
            openingNotification = false
            guard PushNotifications.shared.target == target else { openNotification(); return }
            guard let destination else { return }
            settingsOpen = false; createOpen = false; searchOpen = false; searchFocused = false
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            switch destination {
            case .inbox(let decisionId):
                inboxPath = NavigationPath()
                notificationDecisionId = decisionId
                selectedTab = .inbox
            case .conversation(let route):
                notificationDecisionId = nil
                selectedTab = .bots
                botsPath = NavigationPath()
                botsPath.append(route)
            }
            PushNotifications.shared.target = nil
        }
    }

}

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "exclamationmark.circle").padding(.top, 3)
            Text(message).font(.footnote).textSelection(.enabled)
            Spacer(minLength: 0)
            Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 17)).frame(width: 44, height: 44) }
                .accessibilityLabel("Dismiss error")
        }
        .foregroundStyle(.secondary)
        .padding(.leading, 14)
        .padding(.vertical, 8)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 16))
        .accessibilityIdentifier("action-error")
    }
}

struct ConnectionRow: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        HStack(spacing: 8) {
            Text(title).font(.caption.weight(.medium))
            Spacer()
            if let lastSync = store.lastSync {
                CompactAgeLabel(date: lastSync)
                    .font(.caption2.monospacedDigit())
                    .accessibilityLabel("Last synchronized \(lastSync.formatted())")
            }
        }
        .foregroundStyle(.secondary)
        .accessibilityElement(children: .combine)
    }

    private var title: String {
        switch store.connection {
        case .unconfigured: "Connect your Mac"
        case .connecting: "Connecting to Mac"
        case .connected: "Mac connected"
        case .disconnected: "Waiting for connection"
        case .preview: "Mac · Preview"
        }
    }

}

struct CompactAgeLabel: View {
    let date: Date

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { context in
            Text(label(relativeTo: context.date))
        }
    }

    private func label(relativeTo now: Date) -> String {
        let age = max(0, now.timeIntervalSince(date))
        if age < 60 { return "Now" }
        if age < 3600 { return "\(Int(age / 60)) min" }
        if age < 86400 { return "\(Int(age / 3600)) hr" }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }
}

private struct NativeTabCollapse: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.tabBarMinimizeBehavior(.onScrollDown)
        } else {
            content
        }
    }
}
