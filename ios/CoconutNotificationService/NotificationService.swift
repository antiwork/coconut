import Foundation
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content); return
        }
        // Decryption uses local keys. It never contacts the Mac.
        if let payload = try? NotificationKeys.content(from: request.content.userInfo) {
            NotificationContent.apply(payload, to: content)
        }
        contentHandler(content)
    }
}
