import AppKit
import SwiftUI
import UserNotifications

/// The banners of macOS for a new team message and a new direct message. MessageBanners makes each decision. This class
/// only gives them to macOS, asks for permission, and opens the chat of a banner that the person clicks. Only a full
/// install starts it: a Mac for a team only has no chat client.
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

    init(model: AppModel, store: AppStore) {
        self.model = model
        self.store = store
    }

    /// Start before the store starts, so the store gives each list of the teams to the banners. macOS gives a click
    /// that started the app to the delegate that the app sets before the launch ends.
    func start() {
        center.delegate = self
        store.teamListLoaded = { [weak self] before, after in
            // A list that changes nothing changes no banner.
            guard before != after else { return }
            self?.apply(MessageBanners.plan(before: before, after: after))
        }
    }

    private func apply(_ plan: MessageBannerPlan) {
        let asks = plan.asksPermission && !askedForPermission
        if asks { askedForPermission = true }
        guard asks || !plan.remove.isEmpty || !plan.post.isEmpty else { return }
        let previous = lastChange
        lastChange = Task {
            await previous?.value
            if !plan.remove.isEmpty { center.removeDeliveredNotifications(withIdentifiers: plan.remove) }
            guard asks || !plan.post.isEmpty, await allowed(asking: asks) else { return }
            for banner in plan.post { try? await center.add(Self.request(banner)) }
        }
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

    /// The request of a banner. The identifier is the chat, so a newer banner of the chat takes the place of the older
    /// one.
    private static func request(_ banner: MessageBanner) -> UNNotificationRequest {
        let content = UNMutableNotificationContent()
        content.title = banner.title
        if let subtitle = banner.subtitle { content.subtitle = subtitle }
        content.body = banner.body
        content.sound = .default
        content.threadIdentifier = banner.thread
        content.userInfo = MessageBanners.userInfo(banner.chat)
        return UNNotificationRequest(identifier: banner.identifier, content: content, trigger: nil)
    }

    /// A click on a banner: the app comes to the front, the main window opens, and it shows the chat while the app has
    /// it. The Team screen of the Settings window opens a direct chat on the same path (AppModel.shownChat).
    private func open(_ chat: TeamChatKey?) {
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
        let chat = MessageBanners.chat(userInfo: response.notification.request.content.userInfo)
        Task { @MainActor in
            self.open(chat)
            completionHandler()
        }
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
