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

/// The five tabs of the iPhone in their order: Finance, Support, To-dos, Chats and You. Chats (the case `home`) has
/// every chat and the message box under the list. The page of a project opens from the name of its chat, and the list
/// of all projects from the project chip of the box. The Mac sidebar has its own list.
enum HomeTab: Hashable { case business, support, todos, home, profile }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var selectedTab: HomeTab = .home
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    /// The place that the preview of `previewedNotification` showed.
    @State private var previewedPlace: NotificationDestination?
    @State private var homePath = NavigationPath()
    @State private var cardsShown = false
    @State private var businessPath = NavigationPath()
    @State private var supportPath = NavigationPath()
    @State private var todosPath = NavigationPath()
    @State private var profilePath = NavigationPath()
    @State private var teamJoin: TeamJoinRequest?
    @State private var shownTeamJoin: UUID?

    /// The cards that wait show alone on the stack of To-dos. They keep the tab bar, as a tab root does.
    private var cardsOnTop: Bool { selectedTab == .todos && cardsShown && todosPath.count == 1 }

    private var activePath: NavigationPath {
        switch selectedTab {
        case .home: homePath
        case .business: businessPath
        case .support: supportPath
        case .todos: todosPath
        case .profile: profilePath
        }
    }

    /// The tab bar shows on a tab root and under the cards only. A pushed page hides it, so an open chat has its
    /// message box, with the model in it, at the bottom of the screen and no tab bar under it.
    private var tabBarVisibility: Visibility { activePath.isEmpty || cardsOnTop ? .automatic : .hidden }

    var body: some View {
        // The tab bar shows filled symbols, as the system tab bars do.
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.business) {
                root(tab: .business, path: $businessPath)
            } label: {
                Label { Text("Finance") } icon: { Image(systemName: "chart.xyaxis.line") }
            }.accessibilityIdentifier("home-business")
            Tab(value: HomeTab.support) {
                root(tab: .support, path: $supportPath)
            } label: {
                Label { Text("Support") } icon: { Image(systemName: "lifepreserver.fill") }
            }.accessibilityIdentifier("home-support")
            Tab(value: HomeTab.todos) {
                root(tab: .todos, path: $todosPath)
            } label: {
                Label { Text("To-dos") } icon: { Image(systemName: "checklist") }
            }.badge(store.pendingCardCount)
                .accessibilityIdentifier("home-todos")
            Tab(value: HomeTab.home) {
                root(tab: .home, path: $homePath)
            } label: {
                Label { Text("Chats") } icon: { Image(systemName: "bubble.left.and.bubble.right.fill") }
            // The cards that wait count on To-dos, so this badge has the unread chats and notices only.
            }.badge(HomeFeed.badge(HomeFeed.unread(bots: store.bots, latest: store.chatSummaries.newestActivity, rooms: store.shownTeamRooms,
                                                   local: store.newestLocalMessages, decisions: [], answering: []))
                    + store.shownTeamRooms.reduce(0) { $0 + $1.unseenNotices })
                .accessibilityIdentifier("home-home")
            Tab(value: HomeTab.profile) {
                root(tab: .profile, path: $profilePath)
            } label: {
                Label { Text("You") } icon: { Image(systemName: "person.crop.circle.fill") }
            }.accessibilityIdentifier("home-profile")
        }
        // A document card in a chat opens the page on the stack of Home over the page of its agent, so Back leads to the
        // agent that keeps it.
        .environment(\.openDocument) { id in
            selectedTab = .home
            for route in store.documentPath(id) { homePath.append(route) }
        }
        // A long press on a chat shows the deck of all chats (ChatDeck).
        .modifier(ChatDeckPresenter())
        // A New chat, from a page or from the deck, shows Chats with the message box. The box takes the project.
        .onChange(of: store.chatStarts) { _, _ in
            selectedTab = .home
            homePath = NavigationPath()
        }
        .onChange(of: selectedTab) { _, tab in
            if previewedNotification?.decisionId != nil && tab != .home && tab != .todos {
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
                case .home: HomeLaunchView { open($0, on: path) }
                case .todos: HomeLaunchView(part: .todos) { open($0, on: path) }
                case .business: BusinessHomeView()
                case .support: BusinessReportView(key: "support")
                case .profile: ProfileView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                // The message box sits above the tab bar on Home. Every New chat leads to it.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if tab == .home { NewChatBar() }
                }
                .navigationTitle("")
                .navigationBarTitleDisplayMode(.inline)
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

    /// Shows the cards that wait on the To-dos tab, with the To-dos under them. The card with this ID comes first.
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
        selectedTab = .todos
        todosPath = NavigationPath([HomeDestination.inbox])
    }

    /// Opens the place of a row of Chats or To-dos on the stack of its tab, so Back leads to that tab.
    private func open(_ target: HomeTarget, on path: Binding<NavigationPath>) {
        switch target {
        case .conversation(let route): path.wrappedValue.append(route)
        case .teamChat(let chat): path.wrappedValue.append(TeamChatRoute(chat: chat))
        case .cards(let decisionId): showCards(decisionId)
        case .agent(let botId): path.wrappedValue.append(AgentRoute(botId: botId))
        case .agents: path.wrappedValue.append(AgentListRoute())
        case .connection:
            profilePath = NavigationPath([ProfileRoute.mac])
            selectedTab = .profile
        case .notices(let room): path.wrappedValue.append(NoticeBoardRoute(roomId: room))
        case .business(let route): path.wrappedValue.append(route)
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
        /// Runs the action after the first screen shows, so the screenshot sees what a tap would do.
        func later(_ action: @escaping @MainActor () async -> Void) {
            Task {
                try? await Task.sleep(for: .seconds(1.5))
                await action()
            }
        }
        func show(_ tab: HomeTab, _ routes: [any Hashable] = []) {
            var path = NavigationPath()
            for route in routes { path.append(route) }
            switch tab {
            case .home: homePath = path
            case .business: businessPath = path
            case .support: supportPath = path
            case .todos: todosPath = path
            case .profile: profilePath = path
            }
            selectedTab = tab
        }
        switch place {
        // The chat list is the Chats tab, and a project opens from the name of its chat.
        case "chats": show(.home)
        // Each way to start a chat, run as the person would: the message box of Home shows with the project chosen.
        case "new-chat": show(.home); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "agent-new-chat": show(.home, [AgentRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "chat-new-chat": show(.home, [ConversationRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "new-project": show(.home); later { await store.newProject() }
        // The first message of the box, sent as its Send does: in a new chat of the project, or in a new project.
        case "new-chat-send":
            show(.home)
            later {
                guard let bot = store.bots.first(where: { $0.id == agent }),
                      let chat = await store.sendNewChat(in: bot, text: "Which sellers had a payout held on Friday?") else { return }
                homePath.append(ConversationRoute(botId: chat.id))
            }
        case "new-project-send":
            show(.home)
            later {
                guard let project = await store.newProject() else { return }
                // The person types for a moment before Send.
                try? await Task.sleep(for: .seconds(1.5))
                guard let chat = await store.sendNewChat(in: project, text: "Draft the weekly payout summary for Gumroad.") else { return }
                homePath.append(ConversationRoute(botId: chat.id))
            }
        case "agents": show(.home, [AgentListRoute()])
        case "cards": show(.todos, [HomeDestination.inbox])
        case "chat": show(.home, [ConversationRoute(botId: agent)])
        case "coconut": show(.home, [ConversationRoute(botId: store.bots.coconutChat?.id ?? agent)])
        case "archived": show(.home, [ArchivedAgentsRoute()])
        case "team-chat": show(.home, [TeamChatRoute(chat: TeamChatKey(room: room))])
        case "agent": show(.home, [AgentRoute(botId: agent)])
        case "agent-settings": show(.home, [AgentRoute(botId: agent), BotSettingsRoute(botId: agent)])
        case "all-pages": show(.home, [AllPagesRoute()])
        case "usage": show(.home, [UsageRoute()])
        case "live": show(.home, [LiveRoute()])
        case "spend": show(.home, [SpendRoute()])
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
        case "profile-team": show(.profile, [ProfileRoute.team])
        default: break
        }
        #endif
    }
}
