import Foundation
import Intents
import SwiftUI

@main
struct CoconutApp: App {
    @UIApplicationDelegateAdaptor(CoconutAppDelegate.self) private var appDelegate
    @State private var store = AppStore()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // The counts of the tab bar take the green of each unread badge (UnreadBadge), not the red of the system.
        let bar = UITabBarAppearance()
        for layout in [bar.stackedLayoutAppearance, bar.inlineLayoutAppearance, bar.compactInlineLayoutAppearance] {
            for state in [layout.normal, layout.selected] {
                state.badgeBackgroundColor = UIColor(BoardStyle.green)
                state.badgeTextAttributes = [.foregroundColor: UIColor(BoardStyle.controlLabel)]
            }
        }
        UITabBar.appearance().standardAppearance = bar
        UITabBar.appearance().scrollEdgeAppearance = bar
    }

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
                // iOS suggests the projects that the person writes to outside the share sheet too, for example in
                // Search (ShareSuggestions). A tap on one opens the message box on that project.
                .onContinueUserActivity(String(describing: INSendMessageIntent.self)) { activity in
                    guard let id = (activity.interaction?.intent as? INSendMessageIntent)?.conversationIdentifier,
                          let project = store.bots.first(where: { "project-" + $0.id == id }) else { return }
                    store.startChat(in: project)
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

/// The five tabs of the iPhone in their order: Finance, Support, Product, Chats and You, as the person listed them.
/// For a day the tabs began with Product and Finance was called Business; the person then put Finance first again.
/// Each tab holds only its own things:
/// - Finance: its to-dos, the issues labelled `finance`, then the overview of the business with the risk reviews,
///   which opens Accounting, Marketing and History.
/// - Support: its to-dos, with the conversations past their service level first, then the queue of the customers.
/// - Product: the rules that breach for the agents and the cards that wait, then its to-dos and the open pull requests.
/// - Chats: every agent and every chat with people in the sections of Slack, one for each project, Personal at the
///   bottom and one row Archived last, which opens the archived chats on a page of their own, with the message box
///   under the list. A folded section shows only the real work (ChatList.sections).
/// - You: the person, this device and the settings: the Mac, notifications, connections, usage, the team and the
///   repositories that Coconut follows.
/// Finance, Support and Product each start with their own list of to-dos, the issues of gumroad-private with their
/// label, and each badge counts its own list (TodoLists.swift, TodoCounts): the person asked for three lists and three
/// counts in place of one. An issue is a to-do of one list, so no other tab lists it again. The page of a project opens
/// from the name of its agent, and the list of all projects from the project chip of the box. The app opens on Chats,
/// because the person does not want to land in a list of work. The Mac sidebar has the same sections of the chats, with
/// Unread after Starred (ChatSections).
///
/// A conversation with the agent of a project is an agent in every label (New agent, Archive agent, the section Other
/// agents of Chats and of the Mac sidebar), because the person sees a group chat as people with an agent or without
/// one. The tab is Chats, because it holds the agents and the chats with people. Code names such as
/// `chats`, `ChatList` and `NewChatBar` stay.
///
/// The tab bar always has exactly five tabs. With a sixth, iOS puts the last tabs behind a More tab, and the person
/// does not want More. A new place goes on the page of a tab, or it takes the place of a tab.
enum HomeTab: Hashable { case finance, support, product, chats, profile }

struct CoconutHome: View {
    @Environment(AppStore.self) private var store
    @State private var onboardingOpen = false
    // The first run hides Close, because Coconut needs a pairing before it can show anything.
    @State private var onboardingFirstRun = false
    @State private var selectedTab: HomeTab = .chats
    @State private var notificationDecisionId: String?
    @State private var openingNotification = false
    @State private var previewedNotification: NotificationTarget?
    /// The place that the preview of `previewedNotification` showed.
    @State private var previewedPlace: NotificationDestination?
    @State private var financePath = NavigationPath()
    @State private var supportPath = NavigationPath()
    @State private var productPath = NavigationPath()
    @State private var cardsShown = false
    @State private var chatsPath = NavigationPath()
    @State private var profilePath = NavigationPath()
    @State private var teamJoin: TeamJoinRequest?
    @State private var shownTeamJoin: UUID?

    /// The cards that wait show alone on the stack of Product. They keep the tab bar, as a tab root does.
    private var cardsOnTop: Bool { selectedTab == .product && cardsShown && productPath.count == 1 }

    private var activePath: NavigationPath {
        switch selectedTab {
        case .finance: financePath
        case .support: supportPath
        case .product: productPath
        case .chats: chatsPath
        case .profile: profilePath
        }
    }

    /// Shows `tab` with `path` on its stack.
    private func show(_ tab: HomeTab, path: NavigationPath = NavigationPath()) {
        switch tab {
        case .finance: financePath = path
        case .support: supportPath = path
        case .product: productPath = path
        case .chats: chatsPath = path
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
            // Each of the three tabs of work counts its own to-dos (TodoCounts).
            Tab(value: HomeTab.finance) {
                root(tab: .finance, path: $financePath)
            } label: {
                Label { Text("Finance") } icon: { Image(systemName: "chart.xyaxis.line") }
            }.badge(store.todoCount(.finance))
                .accessibilityIdentifier("home-finance")
            Tab(value: HomeTab.support) {
                root(tab: .support, path: $supportPath)
            } label: {
                Label { Text("Support") } icon: { Image(systemName: "lifepreserver.fill") }
            }.badge(store.todoCount(.support))
                .accessibilityIdentifier("home-support")
            Tab(value: HomeTab.product) {
                root(tab: .product, path: $productPath)
            } label: {
                Label { Text("Product") } icon: { Image(systemName: "hammer.fill") }
            }.badge(store.todoCount(.product))
                .accessibilityIdentifier("home-product")
            Tab(value: HomeTab.chats) {
                root(tab: .chats, path: $chatsPath)
            } label: {
                Label { Text("Chats") } icon: { Image(systemName: "bubble.left.and.bubble.right.fill") }
            // The cards and the notices that wait show on Product, so this badge has the unread chats only.
            }.badge(HomeFeed.chatsBadge(bots: store.bots, rooms: store.shownTeamRooms, local: store.newestLocalMessages))
                .accessibilityIdentifier("home-chats")
            Tab(value: HomeTab.profile) {
                root(tab: .profile, path: $profilePath)
            } label: {
                Label { Text("You") } icon: { Image(systemName: "person.crop.circle.fill") }
            }.accessibilityIdentifier("home-profile")
        }
        // The count of Support has the breach of Finance's overview (TodoCounts), so the overview loads here for every
        // tab, not only while Support shows: at each connection, then every 5 minutes. The publisher writes it every 15
        // minutes, and a report older than an hour proves no breach, so an overview that loaded once dropped from the count.
        .task(id: "\(store.connection)/\(store.businessChanges)") {
            guard store.connection == .connected || store.isDemo else { return }
            while !Task.isCancelled {
                await store.loadBusinessAttention()
                try? await Task.sleep(for: .seconds(300))
            }
        }
        // The to-dos of the three tabs load here too, for their counts: at each connection and each change, then every
        // 2 minutes, so an issue that someone opens or closes on GitHub shows soon. The phone keeps the last list for its
        // next start (TodoIssues.cache).
        .task(id: "todos/\(store.connection)/\(store.businessChanges)") {
            guard store.connection == .connected || store.isDemo else { return }
            while !Task.isCancelled {
                await store.loadTodoIssues()
                try? await Task.sleep(for: .seconds(120))
            }
        }
        // A document card in a chat opens the page on the stack of Chats over the page of its agent, so Back leads
        // to the agent that keeps it.
        .environment(\.openDocument) { id in
            selectedTab = .chats
            for route in store.documentPath(id) { chatsPath.append(route) }
        }
        // A long press on a chat shows the deck of all chats (ChatDeck).
        .modifier(ChatDeckPresenter())
        // A New agent, from a page or from the deck, shows Chats with the message box. The box takes the project.
        .onChange(of: store.chatStarts) { _, _ in show(.chats) }
        .onChange(of: selectedTab) { _, tab in
            // The preview of a card shows in a chat or on Product. Another place leaves it.
            if previewedNotification?.decisionId != nil && ![HomeTab.chats, .product].contains(tab) {
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
                case .finance: BusinessHomeView()
                case .support: BusinessWorkspaceView(function: "support")
                case .product: HomeLaunchView(part: .product) { open($0, on: path) }
                case .chats: HomeLaunchView(part: .chats) { open($0, on: path) }
                case .profile: ProfileView()
                }
            }
                .toolbar(tabBarVisibility, for: .tabBar)
                // The message box sits above the tab bar on every tab (the person: "new chat should show up every tab
                // you're on"). Each tab keeps its own project and draft, and every New agent leads to the box of Chats.
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    NewChatBar(place: NewChatPlace(tab))
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
                .navigationDestination(for: AgentListRoute.self) { _ in AgentsView() }
                .navigationDestination(for: ArchivedChatsRoute.self) { _ in
                    HomeLaunchView(part: .archived) { open($0, on: path) }.inlineNavigationTitle()
                }
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
                .modifier(InAppDestinations())
                .background(BoardStyle.paper)
        }
        // On the stack itself, so that the pushed pages get it too. A link to an issue, a pull request or a support
        // thread opens on this stack (InAppLink).
        .stackRoutes { route in path.wrappedValue.append(route) }
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

    /// Shows a Team chat or a direct chat on the stack of Chats, with the list under it. The sheets close. One notice of
    /// an inbox chat shows over the list of its notices, so Back leads there.
    private func showTeamChat(_ chat: TeamChatKey) {
        notificationDecisionId = nil
        let routes = chat.thread == nil ? [TeamChatRoute(chat: chat)]
            : [TeamChatRoute(chat: chat.wholeChat), TeamChatRoute(chat: chat)]
        show(.chats, path: NavigationPath(routes))
    }

    /// Shows the cards that wait on Product, with the rest of Product under them. The card with this ID comes first.
    private func showCards(_ decisionId: String?) {
        // A card opens in the chat of its agent, where its work is. Only "Review cards", or a card that the app does
        // not have yet, opens the list of cards.
        if let decisionId, let decision = store.decisions.first(where: { $0.id == decisionId }) {
            notificationDecisionId = nil
            show(.chats, path: NavigationPath([store.decisionChatRoute(for: decision)]))
            return
        }
        notificationDecisionId = decisionId
        show(.product, path: NavigationPath([HomeDestination.inbox]))
    }

    /// Opens the place of a row of Product or Chats on the stack of its tab, so Back leads to that tab.
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
            show(.chats, path: NavigationPath([route]))
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
        // Every chat is on the Chats tab, and a project opens from the name of its chat.
        // Each way to start a chat, run as the person would: the message box of Chats shows with the project chosen.
        case "new-chat": open(.chats); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "agent-new-chat": open(.chats, [AgentRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        case "chat-new-chat": open(.chats, [ConversationRoute(botId: agent)]); later { store.startChat(in: store.bots.first { $0.id == agent }) }
        // The first message of the box, sent as its Send does: in a new chat of the project, which stays closed.
        case "new-chat-send":
            open(.chats)
            later {
                guard let bot = store.bots.first(where: { $0.id == agent }) else { return }
                _ = await store.sendNewChat(in: bot, text: "Which sellers had a payout held on Friday?")
            }
        // Brainstorm: the page with the talk of the demo, and with Next steps the message box of Product that gets them.
        case "brainstorm": open(.product); later { PendingBrainstorm.shared.opening = true }
        case "brainstorm-steps":
            open(.product)
            later {
                PendingBrainstorm.shared.finishing = true
                PendingBrainstorm.shared.opening = true
            }
        case "agent-list": open(.chats, [AgentListRoute()])
        case "cards": open(.product, [HomeDestination.inbox])
        case "chat": open(.chats, [ConversationRoute(botId: agent)])
        // Personal, with the line of its Antiwork budget (PersonalBudget.swift).
        case "chat-personal":
            open(.chats, [ConversationRoute(botId: store.bots.first { $0.name == "Personal" && $0.chatOf == nil }?.id ?? agent)])
        case "coconut": open(.chats, [ConversationRoute(botId: store.bots.coconutChat?.id ?? agent)])
        // The page of the archived chats, which the last row of Chats opens.
        case "archived": open(.chats, [ArchivedChatsRoute()])
        case "team-chat": open(.chats, [TeamChatRoute(chat: TeamChatKey(room: room))])
        case "agent": open(.chats, [AgentRoute(botId: agent)])
        case "agent-routines": open(.chats, [AgentRoute(botId: agent), BotSettingsRoute(botId: agent)])
        case "all-pages": open(.chats, [AllPagesRoute()])
        case "usage": open(.chats, [UsageRoute()])
        case "agent-usage": open(.chats, [AgentRoute(botId: agent), UsageRoute(botId: agent)])
        case "live": open(.chats, [LiveRoute()])
        case "spend": open(.chats, [SpendRoute()])
        // The older names of the tabs.
        case "todos", "inbox": open(.product)
        case "agents", "home": open(.chats)
        case "business", "dashboards": open(.finance)
        case "projects": open(.finance, [BusinessRoute.projects])
        case "accounting": open(.finance, [BusinessRoute.function("accounting")])
        case "support-work": open(.support)
        case "support-conversation":
            open(.support, [SupportThreadRoute(id: "sample-0", subject: "Payout timing question")])
        // A risk review past SLA of Finance (RiskReviews.swift): one that Gumclaw has, and one that waits for a choice.
        case "risk-review": open(.finance, [RiskReviewRoute(user: "4321043910336")])
        case "risk-review-new": open(.finance, [RiskReviewRoute(user: "8155930716101")])
        // An issue and a pull request of Product, as a tap on their rows opens them.
        case "github-issue": open(.product, [GitHubItemRoute(repository: "example/gumroad", number: 4712)])
        case "github-pull": open(.product, [GitHubItemRoute(repository: "example/gumroad", number: 4731)])
        case "reports", "history": open(.finance, [BusinessRoute.reports])
        case "marketing": open(.finance, [BusinessRoute.function("marketing")])
        case "ledger": open(.finance, [BusinessRoute.ledger])
        case "page":
            // The list of the pages loads first. The page then opens over the page of its agent.
            Task {
                if store.documents == nil { await store.loadDocuments(quiet: true) }
                guard let page = store.documents?.first else { return }
                open(.chats, store.documentPath(page.id))
            }
        case "notices": open(.chats, [NoticeBoardRoute(roomId: room)])
        // The inbox chat Notices of the team preview (TeamInbox.swift), one notice with answers, and the tap on the
        // notification of a notice.
        case "notice-inbox":
            open(.chats, [TeamChatRoute(chat: TeamChatKey(room: room, chat: AppStore.previewNoticesChat))])
        case "notice-thread":
            let inbox = TeamChatKey(room: room, chat: AppStore.previewNoticesChat)
            open(.chats, [TeamChatRoute(chat: inbox), TeamChatRoute(chat: inbox.inThread(AppStore.previewNoticeThread))])
        case "notice-thread-tap":
            showNotification(.chat(TeamChatKey(room: room, chat: AppStore.previewNoticesChat,
                                               thread: AppStore.previewNoticeThread)))
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
            if place.hasPrefix("report-") { open(.finance, [BusinessRoute.report(String(place.dropFirst(7)))]) }
            if place.hasPrefix("project-") { open(.finance, [BusinessRoute.projects, BusinessRoute.project(String(place.dropFirst(8)))]) }
        }
        #endif
    }
}

extension NewChatPlace {
    /// The place of the message box of a tab (NewChatBar).
    init(_ tab: HomeTab) {
        switch tab {
        case .product: self = .product
        case .support: self = .support
        case .finance: self = .finance
        case .chats: self = .chats
        case .profile: self = .you
        }
    }
}
