import Foundation
import Observation
import UIKit
import UserNotifications
import SwiftUI

struct NotificationSettingsRow: View {
    var body: some View {
        NavigationLink("Notifications") { NotificationSettingsView() }
            .accessibilityIdentifier("notification-settings")
    }
}

struct NotificationSettingsView: View {
    @Environment(AppStore.self) private var store
    @Bindable private var push = PushNotifications.shared
    @State private var daysExpanded = false

    var body: some View {
        Form {
            Section {
                Picker("Notify me", selection: $push.notificationLevel) {
                    ForEach(NotificationLevel.allCases, id: \.self) { level in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(level.title)
                            Text(level.detail).font(.caption).foregroundStyle(.secondary)
                        }
                        .tag(level)
                        .accessibilityIdentifier("notification-level-" + level.rawValue)
                    }
                }
                .pickerStyle(.inline)
                .labelsHidden()
                .accessibilityIdentifier("notification-level")
                Toggle(isOn: $push.messageNotifications) {
                    Text("Messages and failed tasks").frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle()).onTapGesture { push.messageNotifications.toggle() }
                }.accessibilityIdentifier("message-notifications")
            } header: {
                Text("Notify me")
            } footer: {
                Text("A card that needs your attention now always notifies at once. A group opens Inbox.")
            }
            Section {
                Toggle("Use work hours", isOn: $push.afterHoursImportantOnly)
                    .accessibilityIdentifier("after-hours-toggle")
                if push.afterHoursImportantOnly {
                    DatePicker("Start", selection: minutes($push.workStartMinutes), displayedComponents: .hourAndMinute)
                        .accessibilityLabel("Work hours start")
                        .accessibilityIdentifier("work-hours-start")
                    DatePicker("End", selection: minutes($push.workEndMinutes), displayedComponents: .hourAndMinute)
                        .accessibilityLabel("Work hours end")
                        .accessibilityIdentifier("work-hours-end")
                    Picker("Time zone", selection: $push.workTimeZone) {
                        ForEach(TimeZone.knownTimeZoneIdentifiers, id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ")).tag($0) }
                    }
                    .pickerStyle(.navigationLink)
                    .accessibilityIdentifier("work-hours-timezone")
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
                    if push.workStartMinutes >= push.workEndMinutes {
                        InlineError("Start must be before end.")
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel("Work hours are not valid. Start must be before end.")
                            .accessibilityIdentifier("work-hours-error")
                    }
                }
            } header: {
                Text("Work hours")
            } footer: {
                Text("Outside work hours, cards wait in Inbox. When work hours start, one notification counts them.")
                    .accessibilityIdentifier("after-hours-footer")
            }
            Section("Delivery") {
                LabeledContent("Last notification", value: PushStatus.describe(store.pushStatus?.lastDeliveredAt) ?? "None yet")
                    .accessibilityIdentifier("last-notification")
                if let error = store.pushStatus?.lastError {
                    LabeledContent("Last error") {
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(PushStatus.explain(error))
                            if let at = PushStatus.describe(store.pushStatus?.lastErrorAt) {
                                Text(at).font(.caption).foregroundStyle(.secondary)
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
                    Text("App badges are off in iOS Settings.").font(.caption).foregroundStyle(.secondary)
                }
                if let error = store.pushRegistrationError ?? push.error {
                    InlineError(error)
                }
            }
        }
        .tint(BoardStyle.green)
        .settingsBackground()
        .environment(\.timeZone, TimeZone(identifier: "UTC") ?? .gmt)
        .navigationTitle("Notifications")
        .navigationBarTitleDisplayMode(.inline)
        .task { await store.refreshPushStatus() }
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

/// How often non-urgent items notify. The Mac applies it; see `push_schedule.py`.
enum NotificationLevel: String, Codable, CaseIterable, Sendable {
    case every
    case grouped
    case urgentOnly = "urgent_only"

    var title: String {
        switch self {
        case .every: "Every card"
        case .grouped: "Grouped"
        case .urgentOnly: "Only urgent"
        }
    }

    var detail: String {
        switch self {
        case .every: "One notification for each card during work hours."
        case .grouped: "At most one notification every 10 minutes."
        case .urgentOnly: "Only cards that need your attention now."
        }
    }

    /// The saved level. Before levels existed, turning decisions off meant only urgent cards.
    static func load(from defaults: UserDefaults = .standard) -> NotificationLevel {
        if let saved = defaults.string(forKey: "notificationLevel"), let level = NotificationLevel(rawValue: saved) { return level }
        return defaults.object(forKey: "decisionNotifications") as? Bool == false ? .urgentOnly : .grouped
    }
}

struct NotificationPreferences: Encodable, Equatable, Sendable {
    let level: NotificationLevel
    let messages: Bool
}

/// What the Mac last did for this iPhone. Times are ISO 8601 text from the Mac.
struct PushStatus: Decodable, Equatable, Sendable {
    let registered: Bool
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
        NotificationSchedule(enabled: defaults.object(forKey: "afterHoursImportantOnly") as? Bool ?? true,
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
}

struct NotificationTarget: Equatable, Sendable {
    let deviceId: String
    let botId: String
    let taskId: String
    let decisionId: String?
    let messageId: String?
    /// A group counts several items and names none of them.
    var group = false

    var conversationRoute: ConversationRoute { ConversationRoute(botId: botId, decisionId: decisionId, messageId: messageId) }

    func destination(pendingDecisionIds: Set<String>, snapshotReady: Bool) -> NotificationDestination? {
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
        if data["kind"] as? String == "group" {
            deviceId = device; botId = ""; taskId = ""; decisionId = nil; messageId = nil; group = true
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

@MainActor @Observable
final class PushNotifications {
    static let shared = PushNotifications()
    var target: NotificationTarget?
    var authorization: UNAuthorizationStatus = .notDetermined
    var badgesAllowed = false
    @ObservationIgnored private var badgeTask: Task<Void, Never>?
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
    var afterHoursImportantOnly = saved.enabled {
        didSet { UserDefaults.standard.set(afterHoursImportantOnly, forKey: "afterHoursImportantOnly") }
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
        NotificationSchedule(enabled: afterHoursImportantOnly, start: NotificationSchedule.clock(workStartMinutes),
                             end: NotificationSchedule.clock(workEndMinutes), timezone: workTimeZone,
                             weekdays: Array(Set(workWeekdays)).sorted())
    }

    var allowed: Bool { [.authorized, .provisional, .ephemeral].contains(authorization) }
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
        if allowed { UIApplication.shared.registerForRemoteNotifications() }
    }

    func setBadgeCount(_ count: Int) {
        let previous = badgeTask
        badgeTask = Task {
            await previous?.value
            try? await UNUserNotificationCenter.current().setBadgeCount(max(0, count))
        }
    }

    func registered(_ token: Data) {
        let value = token.map { String(format: "%02x", $0) }.joined()
        if deviceToken != value { deviceToken = value }
        if error != nil { error = nil }
    }

    func openSystemSettings() {
        if let url = URL(string: UIApplication.openNotificationSettingsURLString) { UIApplication.shared.open(url) }
    }
}

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

struct PushRegistration: Encodable {
    let token: String
    let environment: String
    let enabled: Bool
    var relay: PushRelayGrant? = nil
    var preferences: NotificationPreferences? = nil
    var schedule: NotificationSchedule? = nil

    /// Changes whenever something the Mac must hear about changes. An invalid enabled schedule is not
    /// sent, so it adds nothing; turning the schedule off always changes it.
    static func signature(deviceId: String, environment: String, token: String?, allowed: Bool,
                          preferences: NotificationPreferences, schedule: NotificationSchedule?) -> String {
        let window = schedule.map { [String($0.enabled), $0.start, $0.end, $0.timezone, $0.weekdays.map(String.init).joined()].joined(separator: ",") } ?? ""
        return [deviceId, environment, token ?? "", String(allowed), preferences.level.rawValue, String(preferences.messages), window].joined(separator: ":")
    }
}

struct PushRegistrationResponse: Decodable, Sendable {
    let registered: Bool
    let configured: Bool
}
