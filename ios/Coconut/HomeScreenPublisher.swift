#if os(iOS)
import ActivityKit
import AppIntents
import OSLog
import SwiftUI
import WidgetKit

// The app side of the widgets (ios/HomeScreen, ios/CoconutWidgets): the glance that they show, written at each change
// of what the app shows, and the Live Activity of the work of the agents. The person asked to bring the most important
// thing to the Home Screen and the Lock Screen and to save taps: a card that waits, an agent that works and its step,
// and New agent, each one tap from the Lock Screen.

extension AppStore {
    /// What the widgets and the Live Activity show now: the cards that wait (newest first), the agents that work (the
    /// newest step first), the unread chats (newest first, a muted chat none) and the to-dos of the three tabs. The
    /// same rows as the app: a chat works while its row in Chats says so (botPhase), with the step of that row.
    func homeScreenGlance(now: Date = .now) -> CoconutGlance {
        let byId = Dictionary(bots.map { ($0.id, $0) }) { first, _ in first }
        let cards = decisions.filter { $0.isPending && !answeringDecisionIds.contains($0.id) }
            .sorted { ($0.createdAt.apiDate ?? .distantPast, $0.id) > ($1.createdAt.apiDate ?? .distantPast, $1.id) }
            .prefix(CoconutGlance.limit)
            .map { decision in
                let chat = conversationBotId(for: decision)
                return CoconutGlance.Card(id: decision.id, title: decision.title, chat: chat,
                                          name: byId[chat]?.listName ?? botName(decision.botId),
                                          createdAt: decision.createdAt.apiDate ?? now)
            }
        let working = bots.compactMap { bot -> CoconutGlance.Work? in
            guard let phase = botPhase(bot) else { return nil }
            let task = tasks.last { $0.isRunning && $0.botId == bot.id }
            let detail = task?.phaseDetail?.trimmingCharacters(in: .whitespacesAndNewlines)
            return CoconutGlance.Work(id: bot.id, name: bot.listName, step: phase.label,
                                      detail: detail.flatMap { $0.isEmpty ? nil : $0 }, startedAt: botStepStart(bot))
        }
        .sorted { ($0.startedAt ?? now, $0.id) > ($1.startedAt ?? now, $1.id) }
        .prefix(CoconutGlance.limit)
        let rows = HomeFeed.unread(bots: bots, latest: chatSummaries.newestActivity, rooms: shownTeamRooms,
                                   local: newestLocalMessages, decisions: [])
        let unread = rows.prefix(CoconutGlance.limit).compactMap { row -> CoconutGlance.Unread? in
            guard case .chat(let item, let date) = row else { return nil }
            return glanceUnread(item, date: date)
        }
        let todos = todoIssues == nil ? [] : TodoArea.allCases.map {
            CoconutGlance.Todos(id: $0.rawValue, title: $0.title, count: todoCount($0, now: now),
                                late: todoLateCount($0, now: now))
        }
        return CoconutGlance(cards: Array(cards), working: Array(working), unread: unread,
                             unreadCount: HomeFeed.chatsBadge(bots: bots, rooms: shownTeamRooms, local: newestLocalMessages),
                             todos: todos, updatedAt: now)
    }

    /// An unread chat as a row of the widgets, with the line that its row in Chats shows.
    private func glanceUnread(_ item: ChatListItem, date: Date) -> CoconutGlance.Unread? {
        let count = item.unreadCount
        switch item {
        case .bot(let bot):
            return CoconutGlance.Unread(id: bot.id, name: bot.listName, preview: BotContactRow.preview(of: bot, in: self),
                                        count: count, date: date, link: .chat(bot.id, card: nil))
        case .team(let room):
            guard let key = item.chat else { return nil }
            return CoconutGlance.Unread(id: item.id, name: room.title, preview: TeamChatPreview.text(room.lastMessage),
                                        count: count, date: date, link: .teamChat(key))
        case .direct(_, let direct):
            guard let key = item.chat else { return nil }
            return CoconutGlance.Unread(id: item.id, name: direct.memberName ?? TeamNames.formerMember,
                                        preview: TeamChatPreview.text(direct.lastMessage, direct: true),
                                        count: count, date: date, link: .teamChat(key))
        case .botDirect(_, let direct):
            guard let key = item.chat else { return nil }
            return CoconutGlance.Unread(id: item.id, name: direct.botName ?? "Agent",
                                        preview: TeamChatPreview.text(direct.lastMessage, direct: true),
                                        count: count, date: date, link: .teamChat(key))
        }
    }

