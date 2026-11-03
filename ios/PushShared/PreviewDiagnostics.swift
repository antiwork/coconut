import Foundation
import Security

/// The last result of a notification preview, which the app sends to the Mac. It holds a reason and times only:
/// no notification content and no key.
struct PreviewReport: Codable, Equatable, Sendable {
    /// Why the last preview failed, or nil when it worked.
    var reason: String?
    /// The first failure since the last preview that worked.
    var failingSince: Date?
    var failedAt: Date?
    var workedAt: Date?

    func recording(_ failure: String?, at now: Date) -> PreviewReport {
        var next = self
        if let failure {
            if reason == nil { next.failingSince = now }
            next.reason = String(failure.prefix(80))
            next.failedAt = now
        } else {
            next.reason = nil
            next.failingSince = nil
            next.workedAt = now
        }
        return next
    }
}

/// The notification extension records each preview here, and the app records its own key failures.
///
/// The items have no access group. The keychain then uses the first group of the entitlements of the writer: for the
/// extension that is the shared push group, so the app can read the item even when the group of NotificationKeys
/// (CoconutPushKeychainGroup) is wrong. A read without a group searches every group of the reader.
enum PreviewDiagnostics {
    enum Source: String { case `extension`, app }

    static let service = "com.gumroad.coconut.push.diagnostics"

    static func record(_ failure: String?, source: Source, now: Date = .now) {
        let current = read(source) ?? PreviewReport()
        // A preview that keeps working changes no state. Write it at most once an hour.
        if failure == nil, current.reason == nil, let worked = current.workedAt, now.timeIntervalSince(worked) < 3600 { return }
        guard let data = try? encoder.encode(current.recording(failure, at: now)) else { return }
        let base = query(source)
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        if SecItemUpdate(base as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
            SecItemAdd(base.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
    }

    static func read(_ source: Source) -> PreviewReport? {
        var query = query(source)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return try? decoder.decode(PreviewReport.self, from: data)
    }

    static func clear() {
        for source in [Source.extension, .app] { SecItemDelete(query(source) as CFDictionary) }
    }

    /// The reason of an error, without its content.
    static func reason(_ error: Error) -> String {
        if let failure = error as? NotificationKeys.Failure { return failure.reason }
        let error = error as NSError
        return "\(error.domain) \(error.code)"
    }

    private static func query(_ source: Source) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: TestIsolation.name(service),
         kSecAttrAccount as String: source.rawValue]
    }

    private static var encoder: JSONEncoder { let value = JSONEncoder(); value.dateEncodingStrategy = .iso8601; return value }
    private static var decoder: JSONDecoder { let value = JSONDecoder(); value.dateDecodingStrategy = .iso8601; return value }
}
