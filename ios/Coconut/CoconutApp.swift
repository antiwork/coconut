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
                // The share extension lists the chats that the app wrote for it.
                .modifier(ShareDirectoryPublisher())
                .environment(store)
                // A new store holds the demo or the paired Mac, and the home starts again on it.
                .id(ObjectIdentifier(store))
                .environment(\.setDemo) { on in
                    UserDefaults.standard.set(on, forKey: AppStore.demoKey)
                    store = AppStore(demo: on)
                    store.start()
                }
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

/// The places of the tab bar of the iPhone. Home has the chats under what needs attention, and the page of an agent opens
/// from the project menu of Home. The Mac sidebar has its own list.
enum HomeTab: Hashable { case home, business, profile }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var creatingAgent = false
    @State private var selectedTab: HomeTab = .home
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    /// The place that the preview of `previewedNotification` showed.
    @State private var previewedPlace: NotificationDestination?
    @State private var homePath = NavigationPath()
    @State private var cardsShown = false
    @State private var businessPath = NavigationPath()
    @State private var profilePath = NavigationPath()
    @State private var keyboardVisible = false
    @State private var conversationFooters: [HomeTab: Bool] = [:]
    @State private var teamJoin: TeamJoinRequest?
    @State private var shownTeamJoin: UUID?

    /// What the conversation on top of the open stack says: nil when the page on top is not a conversation. A root has
    /// no conversation, so a value that a closed conversation left behind never counts there.
    private var conversationCanMinimize: Bool? { atRoot ? nil : conversationFooters[selectedTab] }

    /// The cards that wait show alone on the stack of Home. They keep the tab bar, as a tab root does.
    private var cardsOnTop: Bool { selectedTab == .home && cardsShown && homePath.count == 1 }

    private var activePath: NavigationPath {
        switch selectedTab {
        case .home: homePath
        case .business: businessPath
        case .profile: profilePath
        }
    }

    /// A tab root, with nothing pushed on its stack.
    private var atRoot: Bool { activePath.isEmpty }

    /// The tab bar always shows on a tab root and under the cards. A page pushed on a stack hides it, unless that page
    /// is a conversation, which keeps it. The keyboard hides it on a pushed page only: a keyboard state that was left
    /// behind must never take the bar from a root.
    private var tabBarVisibility: Visibility {
        if atRoot || cardsOnTop { return .automatic }
        return keyboardVisible || conversationCanMinimize == nil ? .hidden : .automatic
    }

    /// The bar collapses to the icon of the open tab only while the person scrolls a conversation that is pushed. A
    /// root never keeps a collapsed bar.
    private var barMayCollapse: Bool { conversationCanMinimize == true && !keyboardVisible }

    var body: some View {
        // The tab bar shows filled symbols, as the system tab bars do.
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.home) {
                root(tab: .home, path: $homePath)
            } label: {
                Label { Text("Home") } icon: {
                    Image(systemName: "house.fill")
                }
            }.badge(HomeFeed.badge(store.homeUnread) + store.shownTeamRooms.reduce(0) { $0 + $1.unseenNotices })
                .accessibilityIdentifier("home-home")
            Tab(value: HomeTab.business) {
                root(tab: .business, path: $businessPath)
            } label: {
                Label { Text("Business") } icon: {
                    Image(systemName: "chart.xyaxis.line")
                }
            }.accessibilityIdentifier("home-business")
            Tab(value: HomeTab.profile) {
                root(tab: .profile, path: $profilePath)
            } label: {
                Label { Text("Profile") } icon: {
                    Image(systemName: "person.crop.circle.fill")
                }
            }.accessibilityIdentifier("home-profile")
        }
        .modifier(NativeTabCollapse(enabled: barMayCollapse))
        // A document card in a chat opens the page on the stack of Home over the page of its agent, so Back leads to the
        // agent that keeps it.
        .environment(\.openDocument) { id in
            selectedTab = .home
            for route in store.documentPath(id) { homePath.append(route) }
        }
        // A long press on a chat shows the deck of all chats (ChatDeck).
        .modifier(ChatDeckPresenter())
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
        .onChange(of: selectedTab) { _, tab in
            if previewedNotification?.decisionId != nil && tab != .home {
                PushNotifications.shared.target = nil
                previewedNotification = nil
            }
        }
        .fullScreenCover(isPresented: $onboardingOpen) { OnboardingView(firstRun: onboardingFirstRun) }
        .onAppear { if OnboardingView.showsOnLaunch(store) { onboardingFirstRun = true; onboardingOpen = true } }
        .onChange(of: store.setupLink, initial: true) { _, link in
            // A tapped pairing link opens the pairing screen. A pairing screen that shows, also one from Settings, takes the link itself.
            guard link != nil, !onboardingOpen, !OnboardingView.isOpen else { return }
            onboardingFirstRun = false
            onboardingOpen = true
        }
        .sheet(item: $teamJoin, onDismiss: {
            // A newer link can wait for this sheet to close. Only the link of this sheet goes.
            if store.teamJoin?.id == shownTeamJoin { store.teamJoin = nil }
        }) { TeamJoinView(link: $0.link).sheetShowsError() }
        .onChange(of: store.teamJoin, initial: true) { _, _ in showTeamJoin() }
        // A link that came while a pairing page showed opens its sheet after the page, also after a new pairing. The
        // page of Settings has its own state, so the count of the pages decides.
        .onChange(of: onboardingOpen) { _, _ in showTeamJoin() }
        .onChange(of: PairingPages.shared.isOpen) { _, _ in showTeamJoin() }
        // After the sheets, so that each sheet gets it too.
        .environment(\.showTeamChat) { chat in showTeamChat(chat) }
        .onChange(of: PushNotifications.shared.target) { _, _ in openNotification() }
        .onChange(of: store.lastSync) { _, _ in openNotification() }
        .onChange(of: store.connection) { _, _ in openNotification() }
        .onAppear { openNotification() }
        .onAppear { showPreviewPlace() }
        // VoiceOver reads a new notice when it shows, as on the Mac.
        .announcesErrors(store.error)
        // Outside each sheet, so that each sheet and each page gets it.
        .errorSheetRoot()
    }

    private func root(tab: HomeTab, path: Binding<NavigationPath>) -> some View {
        NavigationStack(path: path) {
            Group {
                switch tab {
                case .home: HomeLaunchView { open($0) }
                case .business: BusinessHomeView()
                case .profile: ProfileView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                // The message box of a new chat sits above the tab bar on Home.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if tab == .home { NewChatBar() }
                }
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { rootToolbar(create: tab == .home, path: path) }
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
                .navigationDestination(for: AgentListRoute.self) { _ in AgentsView() }
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
                .modifier(AgentDestinations())
                .modifier(ProfileDestinations())
                .background(BoardStyle.paper)
        }
        // On the stack itself, so that the pushed pages get it too.
        .environment(\.pushRoute) { route in path.wrappedValue.append(route) }
        .onPreferenceChange(ConversationFooterPreference.self) { allowed in
            conversationFooters[tab] = allowed
        }
        // A conversation that closed can leave its value behind. A stack with nothing pushed forgets it.
        .onChange(of: path.wrappedValue.isEmpty) { _, empty in
            if empty { conversationFooters[tab] = nil }
        }
    }

    @ToolbarContentBuilder
    private func rootToolbar(create: Bool, path: Binding<NavigationPath>) -> some ToolbarContent {
        // Coconut has no settings screen: Profile has the things of this device, and the Coconut chat changes the rest.
        if create {
            ToolbarItem(placement: .topBarTrailing) {
                Button { createAgent(path) } label: {
                    Image(systemName: "plus")
                }
                .disabled(creatingAgent)
                .accessibilityLabel("Create agent").accessibilityIdentifier("create-bot")
            }
        }
    }

    /// Makes an agent with a random letter and opens its chat (AppStore.newAgent).
    private func createAgent(_ path: Binding<NavigationPath>) {
        creatingAgent = true
        Task {
            defer { creatingAgent = false }
            guard let bot = await store.newAgent() else { return }
            path.wrappedValue.append(ConversationRoute(botId: bot.id))
        }
    }

    /// Shows the join sheet of a tapped invite link. An open sheet closes first, because a second sheet cannot show
    /// over it.
    private func showTeamJoin() {
        guard TeamJoinGate.canShow(store.teamJoin, showing: teamJoin?.id,
                                   pairingOpen: onboardingOpen || PairingPages.shared.isOpen),
              let request = store.teamJoin else { return }
        let sheetOpen = teamJoin != nil
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
        // A notice opens the board of its team. The board reads the team from the store, so nothing waits for the Mac.
        if let room = target.noticeRoom {
            PushNotifications.shared.target = nil
            previewedNotification = nil
            showNotification(.notices(room))
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

    /// Shows a Team chat or a direct chat on the stack of Home, with the chat list under it. The sheets close.
    private func showTeamChat(_ chat: TeamChatKey) {
        notificationDecisionId = nil
        selectedTab = .home
        homePath = NavigationPath()
        homePath.append(TeamChatRoute(chat: chat))
    }

    /// Shows the cards that wait on the Home tab, with Home under them. The card with this ID comes first.
    private func showCards(_ decisionId: String?) {
        // A card opens in the chat of its agent, where its work is. Only "Review cards", or a card that the app does
        // not have yet, opens the list of cards.
        if let decisionId, let decision = store.decisions.first(where: { $0.id == decisionId }) {
            notificationDecisionId = nil
            selectedTab = .home
            homePath = NavigationPath()
            homePath.append(store.decisionChatRoute(for: decision))
            return
        }
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
        case .agent(let botId): homePath.append(AgentRoute(botId: botId))
        case .agents: homePath.append(AgentListRoute())
        case .connection:
            profilePath = NavigationPath([ProfileRoute.mac])
            selectedTab = .profile
        case .notices(let room): homePath.append(NoticeBoardRoute(roomId: room))
        }
    }

    private func showNotification(_ destination: NotificationDestination) {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        switch destination {
        case .inbox(let decisionId):
            showCards(decisionId)
        case .inboxList:
            showCards(nil)
        case .conversation(let route):
            notificationDecisionId = nil
            selectedTab = .home
            homePath = NavigationPath()
            homePath.append(route)
        case .team, .chat:
            if let chat = destination.teamChat { showTeamChat(chat) }
        case .chatList:
            notificationDecisionId = nil
            selectedTab = .home
            homePath = NavigationPath()
        case .notices(let room):
            // The board of a team is part of the stack of Home, so Back leads to the chats.
            notificationDecisionId = nil
            selectedTab = .home
            homePath = NavigationPath()
            homePath.append(NoticeBoardRoute(roomId: room))
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

#if DEBUG && targetEnvironment(simulator)
// The names of the tabs, so `--open profile` finds a tab by its name and stays right when the tabs change.
extension HomeTab: CaseIterable {}
#endif

extension CoconutHome {
    /// Shows one place of the app at launch, for a screenshot of the preview data: `--demo --open agent`. A simulator
    /// takes no taps from a terminal, so the hand check of a screen starts the app on it. The name of a tab shows
    /// its root. Each other place opens on the stack of Home. Only a debug build on a simulator reads the argument.
    private func showPreviewPlace() {
        #if DEBUG && targetEnvironment(simulator)
        let arguments = ProcessInfo.processInfo.arguments
        guard store.isDemo, let index = arguments.firstIndex(of: "--open"), index + 1 < arguments.count else { return }
        let place = arguments[index + 1]
        if let tab = HomeTab.allCases.first(where: { String(describing: $0) == place }) {
            selectedTab = tab
            return
        }
        let agent = store.bots.agents.first { !$0.archived }?.id ?? ""
        let room = store.shownTeamRooms.first?.room ?? ""
        func show(_ tab: HomeTab, _ routes: [any Hashable] = []) {
            var path = NavigationPath()
            for route in routes { path.append(route) }
            switch tab {
            case .home: homePath = path
            case .business: businessPath = path
            case .profile: profilePath = path
            }
            selectedTab = tab
        }
        switch place {
        // Chats and agents are not tabs now: the chat list is Home, and an agent opens from its project menu.
        case "chats": show(.home)
        case "agents": show(.home, [AgentListRoute()])
        case "cards": show(.home, [HomeDestination.inbox])
        case "chat": show(.home, [ConversationRoute(botId: agent)])
        case "coconut": show(.home, [ConversationRoute(botId: store.bots.coconutChat?.id ?? agent)])
        case "archived": show(.home, [ArchivedAgentsRoute()])
        case "team-chat": show(.home, [TeamChatRoute(chat: TeamChatKey(room: room))])
        case "agent": show(.home, [AgentRoute(botId: agent)])
        case "agent-settings": show(.home, [AgentRoute(botId: agent), BotSettingsRoute(botId: agent)])
        case "agent-model": show(.home, [AgentRoute(botId: agent), AgentSettingRoute.model(agent)])
        case "agent-access": show(.home, [AgentRoute(botId: agent), AgentSettingRoute.access(agent)])
        case "all-pages": show(.home, [AllPagesRoute()])
        case "usage": show(.home, [UsageRoute()])
        case "business": show(.business, [])
        case "projects": show(.business, [BusinessRoute.projects])
        case "accounting": show(.business, [BusinessRoute.function("accounting")])
        case "support-work": show(.business, [BusinessRoute.function("support")])
        case "reports": show(.business, [BusinessRoute.reports])
        case "history": show(.business, [BusinessRoute.reports])
        case "page":
            // The list of the pages loads first. The page then opens over the page of its agent.
            Task {
                if store.documents == nil { await store.loadDocuments(quiet: true) }
                guard let page = store.documents?.first else { return }
                show(.home, store.documentPath(page.id))
            }
        case "notices": show(.home, [NoticeBoardRoute(roomId: room)])
        case "team": show(.profile, [ProfileRoute.team])
        case "direct":
            let member = store.shownTeamRooms.first?.directList.first?.member
            show(.home, [TeamChatRoute(chat: TeamChatKey(room: room, member: member))])
        // The tap on the notification of a notice.
        case "notice-tap": showNotification(.notices(room))
        case "profile-mac": show(.profile, [ProfileRoute.mac])
        case "profile-notifications": show(.profile, [ProfileRoute.notifications])
        case "profile-connections": show(.profile, [ProfileRoute.connections])
        case "profile-model": show(.profile, [ProfileRoute.model])
        case "profile-access": show(.profile, [ProfileRoute.access])
        case "profile-parallel": show(.profile, [ProfileRoute.parallelWork])
        case "profile-team": show(.profile, [ProfileRoute.team])
        default: break
        }
        #endif
    }
}