    /// True while the app knows what works: a state of the Mac that it read in this connection, or the demo. A lost
    /// connection shows no work in the app (isBotActive), and the widgets keep the last glance instead of that.
    var homeScreenKnowsWork: Bool { isDemo || (connection == .connected && lastSync != nil) }
}

extension HomeScreenLink {
    /// A chat of a team.
    static func teamChat(_ key: TeamChatKey) -> HomeScreenLink {
        .team(room: key.room, member: key.member, chat: key.chat, bot: key.bot)
    }
}

/// Keeps the widgets and the Live Activity in step with the app: at each change of the glance, at most every two
/// seconds, and every two minutes while the app shows, so their times stay fresh. The app also writes the glance when
/// it leaves the front, because the widgets show it until the app runs again.
struct HomeScreenPublisher: ViewModifier {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @State private var publishedAt = Date.distantPast

    /// The shortest time between two writes.
    static let pace: TimeInterval = 2
    /// How often the app writes the glance again while it shows, with nothing new.
    static let refresh: Duration = .seconds(120)

    func body(content: Content) -> some View {
        content
            .task(id: key) {
                let wait = publishedAt.addingTimeInterval(Self.pace).timeIntervalSinceNow
                if wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
                guard !Task.isCancelled else { return }
                await publish()
            }
            .task(id: scenePhase) {
                await publish()
                guard scenePhase == .active else { return }
                while !Task.isCancelled {
                    try? await Task.sleep(for: Self.refresh)
                    guard !Task.isCancelled else { return }
                    await publish()
                }
            }
    }

    /// The glance without its time, so only a change of what it shows writes it again.
    private var key: CoconutGlance? {
        guard store.homeScreenKnowsWork else { return nil }
        var glance = store.homeScreenGlance()
        glance.updatedAt = .distantPast
        return glance
    }

    private func publish() async {
        // A test run writes nothing: the glance is for the chats of the person.
        guard !TestIsolation.active else { return }
        guard store.isDemo || store.hasPairing else {
            GlanceStore.save(nil)
            WidgetCenter.shared.reloadAllTimelines()
            await WorkActivities.update(nil, canStart: false)
            return
        }
        guard store.homeScreenKnowsWork else { return }
        publishedAt = .now
        let glance = store.homeScreenGlance()
        GlanceStore.save(glance)
        for kind in CoconutGlance.widgetKinds { WidgetCenter.shared.reloadTimelines(ofKind: kind) }
        await WorkActivities.update(glance, canStart: scenePhase == .active)
        await WorkActivities.nameWidgetToken()
    }
}

/// The one Live Activity of the work of the agents (CoconutWorkAttributes). It starts while the app shows and an agent
/// works or a card waits, and follows each change while the app runs. After the person leaves the app, the Mac updates it
/// and ends it through the relay with its push token (backend/coconut/home_screen.py), with a glance sealed for this
/// iPhone. iOS lets an app start an activity only from the front, so an activity starts again only in the app. An
/// activity that hears from neither says from when it is (staleDate) instead of counting on.
enum WorkActivities {
    /// How long a state shows its times live after the app wrote it.
    static let freshFor: TimeInterval = 5 * 60
    /// Each line names why an activity did not start. A line holds no text of a chat.
    private static let log = Logger(subsystem: "com.gumroad.coconut", category: "live-activity")

