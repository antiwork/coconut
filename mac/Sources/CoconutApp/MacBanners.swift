import AppKit
import SwiftUI
import UserNotifications

/// The banners of macOS for a new team message, a new direct message, a new card and a new reply of an agent.
/// MessageBanners and AgentBanners make each decision. This class only gives them to macOS, asks for permission, tells
/// the Mac how long nobody used this Mac, and opens the chat of a banner that the person clicks. Only a full install
/// starts it: a Mac for a team only has no chat client.
@MainActor
final class MacBanners: NSObject, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let model: AppModel
    private let store: AppStore
    /// The action that opens a window of the app. A view gives it, so a click can open the main window also while the
    /// window is closed.
    var openWindow: OpenWindowAction?
    /// The last change of Notification Center. Each change waits for the one before it, so a removal never comes
    /// before an earlier banner of the same chat.
    private var lastChange: Task<Void, Never>?
    private var askedForPermission = false
    private var posted = PostedBanners()
    /// The cards and the chats of the agents at the last change, nil before the first.
    private var agents: AgentActivity?

    init(model: AppModel, store: AppStore) {
        self.model = model
        self.store = store
    }

    /// Start before the store starts, so the store gives each list of the teams to the banners. macOS gives a click
    /// that started the app to the delegate that the app sets before the launch ends.
    func start() {
        center.delegate = self
        // The first change of Notification Center reads the banners that macOS has, also from an earlier run. Each
        // plan waits for it, so no plan runs before the set is known.
        lastChange = Task {
            let delivered = await center.deliveredNotifications().map(\.request.identifier)
            posted = PostedBanners(delivered: delivered)
        }
        // Each list goes to the rule, also a list that is equal to the one before: a read in the app changes the count
        // in the list of the store before the next list loads, and that next list must still remove the banner.
        store.teamListLoaded = { [weak self] before, after in
            guard let self else { return }
            let plan = MessageBanners.plan(before: before, after: after, settings: store.notificationSettings ?? .standard)
            change(remove: plan.remove, post: plan.post.map(Banner.init), asking: plan.asksPermission)
        }
        // While the iPhone waits for the person to leave the Mac (NotificationSettings.mobileDelay), this Mac reports
        // how long nobody used it, every 30 seconds.
        Task {
            while true {
                await reportPresence()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }

    /// A change of the cards or the chats of the agents (AgentBannerWatcher). macOS asks for permission at the first
    /// banner of an agent, when no team asked before.
    func agentsChanged(_ activity: AgentActivity) {
        let plan = AgentBanners.plan(before: agents, after: activity, settings: store.notificationSettings ?? .standard)
        agents = activity
        change(remove: plan.remove, post: plan.post.map(Banner.init), asking: !plan.post.isEmpty)
    }

    /// One change of Notification Center: the removals first, then the new banners.
    private func change(remove: [String], post: [Banner], asking: Bool) {
        let asks = asking && !askedForPermission
        if asks { askedForPermission = true }
        guard asks || !remove.isEmpty || !post.isEmpty else { return }
        let previous = lastChange
        lastChange = Task {
            await previous?.value
            let removals = posted.removals(remove: remove, post: post.map(\.identifier))
            if !removals.isEmpty { center.removeDeliveredNotifications(withIdentifiers: removals) }
            guard asks || !post.isEmpty, await allowed(asking: asks) else { return }
            for banner in post { try? await center.add(Self.request(banner)) }
        }
    }

    /// The idle time of this Mac, for the iPhone that waits while the person works here. It reports nothing while
    /// macOS shows no banner of Coconut: the banners of this Mac then reach nobody.
    private func reportPresence() async {
        guard store.notificationSettings?.mobileDelay != nil else { return }
        let status = await center.notificationSettings().authorizationStatus
        guard status == .authorized || status == .provisional,
              let anyInput = CGEventType(rawValue: ~0) else { return }
        await store.reportPresence(idle: CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: anyInput))
    }

    /// True when the person allows the banners. macOS asks the person one time, when the Mac first has an active team.
    /// After a no, the app posts nothing and does not ask again. The person can change it in System Settings.
    private func allowed(asking: Bool) async -> Bool {
        var status = await center.notificationSettings().authorizationStatus
        if asking && status == .notDetermined {
            _ = try? await center.requestAuthorization(options: [.alert, .sound])
            status = await center.notificationSettings().authorizationStatus
        }
        return status == .authorized || status == .provisional
    }

    /// The request of a banner. The identifier is the chat or the card, so a newer banner of a chat takes the place of
    /// the older one.
    private static func request(_ banner: Banner) -> UNNotificationRequest {
        let content = UNMutableNotificationContent()
        content.title = banner.title
        if let subtitle = banner.subtitle { content.subtitle = subtitle }
        content.body = banner.body
        content.sound = .default
        content.threadIdentifier = banner.thread
        content.userInfo = banner.userInfo
        return UNNotificationRequest(identifier: banner.identifier, content: content, trigger: nil)
    }

    /// A click on a banner: the app comes to the front, the main window opens, and it shows the chat while the app has
    /// it. A share to a chat opens it on the same path (AppModel.shownChat and shownBot).
    private func open(_ chat: TeamChatKey?, bot: String?) {
        if let bot, store.bots.contains(where: { $0.id == bot }) { model.shownBot = bot }
        if let shown = MessageBanners.shownChat(chat, rooms: store.team) { model.shownChat = shown }
        openWindow?(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }

    // The class has no `willPresent`: macOS then shows no banner while Coconut is the active app. The sidebar shows the
    // unread count then.

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping @Sendable () -> Void) {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else {
            completionHandler()
            return
        }
        let info = response.notification.request.content.userInfo
        let chat = MessageBanners.chat(userInfo: info)
        let bot = AgentBanners.bot(userInfo: info)
        Task { @MainActor in
            self.open(chat, bot: bot)
            completionHandler()
        }
    }
}

