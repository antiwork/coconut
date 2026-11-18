import Foundation
import Observation
#if canImport(UIKit)
import UIKit
#endif
#if os(macOS)
import AppKit
#endif
import UserNotifications
import SwiftUI

struct NotificationSettingsView: View {
    @Environment(AppStore.self) private var store
    @Bindable private var push = PushNotifications.shared

    var body: some View {
        Form {
            Section {
                Text(NotificationSentence.text(level: push.notificationLevel, outside: push.outsideWorkHours,
                                               replies: push.messageNotifications,
                                               team: store.team?.notifiesPhone ?? false,
                                               direct: store.team?.notifiesDirect ?? false, schedule: push.schedule))
                    .accessibilityIdentifier("notification-summary")
            }
            Section {
                NavigationLink {
                    WorkHoursView()
                } label: {
                    LabeledContent("Work hours", value: NotificationSentence.workHours(push.schedule))
                }
                .disabled(push.outsideWorkHours == .same)
                .accessibilityIdentifier("work-hours")
            }
            Section {
                Picker("During work hours", selection: $push.notificationLevel) {
                    ForEach(NotificationLevel.shown, id: \.self) { level in
                        Text(level.title).tag(level).accessibilityIdentifier("notification-level-" + level.rawValue)
                    }
                }
                .pickerStyle(.inline)
                .labelsHidden()
                .accessibilityIdentifier("notification-level")
            } header: {
                Text(push.outsideWorkHours == .same ? "At any time" : "During work hours")
            } footer: {
                Text(push.notificationLevel.detail).accessibilityIdentifier("notification-level-footer")
            }
            Section {
                Picker("Outside work hours", selection: $push.outsideWorkHours) {
                    ForEach(OutsideWorkHours.allCases, id: \.self) { choice in
                        Text(choice.title).tag(choice).accessibilityIdentifier("outside-work-hours-" + choice.rawValue)
                    }
                }
                .pickerStyle(.inline)
                .labelsHidden()
                .accessibilityIdentifier("outside-work-hours")
            } header: {
                Text("Outside work hours")
            }
            Section {
                Toggle(isOn: $push.messageNotifications) {
                    Text("Also notify me when an agent replies").frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle()).onTapGesture { push.messageNotifications.toggle() }
                }
                .disabled(push.notificationLevel == .urgentOnly)
                .accessibilityIdentifier("message-notifications")
            } header: {
                Text("Agent replies")
            } footer: {
                Text(push.notificationLevel == .urgentOnly ? "With Only urgent, agent replies do not notify you."
                                                           : "This includes a task that an agent could not finish.")
            }
            Section {
                NavigationLink {
                    UrgentCardsView(summary: store.pushStatus?.urgent)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("What counts as urgent?")
                        if let summary = store.pushStatus?.urgent {
                            Text(summary.weekText).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                }
                .accessibilityIdentifier("urgent-cards")
            }
            Section("Delivery") {
                LabeledContent("Last notification", value: store.pushStatus == nil ? "Not available"
                                                           : PushStatus.describe(store.pushStatus?.lastDeliveredAt) ?? "None yet")
                    .accessibilityIdentifier("last-notification")
                if let error = store.pushStatus?.lastError {
                    LabeledContent("Last error") {
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(PushStatus.explain(error))
                            if let at = PushStatus.describe(store.pushStatus?.lastErrorAt) {
                                Text(at).font(.footnote).foregroundStyle(.secondary)
                            }
                        }
                        .multilineTextAlignment(.trailing)
                    }
                    .accessibilityIdentifier("last-notification-error")
                }
            }
            Section {
                Button(push.allowed ? "iOS notification settings" : "Enable notifications") {
                    if push.authorization == .notDetermined {
                        Task { await push.refresh(requestPermission: true); await store.syncPushRegistration() }
                    } else { push.openSystemSettings() }
                }
                if push.allowed && !push.badgesAllowed {
                    Text("App badges are off in iOS Settings.").font(.footnote).foregroundStyle(.secondary)
                }
                if let error = store.pushRegistrationError ?? push.error {
                    InlineError(error)
                }
            }
        }
        .tint(BoardStyle.green)
        .settingsBackground()
        .navigationTitle("Notifications")
        .inlineNavigationTitle()
        // Read again when the connection returns, so the screen does not keep an unavailable status.
        .task(id: store.connection) { await store.refreshPushStatus() }
    }
}

