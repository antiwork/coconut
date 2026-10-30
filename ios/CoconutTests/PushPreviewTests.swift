import XCTest
import CryptoKit
import UserNotifications
@testable import Coconut

/// The path of a preview from the Mac to the notification extension. MacRelayNotification.json comes from
/// scripts/push-envelope.swift as push-encrypt runs it: backend/coconut/push_relay.py RelayTransport.seal with a
/// disposable sender key, a disposable phone key and synthetic text.
final class PushPreviewTests: XCTestCase {
    struct Fixture: Decodable {
        let deviceId: String
        let privateKey: Data
        let senderKey: Data
        let envelope: PushEnvelope
    }

    private var fixture: Fixture!
    private var userInfo: [AnyHashable: Any]!

    override func setUpWithError() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "MacRelayNotification", withExtension: "json"))
        let data = try Data(contentsOf: url)
        fixture = try JSONDecoder().decode(Fixture.self, from: data)
        let envelope = try XCTUnwrap((JSONSerialization.jsonObject(with: data) as? [String: Any])?["envelope"])
        // The payload that relay/src/apns.js sends to APNs.
        userInfo = ["aps": ["alert": ["title": "Coconut", "body": "Open Coconut to read this update."],
                            "mutable-content": 1, "sound": "default", "thread-id": String(repeating: "a", count: 64)],
                    "coconut_encrypted": envelope]
        clear()
    }

    override func tearDown() { clear() }

    private func clear() {
        guard let fixture else { return }
        NotificationKeys.delete(account: "key:\(fixture.envelope.keyId)")
        NotificationKeys.delete(account: "pair:\(fixture.deviceId)")
        PreviewDiagnostics.clear()
    }

    private func saveBinding() throws {
        let binding = NotificationKeys.Binding(deviceId: fixture.deviceId, privateKey: fixture.privateKey, senderKey: fixture.senderKey)
        XCTAssertEqual(try binding.keyId, fixture.envelope.keyId)
        try NotificationKeys.save(binding, account: "key:\(binding.keyId)")
        try NotificationKeys.save(binding, account: "pair:\(binding.deviceId)")
    }

    func testTheExtensionShowsThePreviewThatTheMacEncrypted() throws {
        try saveBinding()
        let payload = try XCTUnwrap(NotificationKeys.content(from: userInfo))
        let content = UNMutableNotificationContent()
        content.title = "Coconut"; content.body = "Open Coconut to read this update."
        XCTAssertTrue(NotificationContent.apply(payload, to: content))
        XCTAssertEqual(content.title, "Desk")
        XCTAssertEqual(content.body, "Three proposals need your decision.")
        XCTAssertEqual(content.badge, 3)
        XCTAssertEqual(NotificationTarget(userInfo: content.userInfo)?.deviceId, fixture.deviceId)
    }

    func testEachFailureHasAReasonWithoutContent() throws {
        XCTAssertThrowsError(try NotificationKeys.content(from: userInfo)) {
            XCTAssertEqual(PreviewDiagnostics.reason($0), "no key for this envelope")
        }
        XCTAssertThrowsError(try NotificationKeys.content(from: ["coconut_encrypted": "not an object"])) {
            XCTAssertEqual(PreviewDiagnostics.reason($0), "envelope does not decode")
        }
        try saveBinding()
        XCTAssertThrowsError(try NotificationKeys.content(from: userInfo, now: Date(timeIntervalSince1970: 4_102_444_800))) {
            XCTAssertEqual(PreviewDiagnostics.reason($0), "envelope expired")
        }
        let other = NotificationKeys.Binding(deviceId: fixture.deviceId,
                                             privateKey: Curve25519.KeyAgreement.PrivateKey().rawRepresentation, senderKey: fixture.senderKey)
        try NotificationKeys.save(other, account: "pair:\(fixture.deviceId)")
        XCTAssertThrowsError(try NotificationKeys.content(from: userInfo)) {
            XCTAssertEqual(PreviewDiagnostics.reason($0), "the pairing has a newer key")
        }
        XCTAssertNil(try NotificationKeys.content(from: ["aps": [:]]))
    }

    func testTheReportKeepsTheStartOfAFailureAndClearsItWhenAPreviewWorks() throws {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        PreviewDiagnostics.record("keychain read -34018", source: .extension, now: start)
        PreviewDiagnostics.record("keychain read -34018", source: .extension, now: start + 60)
        var report = try XCTUnwrap(PreviewDiagnostics.read(.extension))
        XCTAssertEqual(report.reason, "keychain read -34018")
        XCTAssertEqual(report.failingSince, start)
        XCTAssertEqual(report.failedAt, start + 60)
        PreviewDiagnostics.record(nil, source: .extension, now: start + 120)
        report = try XCTUnwrap(PreviewDiagnostics.read(.extension))
        XCTAssertNil(report.reason)
        XCTAssertNil(report.failingSince)
        XCTAssertEqual(report.workedAt, start + 120)
        XCTAssertNil(PreviewDiagnostics.read(.app))

        let encoder = APIJSON.encoder()
        encoder.dateEncodingStrategy = .iso8601
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: encoder.encode(PreviewReports(extension: report, app: nil))) as? [String: Any])
        XCTAssertEqual(body as NSDictionary, ["extension": ["failed_at": "2027-01-15T08:01:00Z", "worked_at": "2027-01-15T08:02:00Z"]])
    }
}
