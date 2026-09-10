import Foundation
import CryptoKit

struct PushEnvelope: Codable, Sendable {
    let version: Int
    let id: String
    let keyId: String
    let expires: Int
    let encapsulatedKey: Data
    let ciphertext: Data

    static let suite = HPKE.Ciphersuite.Curve25519_SHA256_ChachaPoly

    static func keyID(_ key: Curve25519.KeyAgreement.PublicKey) -> String {
        SHA256.hash(data: key.rawRepresentation).map { String(format: "%02x", $0) }.joined()
    }

    private static func context(id: String, keyId: String, expires: Int) -> Data {
        Data("coconut-push-v1:\(id):\(keyId):\(expires)".utf8)
    }

    static func seal(_ content: Data, id: String, expires: Int,
                     recipient: Curve25519.KeyAgreement.PublicKey,
                     sender: Curve25519.KeyAgreement.PrivateKey) throws -> Self {
        guard UUID(uuidString: id) != nil, content.count <= 2200 else { throw CocoaError(.coderInvalidValue) }
        let keyId = keyID(recipient)
        let context = context(id: id, keyId: keyId, expires: expires)
        var sealer = try HPKE.Sender(recipientKey: recipient, ciphersuite: suite, info: context, authenticatedBy: sender)
        return try Self(version: 1, id: id, keyId: keyId, expires: expires,
                        encapsulatedKey: sealer.encapsulatedKey, ciphertext: sealer.seal(content, authenticating: context))
    }

    func open(recipient: Curve25519.KeyAgreement.PrivateKey,
              sender: Curve25519.KeyAgreement.PublicKey, now: Date = .now) throws -> Data {
        guard version == 1, UUID(uuidString: id) != nil,
              keyId == Self.keyID(recipient.publicKey), expires > Int(now.timeIntervalSince1970),
              encapsulatedKey.count == 32, ciphertext.count <= 2216 else { throw CocoaError(.coderInvalidValue) }
        let context = Self.context(id: id, keyId: keyId, expires: expires)
        var opener = try HPKE.Recipient(privateKey: recipient, ciphersuite: Self.suite, info: context,
                                       encapsulatedKey: encapsulatedKey, authenticatedBy: sender)
        return try opener.open(ciphertext, authenticating: context)
    }
}
