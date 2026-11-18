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
                // Controls take the coconut brown of the app, never black (BoardStyle.control).
                .tint(BoardStyle.control)
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

/// The five tabs of the iPhone in their order: Inbox, Chats, Agents, Dashboards and You. Inbox has what asks for the
/// person, and its badge counts each thing that it lists (TodosCount). Chats is one list of the chats with people: the
/// teams, their named chats and the direct chats. Agents is the list of the chats of the projects in two tabs, Working
/// and Idle, with the message box under the list. The page of a project opens from the name of its chat, and the list
/// of all projects from the project chip of the box. Dashboards has Finance, which opens Support and the other
/// functions. The app opens on Agents, because the person does not want to land in the inbox. The Mac sidebar has its
/// own list.
///
/// The tab bar always has exactly five tabs. With a sixth, iOS puts the last tabs behind a More tab, and the person
/// does not want More. A new place goes on the page of a tab, or it takes the place of a tab.
enum HomeTab: Hashable { case inbox, chats, agents, dashboards, profile }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var selectedTab: HomeTab = .agents
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    /// The place that the preview of `previewedNotification` showed.
    @State private var previewedPlace: NotificationDestination?
    @State private var inboxPath = NavigationPath()
    @State private var cardsShown = false
    @State private var chatsPath = NavigationPath()
    @State private var agentsPath = NavigationPath()
    @State private var dashboardsPath = NavigationPath()
    @State private var profilePath = NavigationPath()
    @State private var teamJoin: TeamJoinRequest?
    @State private var shownTeamJoin: UUID?

    /// The cards that wait show alone on the stack of Inbox. They keep the tab bar, as a tab root does.
    private var cardsOnTop: Bool { selectedTab == .inbox && cardsShown && inboxPath.count == 1 }

    private var activePath: NavigationPath {
        switch selectedTab {
        case .inbox: inboxPath
        case .chats: chatsPath
        case .agents: agentsPath
        case .dashboards: dashboardsPath
        case .profile: profilePath
        }
    }

    /// Shows `tab` with `path` on its stack.
    private func show(_ tab: HomeTab, path: NavigationPath = NavigationPath()) {
        switch tab {
        case .inbox: inboxPath = path
        case .chats: chatsPath = path
        case .agents: agentsPath = path
        case .dashboards: dashboardsPath = path
        case .profile: profilePath = path
        }
        selectedTab = tab
    }

    /// The tab bar shows on a tab root and under the cards only. A pushed page hides it, so an open chat has its
    /// message box, with the model in it, at the bottom of the screen and no tab bar under it.
    private var tabBarVisibility: Visibility { activePath.isEmpty || cardsOnTop ? .automatic : .hidden }

    var body: some View {
        // The tab bar shows filled symbols, as the system tab bars do.
        TabView(selection: $selectedTab) {
            Tab(value: HomeTab.inbox) {
                root(tab: .inbox, path: $inboxPath)
            } label: {
                Label { Text("Inbox") } icon: { Image(systemName: "tray.fill") }
            }.badge(store.todosCount())
                .accessibilityIdentifier("home-inbox")
            Tab(value: HomeTab.chats) {
                root(tab: .chats, path: $chatsPath)
            } label: {
                Label { Text("Chats") } icon: { Image(systemName: "bubble.left.and.bubble.right.fill") }
            }.badge(HomeFeed.chatsBadge(rooms: store.shownTeamRooms, local: store.newestLocalMessages))
                .accessibilityIdentifier("home-chats")
            Tab(value: HomeTab.agents) {
                root(tab: .agents, path: $agentsPath)
            } label: {
                Label { Text("Agents") } icon: { Image(systemName: "square.stack.3d.up.fill") }
            // The cards that wait count on Inbox, so this badge has the unread chats only.
            }.badge(HomeFeed.agentsBadge(bots: store.bots, rooms: store.shownTeamRooms, local: store.newestLocalMessages))
                .accessibilityIdentifier("home-agents")
            Tab(value: HomeTab.dashboards) {
                root(tab: .dashboards, path: $dashboardsPath)
            } label: {
                Label { Text("Dashboards") } icon: { Image(systemName: "gauge.with.needle.fill") }
            }.accessibilityIdentifier("home-dashboards")
            Tab(value: HomeTab.profile) {
                root(tab: .profile, path: $profilePath)
            } label: {
                Label { Text("You") } icon: { Image(systemName: "person.crop.circle.fill") }
            }.accessibilityIdentifier("home-profile")
        }
        // The count of Inbox has the breaches of Finance (TodosCount), so the overview loads here for every tab, not
        // only while Inbox shows: at each connection, then every 5 minutes. The publisher writes it every 15 minutes,
        // and a report older than an hour proves no breach, so an overview that loaded once dropped from the count.
        .task(id: "\(store.connection)/\(store.businessChanges)") {
            guard store.connection == .connected || store.isDemo else { return }
            while !Task.isCancelled {
                await store.loadBusinessAttention()
                try? await Task.sleep(for: .seconds(300))
            }
        }
        // A document card in a chat opens the page on the stack of Agents over the page of its agent, so Back leads
        // to the agent that keeps it.
        .environment(\.openDocument) { id in
            selectedTab = .agents
            for route in store.documentPath(id) { agentsPath.append(route) }
        }
        // A long press on a chat shows the deck of all chats (ChatDeck).
        .modifier(ChatDeckPresenter())
        // A New chat, from a page or from the deck, shows Agents with the message box. The box takes the project.
        .onChange(of: store.chatStarts) { _, _ in show(.agents) }
        .onChange(of: selectedTab) { _, tab in
            // The preview of a card shows in a chat or on Inbox. Another place leaves it.
            if previewedNotification?.decisionId != nil && (tab == .dashboards || tab == .profile) {
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
                case .inbox: HomeLaunchView(part: .inbox) { open($0, on: path) }
                case .chats: HomeLaunchView(part: .chats) { open($0, on: path) }
                case .agents: HomeLaunchView(part: .agents) { open($0, on: path) }
                case .dashboards: BusinessHomeView(title: "Dashboards")
                case .profile: ProfileView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                // The message box sits above the tab bar on Agents. Every New chat leads to it.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if tab == .agents { NewChatBar() }
                }
                .navigationTitle("")
                // A tab root has its name as a large title, so a root and a pushed page look different. You has the
                // person as its heading.
                .navigationBarTitleDisplayMode(tab == .profile ? .inline : .large)
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
                        .inlineNavigationTitle()
                }
                .navigationDestination(for: BotExchangeRoute.self) { route in
                    BotExchangeView(route: route).inlineNavigationTitle()
                }
                .navigationDestination(for: ArchivedAgentsRoute.self) { _ in ArchivedAgentsView().inlineNavigationTitle() }
                .navigationDestination(for: AgentListRoute.self) { _ in AgentsView() }
                .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0).inlineNavigationTitle() }
                .navigationDestination(for: BotSettingsRoute.self) { route in
                    BotSettingsView(botId: route.botId)
                }
                .navigationDestination(for: DocumentRoute.self) { route in
                    DocumentView(documentId: route.id).inlineNavigationTitle()
                }
                .navigationDestination(for: TeamChatRoute.self) { route in
                    TeamChatDestination(route: route).inlineNavigationTitle()
                }
                .navigationDestination(for: TeamScreenRoute.self) { route in
                    TeamRoomView(roomId: route.roomId, pushed: true).inlineNavigationTitle()
                }
                .navigationDestination(for: NoticeBoardRoute.self) { NoticeBoardView(roomId: $0.roomId).inlineNavigationTitle() }
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

    /// Shows a Team chat or a direct chat on the stack of the tab that lists it, with that list under it: Chats, or
    /// Agents for a direct chat with a bot of a team. The sheets close.
    private func showTeamChat(_ chat: TeamChatKey) {
        notificationDecisionId = nil
        show(chat.bot == nil ? .chats : .agents, path: NavigationPath([TeamChatRoute(chat: chat)]))
    }

    /// Shows the cards that wait on Inbox, with the rest of Inbox under them. The card with this ID comes first.
    private func showCards(_ decisionId: String?) {
        // A card opens in the chat of its agent, where its work is. Only "Review cards", or a card that the app does
        // not have yet, opens the list of cards.
        if let decisionId, let decision = store.decisions.first(where: { $0.id == decisionId }) {
            notificationDecisionId = nil
            show(.agents, path: NavigationPath([store.decisionChatRoute(for: decision)]))
            return
        }
        notificationDecisionId = decisionId
        show(.inbox, path: NavigationPath([HomeDestination.inbox]))
    }

    /// Opens the place of a row of Inbox, Chats or Agents on the stack of its tab, so Back leads to that tab.
    private func open(_ target: HomeTarget, on path: Binding<NavigationPath>) {
        switch target {
        case .conversation(let route): path.wrappedValue.append(route)
        case .teamChat(let chat): path.wrappedValue.append(TeamChatRoute(chat: chat))
        case .cards(let decisionId): showCards(decisionId)
        case .agent(let botId): path.wrappedValue.append(AgentRoute(botId: botId))
        case .agents: path.wrappedValue.append(AgentListRoute())
        case .connection: show(.profile, path: NavigationPath([ProfileRoute.mac]))
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
            show(.agents, path: NavigationPath([route]))
        case .team, .chat:
            if let chat = destination.teamChat { showTeamChat(chat) }
        case .chatList:
            notificationDecisionId = nil
            show(.chats)
        case .notices(let room):
            // The board of a team is part of the stack of Chats, so Back leads to the chats with people.
            notificationDecisionId = nil
            show(.chats, path: NavigationPath([NoticeBoardRoute(roomId: room)]))
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
    /// its root. Each other place opens on the stack of the tab that lists it. Only a debug build on a simulator reads
    /// the argument.
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
        func open(_ tab: HomeTab, _ routes: [any Hashable] = []) {
            var path = NavigationPath()
            for route in routes { path.append(route) }
            show(tab, path: path)
        }
        switch place {
        // The chats of the projects are the Agents tab, and a project opens from the name of its chat.
        // Each way to start a chat, run as the person would: the message box of Agents shows with the project chosen.
        case "new-chat": open(.agents); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "agent-new-chat": open(.agents, [AgentRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "chat-new-chat": open(.agents, [ConversationRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        // The first message of the box, sent as its Send does: in a new chat of the project.
        case "new-chat-send":
            open(.agents)
            later {
                guard let bot = store.bots.first(where: { $0.id == agent }),
                      let chat = await store.sendNewChat(in: bot, text: "Which sellers had a payout held on Friday?") else { return }
                agentsPath.append(ConversationRoute(botId: chat.id))
            }
        // Quick to-dos: the sheet of a new one, a to-do added as Add does, and the comments of one.
        case "new-todo": open(.inbox); later { PendingTodos.shared.adding = true }
        case "todo-add", "todo-comments":
            open(.inbox)
            later {
                let choices = QuickTodos.choices(in: store.bots, latest: store.chatSummaries.newestActivity)
                guard let bot = QuickTodos.defaultAgent(last: UserDefaults.standard.string(forKey: QuickTodos.agentKey) ?? "", in: choices),
                      await store.addTodo("Find the three refunds over $500 from last week", agent: bot),
                      place == "todo-comments", let chat = QuickTodos.list(store).first else { return }
                await store.commentOnTodo(chat, text: "Only the ones in USD, please.")
                PendingTodos.shared.commenting = chat.id
            }
        case "agent-list": open(.agents, [AgentListRoute()])
        case "cards": open(.inbox, [HomeDestination.inbox])
        case "chat": open(.agents, [ConversationRoute(botId: agent)])
        // Personal, with the line of its Antiwork budget (PersonalBudget.swift).
        case "chat-personal":
            open(.agents, [ConversationRoute(botId: store.bots.first { $0.name == "Personal" && $0.chatOf == nil }?.id ?? agent)])
        case "coconut": open(.agents, [ConversationRoute(botId: store.bots.coconutChat?.id ?? agent)])
        case "archived": open(.agents, [ArchivedAgentsRoute()])
        case "team-chat": open(.chats, [TeamChatRoute(chat: TeamChatKey(room: room))])
        case "agent": open(.agents, [AgentRoute(botId: agent)])
        case "agent-routines": open(.agents, [AgentRoute(botId: agent), BotSettingsRoute(botId: agent)])
        case "all-pages": open(.agents, [AllPagesRoute()])
        case "usage": open(.agents, [UsageRoute()])
        case "agent-usage": open(.agents, [AgentRoute(botId: agent), UsageRoute(botId: agent)])
        case "live": open(.agents, [LiveRoute()])
        case "spend": open(.agents, [SpendRoute()])
        // The older names of the tabs.
        case "todos": open(.inbox)
        case "business": open(.dashboards)
        case "projects": open(.dashboards, [BusinessRoute.projects])
        case "accounting": open(.dashboards, [BusinessRoute.function("accounting")])
        case "support", "support-work": open(.dashboards, [BusinessRoute.function("support")])
        case "support-conversation":
            open(.dashboards, [BusinessRoute.function("support"),
                               BusinessRoute.supportConversation(id: "sample-0", subject: "Payout timing question")])
        case "reports", "history": open(.dashboards, [BusinessRoute.reports])
        case "marketing": open(.dashboards, [BusinessRoute.function("marketing")])
        case "ledger": open(.dashboards, [BusinessRoute.ledger])
        case "page":
            // The list of the pages loads first. The page then opens over the page of its agent.
            Task {
                if store.documents == nil { await store.loadDocuments(quiet: true) }
                guard let page = store.documents?.first else { return }
                open(.agents, store.documentPath(page.id))
            }
        case "notices": open(.chats, [NoticeBoardRoute(roomId: room)])
        case "team": open(.profile, [ProfileRoute.team])
        case "direct":
            let member = store.shownTeamRooms.first?.directList.first?.member
            open(.chats, [TeamChatRoute(chat: TeamChatKey(room: room, member: member))])
        // The tap on the notification of a notice.
        case "notice-tap": showNotification(.notices(room))
        case "profile-mac": open(.profile, [ProfileRoute.mac])
        case "profile-notifications": open(.profile, [ProfileRoute.notifications])
        case "profile-connections": open(.profile, [ProfileRoute.connections])
        case "profile-team": open(.profile, [ProfileRoute.team])
        default:
            // "report-risk" opens a report by its key, and "project-gumroad" the work of a project.
            if place.hasPrefix("report-") { open(.dashboards, [BusinessRoute.report(String(place.dropFirst(7)))]) }
            if place.hasPrefix("project-") { open(.dashboards, [BusinessRoute.projects, BusinessRoute.project(String(place.dropFirst(8)))]) }
        }
        #endif
    }
}
