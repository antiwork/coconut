import Foundation
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var content: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content); return
        }
        self.contentHandler = contentHandler
        self.content = content
        // Decryption uses local keys. It never contacts the Mac.
        do {
            if let payload = try NotificationKeys.content(from: request.content.userInfo) {
                let applied = NotificationContent.apply(payload, to: content)
                PreviewDiagnostics.record(applied ? nil : "content has no title and body", source: .extension)
            }
        } catch {
            PreviewDiagnostics.record(PreviewDiagnostics.reason(error), source: .extension)
        }
        finish()
    }

    override func serviceExtensionTimeWillExpire() {
        PreviewDiagnostics.record("the extension ran out of time", source: .extension)
        finish()
    }

    private func finish() {
        guard let contentHandler, let content else { return }
        self.contentHandler = nil
        contentHandler(content)
    }
}
