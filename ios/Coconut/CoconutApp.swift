import Foundation
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
                    // An invite link of a team opens the join sheet. The link stays in memory only.
                    if TeamLinkCheck.isLink(url) { store.teamJoin = TeamJoinRequest(link: url.absoluteString) }
                }
                // The web form of an invite link, https://coconut.gumroad.com/i/<token>, arrives as a browsing
                // activity, because coconut.gumroad.com is an associated domain of the app. The same invite as a
                // coconut:// link arrives above.
                .onContinueUserActivity(NSUserActivityTypeBrowsingWeb) { activity in
                    guard let url = activity.webpageURL, TeamLinkCheck.isLink(url) else { return }
                    store.teamJoin = TeamJoinRequest(link: url.absoluteString)
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

enum HomeTab: Hashable { case home, bots, library, business }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var settingsOpen = false
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var createOpen = false
    @State private var selectedTab: HomeTab = .home
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    /// The place that the preview of `previewedNotification` showed.
    @State private var previewedPlace: NotificationDestination?
    @State private var botsPath = NavigationPath()
    @State private var homePath = NavigationPath()
    @State private var cardsShown = false
    @State private var libraryPath = NavigationPath()
    @State private var businessPath = NavigationPath()
    @State private var keyboardVisible = false
    @State private var conversationFooters: [HomeTab: Bool] = [:]
    @State private var teamJoin: TeamJoinRequest?
    @State private var shownTeamJoin: UUID?

    private var conversationCanMinimize: Bool? { conversationFooters[selectedTab] }

    /// The cards that wait show alone on the stack of Home. They keep the tab bar, as a tab root does.
    private var cardsOnTop: Bool { selectedTab == .home && cardsShown && homePath.count == 1 }

    private var tabBarVisibility: Visibility {
        let activePath = switch selectedTab { case .home: homePath; case .bots: botsPath; case .library: libraryPath; case .business: businessPath }
        return keyboardVisible || (!activePath.isEmpty && conversationCanMinimize == nil && !cardsOnTop) ? .hidden : .automatic
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.home) {
                root(tab: .home, path: $homePath)
            } label: {
                Label { Text("Home") } icon: {
                    Image(systemName: "house").environment(\.symbolVariants, .none)
                }
            }.badge(HomeFeed.badge(store.homeUnread))
                .accessibilityIdentifier("home-home")
            Tab(value: HomeTab.bots) {
                root(tab: .bots, path: $botsPath)
            } label: {
                Label { Text("Chats") } icon: {
                    Image(systemName: "bubble.left.and.bubble.right").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-bots")
            Tab(value: HomeTab.business) {
                root(tab: .business, path: $businessPath)
            } label: {
                Label("Business", systemImage: "square.grid.2x2")
            }.accessibilityIdentifier("home-business")
            Tab(value: HomeTab.library) {
                root(tab: .library, path: $libraryPath)
            } label: {
                Label { Text("Library") } icon: {
                    Image(systemName: "rectangle.3.group").environment(\.symbolVariants, .none)
                }
            }.accessibilityIdentifier("home-library")
        }
        .modifier(NativeTabCollapse(enabled: conversationCanMinimize == true && !keyboardVisible))
        // A document card in a chat opens the document on the Library tab, so Back leads to the Library.
        .environment(\.openDocument) { id in
            libraryPath = NavigationPath()
            libraryPath.append(DocumentRoute(id: id))
            selectedTab = .library
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
        .onChange(of: selectedTab) { _, tab in
            if previewedNotification?.decisionId != nil && tab != .home {
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
        .sheet(isPresented: $createOpen) { BotEditorView().sheetShowsError() }
        .sheet(item: $teamJoin, onDismiss: {
            // A newer link can wait for this sheet to close. Only the link of this sheet goes.
            if store.teamJoin?.id == shownTeamJoin { store.teamJoin = nil }
        }) { TeamJoinView(link: $0.link).sheetShowsError() }
        .onChange(of: store.teamJoin, initial: true) { _, _ in showTeamJoin() }
        // A link that came while a pairing page showed opens its sheet after the page, also after a new pairing. The
        // page of Settings has its own state, so the count of the pages decides.
        .onChange(of: onboardingOpen) { _, _ in showTeamJoin() }
        .onChange(of: PairingPages.shared.isOpen) { _, _ in showTeamJoin() }
        // After the sheets, so that the Team screen in Settings gets it too.
        .environment(\.showTeamChat) { chat in showTeamChat(chat) }
        .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
        .onChange(of: store.lastSync) { _, _ in openNotification() }
        .onChange(of: store.connection) { _, _ in openNotification() }
        .onAppear { openNotification() }
        // VoiceOver reads a new notice when it shows, as on the Mac.
        .announcesErrors(store.error)
        // Outside each sheet, so that each sheet and each page gets it.
        .errorSheetRoot()
    }

    private func root(tab: HomeTab, path: Binding<NavigationPath>) -> some View {
        NavigationStack(path: path) {
            Group {
                switch tab {
                case .home: HomeView { open($0) }
                case .bots: HomeLaunchView()
                case .library: LibraryView()
                case .business: BusinessHomeView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { rootToolbar(create: tab == .bots) }
                .topBanners { area in
                    RootBanners(area: area) {
                        onboardingFirstRun = false
                        onboardingOpen = true
                    }
                }
                .navigationDestination(for: HomeDestination.self) { destination in
                    switch destination {
                    case .inbox:
                        // The cards that wait, with the card of the row or of the notification first.
                        DecisionInboxView(focusDecisionId: $notificationDecisionId,
                                          refreshingDecisionId: PushNotifications.shared.target?.decisionId)
                            .navigationBarTitleDisplayMode(.inline)
                            .onAppear { cardsShown = true }
                            .onDisappear { cardsShown = false }
                    }
                }
                .navigationDestination(for: ConversationRoute.self) { route in
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId, focusMessageId: route.messageId, attachedWork: route.workContext)
                }
                .navigationDestination(for: BotExchangeRoute.self) { route in
                    BotExchangeView(route: route)
                }
                .navigationDestination(for: ArchivedAgentsRoute.self) { _ in ArchivedAgentsView() }
                .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
                .navigationDestination(for: BotSettingsRoute.self) { route in
                    BotSettingsView(botId: route.botId)
                }
                .navigationDestination(for: DocumentRoute.self) { route in
                    DocumentView(documentId: route.id)
                }
                .navigationDestination(for: TeamChatRoute.self) { route in
                    TeamChatDestination(route: route)
                }
                .navigationDestination(for: TeamScreenRoute.self) { route in
                    TeamRoomView(roomId: route.roomId, pushed: true)
                }
                .navigationDestination(for: NoticeBoardRoute.self) { NoticeBoardView(roomId: $0.roomId) }
                .background(BoardStyle.paper)
        }
        // On the stack itself, so that the pushed pages get it too.
        .environment(\.pushRoute) { route in path.wrappedValue.append(route) }
        .onPreferenceChange(ConversationFooterPreference.self) { allowed in
            conversationFooters[tab] = allowed
        }
    }

    @ToolbarContentBuilder
    private func rootToolbar(create: Bool) -> some ToolbarContent {
        // Coconut has no settings screen: the Coconut chat at the top of Chats changes it.
        if create {
            ToolbarItem(placement: .topBarTrailing) {
                Button { createOpen = true } label: {
                    Image(systemName: "plus")
                }.accessibilityLabel("Create agent").accessibilityIdentifier("create-bot")
            }
        }
    }

    /// Shows the join sheet of a tapped invite link. An open sheet closes first, because a second sheet cannot show
    /// over it.
    private func showTeamJoin() {
        guard TeamJoinGate.canShow(store.teamJoin, showing: teamJoin?.id,
                                   pairingOpen: onboardingOpen || PairingPages.shared.isOpen),
              let request = store.teamJoin else { return }
        let sheetOpen = settingsOpen || createOpen || teamJoin != nil
        settingsOpen = false
        createOpen = false
        teamJoin = nil
        Task {
            if sheetOpen { try? await Task.sleep(for: .milliseconds(450)) }
            guard store.teamJoin?.id == request.id else { return }
            shownTeamJoin = request.id
            teamJoin = request
        }
    }

    private func openNotification() {
        guard let target = PushNotifications.shared.target else { return }
        guard store.acceptsNotification(target) else {
            PushNotifications.shared.target = nil
            return
        }
        if target.team == nil && (target.group || target.decisionId == nil) {
            PushNotifications.shared.target = nil
            previewedNotification = nil
            showNotification(target.group ? .inboxList : .conversation(target.conversationRoute))
            return
        }
        // A card and a team show their place at once. The Mac then confirms it: a card can be answered already, and
        // the person can have left the team.
        if previewedNotification != target {
            previewedNotification = target
            let preview = store.notificationPreview(target)
            previewedPlace = preview
            showNotification(preview)
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
            // The chat of a team that the preview opened stays as it is: a second push would animate again and lose
            // the scroll position. A card shows again, so its Inbox focuses the fresh card.
            if target.team == nil || destination != previewedPlace { showNotification(destination) }
            previewedPlace = nil
        }
    }

    /// Shows a Team chat or a direct chat on the Chats tab, with the Chats list under it. Settings and the sheets close.
    private func showTeamChat(_ chat: TeamChatKey) {
        settingsOpen = false; createOpen = false
        notificationDecisionId = nil
        selectedTab = .bots
        botsPath = NavigationPath()
        botsPath.append(TeamChatRoute(chat: chat))
    }

    /// Shows the cards that wait on the Home tab, with Home under them. The card with this ID comes first.
    private func showCards(_ decisionId: String?) {
        notificationDecisionId = decisionId
        selectedTab = .home
        homePath = NavigationPath([HomeDestination.inbox])
    }

    /// Opens the place of a row of Home on the stack of Home, so Back leads to Home.
    private func open(_ target: HomeTarget) {
        switch target {
        case .conversation(let route): homePath.append(route)
        case .teamChat(let chat): homePath.append(TeamChatRoute(chat: chat))
        case .cards(let decisionId): showCards(decisionId)
        case .agent(let botId): homePath.append(BotSettingsRoute(botId: botId))
        case .agents: selectedTab = .bots
        case .connection: Task { await store.reconnect() }
        case .notices(let room): homePath.append(NoticeBoardRoute(roomId: room))
        }
    }

    private func showNotification(_ destination: NotificationDestination) {
        settingsOpen = false; createOpen = false
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        switch destination {
        case .inbox(let decisionId):
            showCards(decisionId)
        case .inboxList:
            showCards(nil)
        case .conversation(let route):
            notificationDecisionId = nil
            selectedTab = .bots
            botsPath = NavigationPath()
            botsPath.append(route)
        case .team, .chat:
            if let chat = destination.teamChat { showTeamChat(chat) }
        case .chatList:
            notificationDecisionId = nil
            selectedTab = .bots
            botsPath = NavigationPath()
        }
    }

}

/// The banners at the top of a tab root: the connection to the Mac, and the error of an action. A struct of its own,
/// so that it reads the error where it stands, inside the order of the sheets (ShownError).
private struct RootBanners: View {
    @Environment(AppStore.self) private var store
    @ShownError private var error
    let area: CGFloat?
    let pair: () -> Void

    var body: some View {
        let notice = store.isDemo ? nil : store.connection.notice
        if notice != nil || error != nil {
            VStack(spacing: 8) {
                if let notice { ConnectionBanner(notice: notice, pair: pair) }
                if let error {
                    // The page less the padding under the column.
                    ErrorBanner(message: error, area: area.map { $0 - 8 }) { store.error = nil }
                }
            }
            .padding(.horizontal, 16).padding(.bottom, 8)
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
