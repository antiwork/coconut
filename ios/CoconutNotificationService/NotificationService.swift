import Foundation
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content); return
        }
        // Decryption uses local keys. It never contacts the Mac or waits for Tailscale.
        if let payload = try? NotificationKeys.content(from: request.content.userInfo),
           let aps = payload["aps"] as? [String: Any], let alert = aps["alert"] as? [String: String],
           let title = alert["title"], let body = alert["body"] {
            content.title = title
            content.subtitle = alert["subtitle"] ?? ""
            content.body = body
            if let thread = aps["thread-id"] as? String { content.threadIdentifier = thread }
            content.userInfo = payload
        }
        contentHandler(content)
    }
}
