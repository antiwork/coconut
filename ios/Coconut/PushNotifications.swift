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

    var body: some View {
        Form {
            Section {
                Toggle(isOn: $push.decisionNotifications) {
                    Text("Decisions").frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle()).onTapGesture { push.decisionNotifications.toggle() }
                }.accessibilityIdentifier("decision-notifications")
                Toggle(isOn: $push.messageNotifications) {
                    Text("Messages").frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle()).onTapGesture { push.messageNotifications.toggle() }
                }.accessibilityIdentifier("message-notifications")
            } footer: {
                Text("Decisions open the card in Inbox. Messages open the bot’s chat.")
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
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .tint(BoardStyle.green)
        .navigationTitle("Notifications")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct NotificationPreferences: Encodable, Equatable, Sendable {
    let decisions: Bool
    let messages: Bool
}

enum NotificationDestination: Equatable {
    case inbox(String)
    case conversation(ConversationRoute)
}

struct NotificationTarget: Equatable, Sendable {
    let deviceId: String
    let botId: String
    let taskId: String
    let decisionId: String?
    let messageId: String?

    var conversationRoute: ConversationRoute { ConversationRoute(botId: botId, decisionId: decisionId, messageId: messageId) }

    func destination(pendingDecisionIds: Set<String>, snapshotReady: Bool) -> NotificationDestination? {
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
              let device = data["device_id"] as? String, UUID(uuidString: device) != nil,
              let bot = data["bot_id"] as? String, UUID(uuidString: bot) != nil,
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
    var decisionNotifications = UserDefaults.standard.object(forKey: "decisionNotifications") as? Bool ?? true {
        didSet { UserDefaults.standard.set(decisionNotifications, forKey: "decisionNotifications") }
    }
    var messageNotifications = UserDefaults.standard.object(forKey: "messageNotifications") as? Bool ?? true {
        didSet { UserDefaults.standard.set(messageNotifications, forKey: "messageNotifications") }
    }
    var preferences: NotificationPreferences {
        NotificationPreferences(decisions: decisionNotifications, messages: messageNotifications)
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
        authorization = settings.authorizationStatus
        badgesAllowed = settings.badgeSetting == .enabled
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
        deviceToken = token.map { String(format: "%02x", $0) }.joined()
        error = nil
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
}

struct PushRegistrationResponse: Decodable, Sendable {
    let registered: Bool
    let configured: Bool
}
