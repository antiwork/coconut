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
    var workContext: WorkReference? = nil
}

struct BotSettingsRoute: Hashable {
    let botId: String
}

enum HomeDestination: Hashable {
    case inbox, archived, bots
}

enum HomeTab: Hashable { case coconut, inbox }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var settingsOpen = false
    @State private var selectedTab: HomeTab = .coconut
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    @State private var botsPath = NavigationPath()
    @State private var inboxPath = NavigationPath()
    @State private var keyboardVisible = false

    private var tabBarVisibility: Visibility {
        let activePath = selectedTab == .inbox ? inboxPath : botsPath
        return keyboardVisible || !activePath.isEmpty ? .hidden : .automatic
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.coconut) {
                root(tab: .coconut, path: $botsPath)
            } label: {
                Label { Text("Coconut") } icon: {
                    Image(systemName: "bubble.left.and.bubble.right").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-coconut")
            Tab(value: HomeTab.inbox) {
                root(tab: .inbox, path: $inboxPath)
            } label: {
                Label { Text("Inbox") } icon: {
                    Image(systemName: "tray").environment(\.symbolVariants, .none)
                }
            }.badge(store.inboxBadgeCount)
                .accessibilityIdentifier("home-inbox")
        }
        .modifier(NativeTabCollapse())
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
        .onChange(of: selectedTab) { _, tab in
            if previewedNotification?.decisionId != nil && tab != .inbox {
                PushNotifications.shared.target = nil
                previewedNotification = nil
            }
        }
        .sheet(isPresented: $settingsOpen) { AppSettingsView() }
        .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
        .onChange(of: store.lastSync) { _, _ in openNotification() }
        .onChange(of: store.connection) { _, _ in openNotification() }
        .onAppear { openNotification() }
        .onChange(of: store.activityReference) { _, route in
            guard let route else { return }
            settingsOpen = false
            if selectedTab == .inbox { inboxPath.append(route) } else { botsPath.append(route) }
            store.activityReference = nil
        }
        .onChange(of: store.coconutReply) { _, reply in
            guard reply != nil else { return }
            settingsOpen = false
            selectedTab = .coconut
            botsPath = NavigationPath()
        }
    }

    private func root(tab: HomeTab, path: Binding<NavigationPath>) -> some View {
        NavigationStack(path: path) {
            Group {
                if tab == .inbox { DecisionInboxView(focusDecisionId: $notificationDecisionId, refreshingDecisionId: PushNotifications.shared.target?.decisionId) }
                else if let bot = store.conversationBot {
                    ConversationView(botId: bot.id, isRoot: true, openSettings: { settingsOpen = true }).id(bot.id)
                } else { ConversationSetupView() }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)

                .navigationDestination(for: HomeDestination.self) { destination in
                    switch destination {
                    case .inbox:
                        DecisionInboxView().navigationTitle("Inbox").navigationBarTitleDisplayMode(.inline)
                    case .archived:
                        ArchivedBotsView()
                    case .bots:
                        BotDirectoryView()
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
                .background(BoardStyle.paper)
                .overlay(alignment: .top) {
                    if let error = store.error {
                        ErrorBanner(message: error) { store.error = nil }.padding(.horizontal, 16)
                    }
                }
        }
    }

    private func openNotification() {
        guard let target = PushNotifications.shared.target else { return }
        guard store.acceptsNotification(target) else {
            PushNotifications.shared.target = nil
            return
        }
        if target.decisionId == nil {
            PushNotifications.shared.target = nil
            previewedNotification = nil
            showNotification(.conversation(target.conversationRoute))
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
        settingsOpen = false
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        switch destination {
        case .inbox(let decisionId):
            inboxPath = NavigationPath()
            notificationDecisionId = decisionId
            selectedTab = .inbox
        case .conversation(let route):
            notificationDecisionId = nil
            selectedTab = .coconut
            botsPath = NavigationPath()
            botsPath.append(route)
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
