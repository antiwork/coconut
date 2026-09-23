import XCTest
import CryptoKit
import UserNotifications
@testable import Coconut

final class PushEnvelopeTests: XCTestCase {
    func testDecryptsEnvelopeProducedByMacMini() throws {
        struct Fixture: Decodable { let privateKey: Data; let senderKey: Data; let envelope: PushEnvelope }
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "MacEncryptedNotification", withExtension: "json"))
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        // The fixture uses a disposable phone key and synthetic text.
        let data = try fixture.envelope.open(recipient: .init(rawRepresentation: fixture.privateKey),
            sender: .init(rawRepresentation: fixture.senderKey),
            now: Date(timeIntervalSince1970: TimeInterval(fixture.envelope.expires - 1)))
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let aps = try XCTUnwrap(payload["aps"] as? [String: Any])
        let alert = try XCTUnwrap(aps["alert"] as? [String: String])
        XCTAssertEqual(alert["body"], "Three proposals need your decision.")
        XCTAssertNotNil(NotificationTarget(userInfo: payload))
    }

    func testPreviewAndRouteSurviveEncryption() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), mac = Curve25519.KeyAgreement.PrivateKey()
        let text = Data("{\"title\":\"Desk\",\"body\":\"Three proposals need your decision.\",\"task\":\"private-task\"}".utf8)
        let envelope = try PushEnvelope.seal(text, id: UUID().uuidString, expires: Int(Date.now.timeIntervalSince1970) + 600,
                                             recipient: phone.publicKey, sender: mac)
        let encoded = try JSONEncoder().encode(envelope)
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("Desk"))
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("private-task"))
        let restored = try JSONDecoder().decode(PushEnvelope.self, from: encoded)
        XCTAssertEqual(try restored.open(recipient: phone, sender: mac.publicKey), text)
    }

    func testEncryptedInboxBadgeReachesNotificationContentAndClears() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), mac = Curve25519.KeyAgreement.PrivateKey()
        let content = UNMutableNotificationContent()
        for count in [3, 0] {
            let payload: [String: Any] = ["aps": ["alert": ["title": "Desk", "body": "Update"], "badge": count]]
            let envelope = try PushEnvelope.seal(JSONSerialization.data(withJSONObject: payload),
                id: UUID().uuidString, expires: Int(Date.now.timeIntervalSince1970) + 600,
                recipient: phone.publicKey, sender: mac)
            let plain = try envelope.open(recipient: phone, sender: mac.publicKey)
            let decoded = try XCTUnwrap(JSONSerialization.jsonObject(with: plain) as? [String: Any])
            NotificationContent.apply(decoded, to: content)
            XCTAssertEqual(content.badge, NSNumber(value: count))
            XCTAssertEqual(content.body, "Update")
            XCTAssertFalse(String(decoding: try JSONEncoder().encode(envelope), as: UTF8.self).contains("badge"))
        }
    }

    func testAnotherPhoneCannotReadThePreview() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), mac = Curve25519.KeyAgreement.PrivateKey()
        let envelope = try PushEnvelope.seal(Data("Private message".utf8), id: UUID().uuidString,
            expires: Int(Date.now.timeIntervalSince1970) + 600, recipient: phone.publicKey, sender: mac)
        XCTAssertThrowsError(try envelope.open(recipient: .init(), sender: mac.publicKey))
    }

    func testRelayCannotForgeMessageFromPairedMac() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), pairedMac = Curve25519.KeyAgreement.PrivateKey()
        let attacker = Curve25519.KeyAgreement.PrivateKey()
        let forged = try PushEnvelope.seal(Data("Approve this forged action".utf8), id: UUID().uuidString,
            expires: Int(Date.now.timeIntervalSince1970) + 600, recipient: phone.publicKey, sender: attacker)
        XCTAssertThrowsError(try forged.open(recipient: phone, sender: pairedMac.publicKey))
    }

    func testModifiedIDOrExpiryFailsAuthentication() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), mac = Curve25519.KeyAgreement.PrivateKey()
        let envelope = try PushEnvelope.seal(Data("Private message".utf8), id: UUID().uuidString,
            expires: Int(Date.now.timeIntervalSince1970) + 600, recipient: phone.publicKey, sender: mac)
        let changed = PushEnvelope(version: 1, id: UUID().uuidString, keyId: envelope.keyId,
            expires: envelope.expires, encapsulatedKey: envelope.encapsulatedKey, ciphertext: envelope.ciphertext)
        XCTAssertThrowsError(try changed.open(recipient: phone, sender: mac.publicKey))
        XCTAssertThrowsError(try envelope.open(recipient: phone, sender: mac.publicKey,
                                               now: Date(timeIntervalSince1970: TimeInterval(envelope.expires))))
    }

    func testPayloadFitsAppleLimitWithUnicode() throws {
        let phone = Curve25519.KeyAgreement.PrivateKey(), mac = Curve25519.KeyAgreement.PrivateKey()
        let envelope = try PushEnvelope.seal(Data(String(repeating: "🥥", count: 550).utf8), id: UUID().uuidString,
            expires: Int(Date.now.timeIntervalSince1970) + 600, recipient: phone.publicKey, sender: mac)
        let value = try JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope))
        let payload = try JSONSerialization.data(withJSONObject: ["aps": ["alert": ["title": "Coconut", "body": "Open Coconut to read this update."],
            "mutable-content": 1, "sound": "default", "thread-id": String(repeating: "a", count: 64)], "coconut_encrypted": value])
        XCTAssertLessThan(payload.count, 4096)
        XCTAssertThrowsError(try PushEnvelope.seal(Data(repeating: 1, count: 2201), id: UUID().uuidString,
            expires: envelope.expires, recipient: phone.publicKey, sender: mac))
    }

    func testCorruptEncryptedNotificationCannotUseUnverifiedRoutingFields() {
        let target = NotificationTarget(userInfo: ["coconut_encrypted": ["version": 9],
            "coconut": ["version": 1, "device_id": UUID().uuidString, "bot_id": UUID().uuidString, "task_id": UUID().uuidString]])
        XCTAssertNil(target)
    }
}
