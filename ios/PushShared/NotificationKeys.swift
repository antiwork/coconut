import Foundation
import Security
import CryptoKit

enum NotificationKeys {
    struct Binding: Codable, Sendable {
        let deviceId: String
        let privateKey: Data
        let senderKey: Data
        var key: Curve25519.KeyAgreement.PrivateKey { get throws { try .init(rawRepresentation: privateKey) } }
        var publicKey: Data { get throws { try key.publicKey.rawRepresentation } }
        var keyId: String { get throws { try PushEnvelope.keyID(key.publicKey) } }
    }

    static func binding(deviceId: String, senderKey: Data) throws -> Binding {
        let sender = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: senderKey)
        let current = try read(Binding.self, account: "pair:\(deviceId)")
        if let current, current.senderKey == senderKey { return current }
        let binding = Binding(deviceId: deviceId, privateKey: Curve25519.KeyAgreement.PrivateKey().rawRepresentation,
                              senderKey: sender.rawRepresentation)
        try save(binding, account: "key:\(binding.keyId)")
        try save(binding, account: "pair:\(deviceId)")
        if let previous = try current?.keyId { delete(account: "key:\(previous)") }
        return binding
    }

    static func forget(deviceId: String) {
        if let binding = try? read(Binding.self, account: "pair:\(deviceId)"), let keyId = try? binding.keyId {
            delete(account: "key:\(keyId)")
        }
        delete(account: "pair:\(deviceId)")
    }

    static func content(from userInfo: [AnyHashable: Any]) throws -> [String: Any]? {
        guard let value = userInfo["coconut_encrypted"] else { return nil }
        let envelope = try JSONDecoder().decode(PushEnvelope.self, from: JSONSerialization.data(withJSONObject: value))
        guard let binding = try read(Binding.self, account: "key:\(envelope.keyId)"),
              let current = try read(Binding.self, account: "pair:\(binding.deviceId)"),
              try current.keyId == envelope.keyId else { throw CocoaError(.coderValueNotFound) }
        let data = try envelope.open(recipient: binding.key,
                                     sender: Curve25519.KeyAgreement.PublicKey(rawRepresentation: binding.senderKey))
        guard let content = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let target = content["coconut"] as? [String: Any], target["device_id"] as? String == binding.deviceId else {
            throw CocoaError(.coderInvalidValue)
        }
        return content
    }

    static func read<T: Decodable>(_ type: T.Type, account: String) throws -> T? {
        var query = query(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw CocoaError(.fileReadNoPermission) }
        return try JSONDecoder().decode(type, from: data)
    }

    static func save<T: Encodable>(_ value: T, account: String) throws {
        let data = try JSONEncoder().encode(value)
        let attributes: [String: Any] = [kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        let base = query(account: account)
        let status = SecItemUpdate(base as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound,
              SecItemAdd(base.merging(attributes) { _, new in new } as CFDictionary, nil) == errSecSuccess else {
            throw CocoaError(.fileWriteNoPermission)
        }
    }

    static func delete(account: String) { SecItemDelete(query(account: account) as CFDictionary) }

    private static func query(account: String) -> [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "com.gumroad.coconut.push", kSecAttrAccount as String: account]
        if let group = Bundle.main.object(forInfoDictionaryKey: "CoconutPushKeychainGroup") as? String, !group.isEmpty {
            result[kSecAttrAccessGroup as String] = group
        }
        return result
    }
}