    static func update(_ glance: CoconutGlance?, canStart: Bool) async {
        let activities = Activity<CoconutWorkAttributes>.activities
        let state = glance.map(CoconutWorkAttributes.ContentState.init)
        guard let state, !state.isDone else {
            let ended = !activities.isEmpty
            for activity in activities { await activity.end(nil, dismissalPolicy: .immediate) }
            // The Mac would otherwise update an activity that ended.
            if ended { await PushTokens.shared.forgetActivities() }
            return
        }
        let content = ActivityContent(state: state, staleDate: state.updatedAt.addingTimeInterval(freshFor))
        guard let current = activities.first(where: { $0.activityState == .active || $0.activityState == .stale }) else {
            guard canStart else { return }
            guard ActivityAuthorizationInfo().areActivitiesEnabled else {
                log.notice("Live Activities are off for Coconut.")
                return
            }
            do {
                follow(try Activity.request(attributes: CoconutWorkAttributes(), content: content, pushType: .token))
            } catch {
                // Without a push token the activity still follows the app, as before the Mac sent glances.
                log.error("The Live Activity did not start with a push token: \(String(describing: error), privacy: .public)")
                do {
                    _ = try Activity.request(attributes: CoconutWorkAttributes(), content: content, pushType: nil)
                } catch {
                    log.error("The Live Activity did not start: \(String(describing: error), privacy: .public)")
                }
            }
            return
        }
        // One activity holds every agent. An activity of an earlier run of the app ends.
        for activity in activities where activity.id != current.id { await activity.end(nil, dismissalPolicy: .immediate) }
        await current.update(content)
        let (id, token) = (current.id, current.pushToken)
        follow(current)
        if let token { await PushTokens.shared.name(token, activity: id) }
    }

    /// Gives each new push token of the activity to the relay, for the run of the app: iOS can change it while the
    /// activity runs.
    private static func follow(_ activity: sending Activity<CoconutWorkAttributes>) {
        let id = activity.id
        Task {
            guard await PushTokens.shared.follow(id) else { return }
            for await token in activity.pushTokenUpdates { await PushTokens.shared.name(token, activity: id) }
        }
    }

    /// Gives the relay the push token of the widgets (iOS 26), once in each run of the app. The widget extension names it
    /// when it changes (GlancePushHandler), but the extension can stop before the relay answers.
    static func nameWidgetToken() async {
        guard #available(iOS 26.0, *), HomeScreenRoute.load() != nil, !(await PushTokens.shared.widgetsNamed),
              let token = await WidgetCenter.shared.currentPushInfo?.token else { return }
        await PushTokens.shared.name(widgets: token)
    }
}

/// The push tokens of the Live Activity and of the widgets that the relay took in this run of the app. Without a route of
/// notifications on the relay a call fails, and the next change tries again.
actor PushTokens {
    static let shared = PushTokens()
    private var named: [String: Data] = [:]
    private var followed = Set<String>()
    private(set) var widgetsNamed = false

    /// True the first time for each activity.
    func follow(_ activity: String) -> Bool { followed.insert(activity).inserted }

    func name(_ token: Data, activity: String) async {
        guard named[activity] != token else { return }
        if await HomeScreenRelay.setActivityToken(token) { named[activity] = token }
    }

    func forgetActivities() async {
        named = [:]
        await HomeScreenRelay.setActivityToken(nil)
    }

    func name(widgets token: Data) async {
        widgetsNamed = await HomeScreenRelay.setWidgetToken(token)
    }
}

/// New agent in Spotlight, Siri and Shortcuts, and on the Action button through a shortcut.
struct CoconutShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: NewAgentIntent(),
                    phrases: ["New agent in \(.applicationName)", "Ask \(.applicationName)"],
                    shortTitle: "New agent", systemImageName: "square.and.pencil")
    }
}
#endif