/// The editor of the work hours: start, end, time zone and days.
struct WorkHoursView: View {
    @Bindable private var push = PushNotifications.shared
    @State private var daysExpanded = true

    var body: some View {
        Form {
            Section {
                DatePicker("Start", selection: minutes($push.workStartMinutes), displayedComponents: .hourAndMinute)
                    .accessibilityLabel("Work hours start")
                    .accessibilityIdentifier("work-hours-start")
                DatePicker("End", selection: minutes($push.workEndMinutes), displayedComponents: .hourAndMinute)
                    .accessibilityLabel("Work hours end")
                    .accessibilityIdentifier("work-hours-end")
                Picker("Time zone", selection: $push.workTimeZone) {
                    ForEach(TimeZone.knownTimeZoneIdentifiers, id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ")).tag($0) }
                }
                .pagePickerStyle()
                .accessibilityIdentifier("work-hours-timezone")
                if push.workStartMinutes >= push.workEndMinutes {
                    InlineError("Start must be before end.")
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("Work hours are not valid. Start must be before end.")
                        .accessibilityIdentifier("work-hours-error")
                }
            }
            Section {
                DisclosureGroup(isExpanded: $daysExpanded) {
                    ForEach(NotificationSchedule.weekdayNames, id: \.number) { day in
                        Toggle(day.name, isOn: weekday(day.number))
                            .accessibilityIdentifier("work-day-" + day.name.lowercased())
                    }
                } label: {
                    HStack {
                        Text("Work days")
                        Spacer()
                        Text(NotificationSchedule.summary(of: push.workWeekdays)).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("work-days-disclosure")
                }
                if push.workWeekdays.isEmpty {
                    InlineError("Choose at least one work day.")
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("Work days are not valid. Choose at least one day.")
                        .accessibilityIdentifier("work-days-error")
                }
            }
        }
        .tint(BoardStyle.green)
        .settingsBackground()
        .environment(\.timeZone, TimeZone(identifier: "UTC") ?? .gmt)
        .navigationTitle("Work hours")
        .inlineNavigationTitle()
    }

    private func weekday(_ number: Int) -> Binding<Bool> {
        Binding(get: { push.workWeekdays.contains(number) },
                set: { on in
                    var days = Set(push.workWeekdays)
                    if on { days.insert(number) } else { days.remove(number) }
                    push.workWeekdays = days.sorted()
                })
    }

    // The picker shows plain clock times, so it reads and writes minutes in UTC.
    private func minutes(_ value: Binding<Int>) -> Binding<Date> {
        Binding(get: { NotificationSchedule.date(minutes: value.wrappedValue) },
                set: { value.wrappedValue = NotificationSchedule.minutes(of: $0) })
    }
}

/// What makes a card urgent, with the last urgent cards as examples.
struct UrgentCardsView: View {
    let summary: UrgentSummary?

    var body: some View {
        Form {
            Section {
                Text(NotificationSentence.urgentExplanation)
                if let summary {
                    Text(summary.weekText).foregroundStyle(.secondary).accessibilityIdentifier("urgent-week")
                }
            }
            if let examples = summary?.examples, !examples.isEmpty {
                Section("Last urgent cards") {
                    ForEach(Array(examples.enumerated()), id: \.offset) { _, example in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(example.title ?? "Untitled card")
                            let detail = [example.botName, PushStatus.describe(example.createdAt)].compactMap { $0 }
                            if !detail.isEmpty {
                                Text(detail.joined(separator: " · ")).font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        .settingsBackground()
        .navigationTitle("Urgent cards")
        .inlineNavigationTitle()
    }
}

/// How often non-urgent items notify. The Mac applies it; see `push_schedule.py`.
enum NotificationLevel: String, Codable, CaseIterable, Sendable {
    case every
    case grouped
    case urgentOnly = "urgent_only"

    var title: String {
        switch self {
        case .every: "Every card"
        case .grouped: "One summary at a time"
        case .urgentOnly: "Only urgent"
        }
    }

    /// The footer of the chosen level. It says exactly what the Mac does (push_schedule.SETTLE and INTERVAL).
    var detail: String {
        switch self {
        case .every: "Each card notifies you at once, in its own notification."
        case .grouped: "Each card notifies you at once, in its own notification."
        case .urgentOnly: "Only urgent cards notify you. Other cards wait in Inbox without a notification."
        }
    }

    /// The choices of the app. The Mac sends one notification per item, so a summary is no choice any more.
    static let shown: [NotificationLevel] = [.every, .urgentOnly]

    /// The saved level. Before levels existed, turning decisions off meant only urgent cards. A saved summary is now
    /// one notification per card.
    static func load(from defaults: UserDefaults = .standard) -> NotificationLevel {
        if let saved = defaults.string(forKey: "notificationLevel"), let level = NotificationLevel(rawValue: saved) {
            return level == .grouped ? .every : level
        }
        return defaults.object(forKey: "decisionNotifications") as? Bool == false ? .urgentOnly : .every
    }
}

struct PushChoices: Equatable, Sendable {
    let preferences: NotificationPreferences
    let schedule: NotificationSchedule
    /// Only urgent and Nothing give the same schedule, so this must be a part of its own.
    let outside: OutsideWorkHours
}

struct NotificationPreferences: Encodable, Equatable, Sendable {
    let level: NotificationLevel
    let messages: Bool
}

/// What the Mac last did for this iPhone. Times are ISO 8601 text from the Mac.
struct PushStatus: Decodable, Equatable, Sendable {
    let registered: Bool
    var urgent: UrgentSummary? = nil
    let lastDeliveredAt: String?
    let lastError: String?
    let lastErrorAt: String?

    static func date(_ text: String?) -> Date? {
        guard let text else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    static func describe(_ text: String?, relativeTo now: Date = Date()) -> String? {
        guard let date = date(text) else { return nil }
        return RelativeDateTimeFormatter().localizedString(for: date, relativeTo: now)
    }

    /// A short explanation of the reason that the relay or APNs gave.
    static func explain(_ reason: String) -> String {
        switch reason {
        case "Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic": "iOS no longer accepts this iPhone’s address. Open Coconut to register again."
        case "RelayRejected": "The notification service refused this iPhone. Open Coconut to register again."
        case "NotRefreshed": "This iPhone did not connect for 30 days. Open Coconut to register again."
        case "RateLimited", "RetryLater", "TooManyRequests": "Too many notifications. Coconut retries later."
        case "EncryptionUnavailable": "Your Mac could not encrypt a notification. Coconut retries later."
        case "TransportUnavailable", "PushNotConfigured": "Your Mac could not reach the notification service. Coconut retries later."
        default: reason
        }
    }
}

struct NotificationSchedule: Codable, Equatable, Sendable {
    static let defaultTimeZone = "America/New_York"
    /// ISO weekdays: 1 is Monday and 7 is Sunday.
    static let allWeekdays = Array(1...7)
    static let weekdayNames: [(number: Int, name: String)] = [
        (1, "Monday"), (2, "Tuesday"), (3, "Wednesday"), (4, "Thursday"), (5, "Friday"), (6, "Saturday"), (7, "Sunday")]
    var enabled = true
    var start = "09:00"
    var end = "18:00"
    var timezone = NotificationSchedule.defaultTimeZone
    var weekdays = NotificationSchedule.allWeekdays

    var hasValidDays: Bool {
        !weekdays.isEmpty && weekdays.allSatisfy { (1...7).contains($0) } && Set(weekdays).count == weekdays.count
    }

    var isValid: Bool {
        start < end && TimeZone(identifier: timezone) != nil && hasValidDays
    }

    /// What to send the Mac. A turned-off schedule always goes out, with a valid window, so an
    /// invalid edit never keeps the Mac pushing by the old window. An invalid enabled one is held back.
    var registrable: NotificationSchedule? {
        if isValid { return self }
        return enabled ? nil : NotificationSchedule(enabled: false)
    }

    /// A short label for the chosen days.
    static func summary(of days: [Int]) -> String {
        let chosen = Set(days).intersection(allWeekdays)
        if chosen.count == 7 { return "Every day" }
        if chosen == [1, 2, 3, 4, 5] { return "Weekdays" }
        if chosen == [6, 7] { return "Weekends" }
        if chosen.isEmpty { return "None" }
        return weekdayNames.filter { chosen.contains($0.number) }.map { String($0.name.prefix(3)) }.joined(separator: ", ")
    }

    private static var utc: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        return calendar
    }

    /// The minutes since midnight a picker date shows, read as UTC clock time. Never negative.
    static func minutes(of date: Date) -> Int {
        let parts = utc.dateComponents([.hour, .minute], from: date)
        return (parts.hour ?? 0) * 60 + (parts.minute ?? 0)
    }

    /// The picker date that shows these minutes as UTC clock time.
    static func date(minutes: Int) -> Date {
        let day = ((minutes % 1440) + 1440) % 1440
        return utc.date(from: DateComponents(year: 2001, month: 1, day: 1, hour: day / 60, minute: day % 60)) ?? Date(timeIntervalSince1970: 0)
    }

    /// The schedule saved in `defaults`. Anything missing or unreadable takes its default.
    static func load(from defaults: UserDefaults = .standard) -> NotificationSchedule {
        NotificationSchedule(enabled: OutsideWorkHours.load(from: defaults) != .same,
                             start: clock(defaults.object(forKey: "workStartMinutes") as? Int ?? 9 * 60),
                             end: clock(defaults.object(forKey: "workEndMinutes") as? Int ?? 18 * 60),
                             timezone: defaults.string(forKey: "workTimeZone") ?? defaultTimeZone,
                             weekdays: (defaults.array(forKey: "workWeekdays") as? [Int]) ?? allWeekdays)
    }

    /// Minutes since midnight for an `HH:MM` clock text, or nil when it is not one.
    static func minutes(fromClock text: String) -> Int? {
        let parts = text.split(separator: ":").compactMap { Int($0) }
        return parts.count == 2 ? parts[0] * 60 + parts[1] : nil
    }

    static func clock(_ minutes: Int) -> String { String(format: "%02d:%02d", ((minutes % 1440) + 1440) % 1440 / 60, ((minutes % 1440) + 1440) % 1440 % 60) }
}

enum NotificationDestination: Equatable {
    case inbox(String)
    /// A group notification opens the Inbox list.
    case inboxList
    case conversation(ConversationRoute)
    /// The Team chat of a team, by its room ID.
    case team(String)
    /// Another chat of a team: a named chat, or a direct chat with one person of the team.
    case chat(TeamChatKey)
    /// The Chats list: a team notification of a team that the app no longer has.
    case chatList
    /// The notice board of a team, by its room ID: a notification of a new notice.
    case notices(String)

    /// The place of the chat of a team: the Team chat, a named chat or a direct chat.
    init(chat: TeamChatKey) {
        self = chat.isDirect || chat.chat != nil ? .chat(chat) : .team(chat.room)
    }

    /// The chat that this place shows. Nil for a place that is not a chat of a team.
    var teamChat: TeamChatKey? {
        switch self {
        case .team(let room): TeamChatKey(room: room)
        case .chat(let chat): chat
        default: nil
        }
    }
}

struct NotificationTarget: Equatable, Sendable {
    let deviceId: String
    let botId: String
    let taskId: String
    let decisionId: String?
    let messageId: String?
    /// A group counts several items and names none of them.
    var group = false
    /// The room ID of a team message, or of a group of the messages of one team. For a direct message, the room of
    /// its chat.
    var team: String? = nil
    /// The other member of a direct chat, for a direct message or a group of the messages of one direct chat.
    var member: String? = nil
    /// The team bot ID of a direct chat with a bot, for a direct message or a group of the messages of one such chat.
    var bot: String? = nil
    /// The named chat of a team message, or of a group of the messages of one named chat. An older Mac sends none.
    var named: String? = nil
    /// The room of a new notice: a tap opens the notice board of that team.
    var noticeRoom: String? = nil

    var conversationRoute: ConversationRoute { ConversationRoute(botId: botId, decisionId: decisionId, messageId: messageId) }
    /// The chat of a team that the notification opens: a Team chat, a named chat or a direct chat.
    var chat: TeamChatKey? { team.map { TeamChatKey(room: $0, member: member, chat: named, bot: bot) } }

    func destination(pendingDecisionIds: Set<String>, snapshotReady: Bool) -> NotificationDestination? {
        if let noticeRoom { return .notices(noticeRoom) }
        if let chat { return NotificationDestination(chat: chat) }
        if group { return .inboxList }
        if let decisionId {
            guard snapshotReady else { return nil }
            if pendingDecisionIds.contains(decisionId) { return .inbox(decisionId) }
        }
        return .conversation(conversationRoute)
    }

    init?(userInfo: [AnyHashable: Any]) {
        let decoded = try? NotificationKeys.content(from: userInfo)
        guard userInfo["coconut_encrypted"] == nil || decoded != nil else { return nil }
        guard let data = (decoded?["coconut"] ?? userInfo["coconut"]) as? [String: Any], data["version"] as? Int == 1,
              let device = data["device_id"] as? String, UUID(uuidString: device) != nil else { return nil }
        // A team message names its room. A group of one team names it too, and each other group names none.
        let room = data["team"] as? String
        guard room == nil || isLowercaseHex64(room!) else { return nil }
        // A message of a named chat names the chat too. A value that is not the ID of an entry opens the Team chat.
        let named = (data["chat"] as? String).flatMap { isEntryId($0) ? $0 : nil }
        // A direct message names the room and the other member of its chat, or the team bot ID of a chat with a bot.
        // A group of one direct chat names them too.
        var direct: (room: String, member: String?, bot: String?)?
        if let value = data["direct"], !(value is NSNull) {
            guard let fields = value as? [String: Any], let room = fields["team"] as? String, isLowercaseHex64(room)
            else { return nil }
            if let member = fields["member"] as? String, isLowercaseHex64(member) {
                direct = (room, member, nil)
            } else if fields["member"] == nil, let bot = fields["bot"] as? String, isTeamBotId(bot, room: room) {
                direct = (room, nil, bot)
            } else {
                return nil
            }
        }
        if data["kind"] as? String == "group" {
            deviceId = device; botId = ""; taskId = ""; decisionId = nil; messageId = nil; group = true
            team = direct?.room ?? room; member = direct?.member; bot = direct?.bot
            self.named = direct == nil && room != nil ? named : nil
            return
        }
        if data["kind"] as? String == "notice" {
            guard let room else { return nil }
            deviceId = device; botId = ""; taskId = ""; decisionId = nil; messageId = nil; noticeRoom = room
            return
        }
        if data["kind"] as? String == "team" {
            guard let room else { return nil }
            deviceId = device; botId = ""; taskId = ""; decisionId = nil; messageId = nil; team = room
            self.named = named
            return
        }
        if data["kind"] as? String == "direct" {
            guard let direct else { return nil }
            deviceId = device; botId = ""; taskId = ""; decisionId = nil; messageId = nil
            team = direct.room; member = direct.member; bot = direct.bot
            return
        }
        guard let bot = data["bot_id"] as? String, UUID(uuidString: bot) != nil,
              let task = data["task_id"] as? String, UUID(uuidString: task) != nil else { return nil }
        let decision = data["decision_id"] as? String
        guard decision == nil || UUID(uuidString: decision!) != nil else { return nil }
        let message = data["message_id"] as? String
        guard message == nil || UUID(uuidString: message!) != nil else { return nil }
        deviceId = device; botId = bot; taskId = task; decisionId = decision; messageId = message
    }
}

/// True for a team bot ID of the room: "t-", the room ID, "-", and the entry ID of the `bot` entry.
private func isTeamBotId(_ text: String, room: String) -> Bool {
    let prefix = "t-" + room + "-"
    return text.hasPrefix(prefix) && isEntryId(String(text.dropFirst(prefix.count)))
}

/// True for the ID of an entry of a team log: 32 lowercase hex characters, as a named chat has.
private func isEntryId(_ text: String) -> Bool {
    text.utf8.count == 32 && text.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

@MainActor @Observable
final class PushNotifications {
    static let shared = PushNotifications()
    var target: NotificationTarget?
    var authorization: UNAuthorizationStatus = .notDetermined
    var badgesAllowed = false
    @ObservationIgnored private var badgeTask: Task<Void, Never>?
    @ObservationIgnored private var removalTask: Task<Void, Never>?
    var deviceToken: String?
    var error: String?
    var notificationLevel = NotificationLevel.load() {
        didSet { UserDefaults.standard.set(notificationLevel.rawValue, forKey: "notificationLevel") }
    }
    var messageNotifications = UserDefaults.standard.object(forKey: "messageNotifications") as? Bool ?? true {
        didSet { UserDefaults.standard.set(messageNotifications, forKey: "messageNotifications") }
    }
    var preferences: NotificationPreferences {
        NotificationPreferences(level: notificationLevel, messages: messageNotifications)
    }
    private static let saved = NotificationSchedule.load()
    var outsideWorkHours = OutsideWorkHours.load() {
        didSet { UserDefaults.standard.set(outsideWorkHours.rawValue, forKey: OutsideWorkHours.key) }
    }
    var workStartMinutes = NotificationSchedule.minutes(fromClock: saved.start) ?? 9 * 60 {
        didSet { UserDefaults.standard.set(workStartMinutes, forKey: "workStartMinutes") }
    }
    var workEndMinutes = NotificationSchedule.minutes(fromClock: saved.end) ?? 18 * 60 {
        didSet { UserDefaults.standard.set(workEndMinutes, forKey: "workEndMinutes") }
    }
    var workTimeZone = saved.timezone {
        didSet { UserDefaults.standard.set(workTimeZone, forKey: "workTimeZone") }
    }
    /// Stored as ISO weekday numbers. A missing or unreadable value means every day.
    var workWeekdays = saved.weekdays {
        didSet { UserDefaults.standard.set(workWeekdays, forKey: "workWeekdays") }
    }
    var schedule: NotificationSchedule {
        NotificationSchedule(enabled: outsideWorkHours != .same, start: NotificationSchedule.clock(workStartMinutes),
                             end: NotificationSchedule.clock(workEndMinutes), timezone: workTimeZone,
                             weekdays: Array(Set(workWeekdays)).sorted())
    }

    /// Everything that the Mac must hear about. The app registers again when any part changes.
    var choices: PushChoices {
        PushChoices(preferences: preferences, schedule: schedule, outside: outsideWorkHours)
    }

    var allowed: Bool {
        #if os(iOS)
        [.authorized, .provisional, .ephemeral].contains(authorization)
        #else
        [.authorized, .provisional].contains(authorization)
        #endif
    }
    var environment: String { Bundle.main.object(forInfoDictionaryKey: "CoconutPushEnvironment") as? String ?? "development" }

    func refresh(requestPermission: Bool = false) async {
        let center = UNUserNotificationCenter.current()
        var settings = await center.notificationSettings()
        if requestPermission && settings.authorizationStatus == .notDetermined {
            do { _ = try await center.requestAuthorization(options: [.alert, .sound, .badge]) }
            catch { self.error = "iOS could not enable notifications. Try again in Settings." }
            settings = await center.notificationSettings()
        }
        // The app refreshes after each update. Unchanged values must not redraw the screens that read them.
        if authorization != settings.authorizationStatus { authorization = settings.authorizationStatus }
        if badgesAllowed != (settings.badgeSetting == .enabled) { badgesAllowed = settings.badgeSetting == .enabled }
        await PushRelayClient.shared.flushRevocations()
        #if os(iOS)
        // The push relay serves the iPhone app only. A Mac app shows local notifications while it runs.
        if allowed { UIApplication.shared.registerForRemoteNotifications() }
        #endif
    }

    func setBadgeCount(_ count: Int) {
        let previous = badgeTask
        badgeTask = Task {
            await previous?.value
            #if os(macOS)
            // The Dock tile of the Mac app shows the same number. Zero clears it.
            NSApp.dockTile.badgeLabel = count > 0 ? String(count) : nil
            #else
            try? await UNUserNotificationCenter.current().setBadgeCount(max(0, count))
            #endif
        }
    }

    #if os(iOS)
    /// Removes the delivered notifications of Coconut that `isRead` names (AppStore.notificationIsRead). One call at a
    /// time, so a removal never runs on a list that an earlier one changed.
    func removeDelivered(where isRead: @escaping (NotificationTarget) -> Bool) async {
        let previous = removalTask
        let task = Task {
            await previous?.value
            let center = UNUserNotificationCenter.current()
            let identifiers = await center.deliveredNotifications().compactMap { notification -> String? in
                guard let target = NotificationTarget(userInfo: notification.request.content.userInfo),
                      isRead(target) else { return nil }
                return notification.request.identifier
            }
            if !identifiers.isEmpty { center.removeDeliveredNotifications(withIdentifiers: identifiers) }
        }
        removalTask = task
        await task.value
    }
    #endif

    func registered(_ token: Data) {
        let value = token.map { String(format: "%02x", $0) }.joined()
        if deviceToken != value { deviceToken = value }
        if error != nil { error = nil }
    }

    func openSystemSettings() {
        #if os(iOS)
        if let url = URL(string: UIApplication.openNotificationSettingsURLString) { UIApplication.shared.open(url) }
        #else
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
        #endif
    }
}

#if os(iOS)
@MainActor
final class CoconutAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushNotifications.shared.registered(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushNotifications.shared.error = "iOS could not register for push notifications. Reopen Coconut to retry."
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping @Sendable (UNNotificationPresentationOptions) -> Void) {
        // Open conversations already receive updates from the Mac.
        Task { @MainActor in completionHandler([]) }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping @Sendable () -> Void) {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else {
            completionHandler()
            return
        }
        let target = NotificationTarget(userInfo: response.notification.request.content.userInfo)
        // UIKit updates its scene when this callback finishes. Complete on the main thread.
        Task { @MainActor in
            PushNotifications.shared.target = target
            completionHandler()
        }
    }
}
#endif

struct PushRegistration: Encodable {
    let token: String
    let environment: String
    let enabled: Bool
    var relay: PushRelayGrant? = nil
    var preferences: NotificationPreferences? = nil
    var schedule: NotificationSchedule? = nil
    /// "urgent" or "nothing". It is not in the schedule, because a Mac before this choice refuses unknown schedule fields.
    var outsideWorkHours: String? = nil

    /// Changes whenever something the Mac must hear about changes. An invalid enabled schedule is not
    /// sent, so it adds nothing; turning the schedule off always changes it.
    static func signature(deviceId: String, environment: String, token: String?, allowed: Bool,
                          preferences: NotificationPreferences, schedule: NotificationSchedule?,
                          outside: OutsideWorkHours = .urgent) -> String {
        let window = schedule.map { [String($0.enabled), $0.start, $0.end, $0.timezone, $0.weekdays.map(String.init).joined()].joined(separator: ",") } ?? ""
        return [deviceId, environment, token ?? "", String(allowed), preferences.level.rawValue, String(preferences.messages), window,
                outside.rawValue].joined(separator: ":")
    }
}

/// The last preview results of the notification extension and of the app (PreviewDiagnostics).
struct PreviewReports: Encodable, Equatable, Sendable {
    let `extension`: PreviewReport?
    let app: PreviewReport?
}

struct PreviewReportsResponse: Decodable, Sendable {
    let recorded: Bool
}

struct PushRegistrationResponse: Decodable, Sendable {
    let registered: Bool
    let configured: Bool
}