/// One banner for macOS: of a team chat (MessageBanner) or of an agent (AgentBanner).
private struct Banner: Sendable {
    let identifier: String
    let title: String
    let subtitle: String?
    let body: String
    let thread: String
    let userInfo: [String: String]

    init(_ banner: MessageBanner) {
        identifier = banner.identifier
        title = banner.title
        subtitle = banner.subtitle
        body = banner.body
        thread = banner.thread
        userInfo = MessageBanners.userInfo(banner.chat)
    }

    init(_ banner: AgentBanner) {
        identifier = banner.identifier
        title = banner.title
        subtitle = banner.subtitle
        body = banner.body
        thread = banner.thread
        userInfo = AgentBanners.userInfo(banner.bot)
    }
}

/// Gives the action that opens a window to the banners when the view appears.
struct BannerWindowOpener: ViewModifier {
    let banners: MacBanners
    @Environment(\.openWindow) private var openWindow

    func body(content: Content) -> some View {
        content.onAppear { banners.openWindow = openWindow }
    }
}

/// Gives each change of the cards and the chats of the agents to the banners. It sits on the menu bar label, which shows
/// while the app runs, as DockBadge.
struct AgentBannerWatcher: ViewModifier {
    let banners: MacBanners
    let store: AppStore
    let enabled: Bool

    func body(content: Content) -> some View {
        content.onChange(of: AgentActivity(bots: store.bots, decisions: store.decisions), initial: true) { _, activity in
            if enabled && !store.isDemo { banners.agentsChanged(activity) }
        }
    }
}

/// Pause Notifications in the menu bar, as in the menu of Slack. An urgent card still notifies during a pause.
struct PauseNotificationsMenu: View {
    let store: AppStore

    var body: some View {
        if let settings = store.notificationSettings {
            if let end = settings.pauseEnd, end > .now {
                Text(NotificationSettings.pauseText(end))
                Button("Resume Notifications") {
                    var resumed = settings
                    resumed.pausedUntil = nil
                    store.setNotificationSettings(resumed)
                }
            } else {
                Menu("Pause Notifications") {
                    ForEach(NotificationSettings.Pause.allCases, id: \.self) { choice in
                        Button(choice.title) { store.setNotificationSettings(settings.paused(choice)) }
                    }
                }
            }
        }
    }
}

/// Keeps the badge on the Dock tile at the count of the app (AppStore.appBadgeCount). It sits on the menu bar label,
/// which shows while the app runs, so the number stays right with the window closed.
struct DockBadge: ViewModifier {
    let store: AppStore
    let enabled: Bool

    func body(content: Content) -> some View {
        content.onChange(of: store.reconciledBadgeCount, initial: true) { _, _ in
            if enabled && !store.isDemo { store.reconcileBadge() }
        }
    }
}
