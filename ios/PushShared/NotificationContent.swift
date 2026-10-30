import Foundation
import UserNotifications

// The extension applies only content authenticated by NotificationKeys.
enum NotificationContent {
    /// Returns false, and changes nothing, when the payload has no alert with a title and a body.
    @discardableResult
    static func apply(_ payload: [String: Any], to content: UNMutableNotificationContent) -> Bool {
        guard let aps = payload["aps"] as? [String: Any],
              let alert = aps["alert"] as? [String: String],
              let title = alert["title"], let body = alert["body"] else { return false }
        content.title = title
        content.subtitle = alert["subtitle"] ?? ""
        content.body = body
        if let thread = aps["thread-id"] as? String { content.threadIdentifier = thread }
        if let badge = aps["badge"] as? Int, badge >= 0 { content.badge = NSNumber(value: badge) }
        content.userInfo = payload
        return true
    }
}
