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
}

struct BotSettingsRoute: Hashable {
    let botId: String
}

enum HomeDestination: Hashable {
    case inbox, archived
}

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var settingsOpen = false
    @State private var createOpen = false
    @State private var searchOpen = false
    @State private var searchText = ""
    @State private var path = NavigationPath()
    @FocusState private var searchFocused: Bool

    private var homeBar: some View {
        HStack(spacing: 8) {
            Button { settingsOpen = true } label: {
                Image(systemName: "person.fill")
                    .font(.system(size: 19, weight: .medium)).foregroundStyle(.secondary)
                    .frame(width: 38, height: 38)
                    .background(BoardStyle.ink.opacity(0.09), in: Circle())
                    .frame(width: 44, height: 44)
            }.accessibilityLabel("App settings").accessibilityIdentifier("app-settings")
            Spacer(minLength: 16)
            Button { searchOpen = true; searchFocused = true } label: {
                Image(systemName: "magnifyingglass").font(.system(size: 22, weight: .regular))
                    .frame(width: 44, height: 44)
            }.accessibilityLabel("Search conversations").accessibilityIdentifier("search-conversations")
            Button { createOpen = true } label: {
                Image(systemName: "plus").font(.system(size: 25, weight: .light))
                    .frame(width: 44, height: 44)
            }.accessibilityLabel("Create bot").accessibilityIdentifier("create-bot")
        }
        .buttonStyle(HomeCircleStyle())
        .padding(.horizontal, 18).padding(.top, 12).padding(.bottom, 14)
        .background(BoardStyle.paper)
    }

    var body: some View {
        NavigationStack(path: $path) {
            HomeLaunchView(searchText: searchText)
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar(path.isEmpty ? .hidden : .visible, for: .navigationBar)
                .safeAreaInset(edge: .top, spacing: 0) {
                    VStack(spacing: 0) {
                        homeBar
                        if searchOpen {
                            HStack(spacing: 10) {
                                Image(systemName: "magnifyingglass").font(.system(size: 17)).foregroundStyle(.secondary)
                                TextField("Search conversations", text: $searchText)
                                    .focused($searchFocused).textInputAutocapitalization(.never)
                                    .autocorrectionDisabled().accessibilityIdentifier("home-search-input")
                                Button { searchFocused = false; searchOpen = false; searchText = "" } label: {
                                    Image(systemName: "xmark.circle.fill").font(.system(size: 17)).foregroundStyle(.secondary).frame(width: 44, height: 44)
                                }.accessibilityLabel("Close search").accessibilityIdentifier("close-search")
                            }
                            .padding(.leading, 24).padding(.trailing, 12)
                            .background(BoardStyle.paper)
                        }
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
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId)
                }
                .navigationDestination(for: BotExchangeRoute.self) { route in
                    BotExchangeView(route: route)
                }
                .navigationDestination(for: BotSettingsRoute.self) { route in
                    BotSettingsView(botId: route.botId)
                }
                .sheet(isPresented: $settingsOpen) { AppSettingsView() }
                .sheet(isPresented: $createOpen) { BotEditorView() }
                .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
                .onAppear { openNotification() }
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
        settingsOpen = false; createOpen = false
        path = NavigationPath()
        path.append(target.conversationRoute)
        PushNotifications.shared.target = nil
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

private struct HomeCircleStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(BoardStyle.ink)
            .background(BoardStyle.surface.opacity(0.45), in: Circle())
            .overlay(Circle().stroke(BoardStyle.line, lineWidth: 0.8))
            .opacity(configuration.isPressed ? 0.55 : 1)
    }
}
