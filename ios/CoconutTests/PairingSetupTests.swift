import Foundation
import XCTest
@testable import Coconut

final class PairingSetupTests: XCTestCase {
    private let fingerprint = String(repeating: "a", count: 64)

    func testValidPayloadPreservesOriginAndLeadingZeros() throws {
        let setup = try PairingSetup.parse(payload())

        XCTAssertEqual(setup.serverURL, "https://mac-mini.local:8765")
        XCTAssertEqual(setup.certificateSHA256, fingerprint)
        XCTAssertEqual(setup.pairingCode, "0123456789")
    }

    func testFingerprintUsesLowercaseWithoutChangingOtherFields() throws {
        let mixed = String(repeating: "aBcDeF09", count: 8)
        let setup = try PairingSetup.parse(payload(["certificate_sha256": mixed]))
        let normalized = try PairingSetup.parse(payload(["certificate_sha256": mixed.lowercased()]))

        XCTAssertEqual(setup.certificateSHA256, mixed.lowercased())
        XCTAssertEqual(setup, normalized)
    }

    func testAcceptsLocalNetworkAndTailscaleOrigins() throws {
        let origins = [
            "https://mac-mini.local", "https://mac-mini.local:8765/",
            "https://mac-mini.tail123.ts.net:8765", "https://192.168.1.20:8765",
            "https://100.72.5.14:8765", "https://127.0.0.1:1",
            "https://[::1]:65535", "https://[fd7a:115c:a1e0::42]:8765"
        ]

        for origin in origins {
            XCTAssertNoThrow(try PairingSetup.parse(payload(["server_url": origin])), origin)
        }
    }

    func testRejectsMalformedJSONAndNonobjectRoots() {
        for input in ["", "{", "not json", "[]", "null", "true", "1", "\"pairing\""] {
            XCTAssertThrowsError(try PairingSetup.parse(input), input)
        }
    }

    func testRequiresEveryField() throws {
        for key in ["version", "server_url", "certificate_sha256", "pairing_code"] {
            XCTAssertThrowsError(try PairingSetup.parse(payload(removing: [key])), key)
        }
    }

    func testRejectsUnknownFieldsIncludingCredentialsAndModelSettings() throws {
        for key in ["extra", "token", "device_token", "api_key", "model", "model_name", "base_url"] {
            XCTAssertThrowsError(try PairingSetup.parse(payload([key: "unexpected"])), key)
        }
    }

    func testRejectsUnknownAndNonintegerVersions() throws {
        let versions: [Any] = [0, 2, -1, 1.5, "1", true, false, NSNull(), [], [:]]
        for version in versions {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["version": version])), "\(version)")
        }
    }

    func testRequiresStringFieldsWithoutCoercion() throws {
        let values: [Any] = [123, true, NSNull(), [], [:]]
        for key in ["server_url", "certificate_sha256", "pairing_code"] {
            for value in values {
                XCTAssertThrowsError(try PairingSetup.parse(payload([key: value])), "\(key): \(value)")
            }
        }
    }

    func testAcceptsPayloadAtByteLimit() throws {
        let input = try payload()
        let padded = input + String(repeating: " ", count: 4096 - input.utf8.count)

        XCTAssertEqual(padded.utf8.count, 4096)
        XCTAssertNoThrow(try PairingSetup.parse(padded))
    }

    func testRejectsPayloadAboveByteLimitBeforeTrimming() throws {
        let input = try payload()
        let padded = input + String(repeating: " ", count: 4097 - input.utf8.count)

        XCTAssertEqual(padded.utf8.count, 4097)
        XCTAssertThrowsError(try PairingSetup.parse(padded))
    }

    func testRequiresHTTPSWithAHost() throws {
        for origin in ["http://mac-mini.local:8765", "ftp://mac-mini.local", "mac-mini.local:8765",
                       "//mac-mini.local:8765", "https://", "https:///", "https:mac-mini.local"] {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), origin)
        }
    }

    func testRejectsUserInformationQueriesAndFragments() throws {
        let origins = [
            "https://user@mac-mini.local", "https://user:password@mac-mini.local",
            "https://@mac-mini.local", "https://mac-mini.local?token=secret",
            "https://mac-mini.local?", "https://mac-mini.local#section", "https://mac-mini.local#"
        ]

        for origin in origins {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), origin)
        }
    }

    func testRejectsPathsBeyondTheOriginSlash() throws {
        for path in ["/v1", "//", "/./", "/../", "/pair"] {
            let origin = "https://mac-mini.local:8765" + path
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), path)
        }
    }

    func testRejectsURLWhitespaceAndBackslashes() throws {
        let origins = [
            " https://mac-mini.local", "https://mac-mini.local ", "https://mac mini.local",
            "https://mac-mini.local\n", "https://mac-mini.local\t", "https://mac-mini.local\u{00A0}",
            "https://mac-mini.local\\", "https:\\mac-mini.local", "https://mac-mini.local\\@other.local"
        ]

        for origin in origins {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), origin)
        }
    }

    func testRejectsPercentEncodingInEveryURLComponent() throws {
        let origins = [
            "https://%6dac-mini.local", "https://mac-mini%2elocal", "https://mac-mini.local/%2F",
            "https://mac-mini.local/%2e", "https://[fe80::1%25en0]:8765"
        ]

        for origin in origins {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), origin)
        }
    }

    func testRequiresPortsWithinTheValidRange() throws {
        for port in ["0", "65536", "999999", "-1", "abc"] {
            let origin = "https://mac-mini.local:" + port
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": origin])), port)
        }
    }

    func testRejectsInvalidDNSLabels() throws {
        for host in ["-mac.local", "mac-.local", "mac._local", "mac..local", ".mac.local", "mác.local"] {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["server_url": "https://" + host])), host)
        }
    }

    func testRequiresExactly64FingerprintCharacters() throws {
        for count in [0, 1, 63, 65] {
            XCTAssertThrowsError(
                try PairingSetup.parse(payload(["certificate_sha256": String(repeating: "a", count: count)])),
                "Length \(count)"
            )
        }
    }

    func testRequiresASCIIHexFingerprintWithoutSeparators() throws {
        let invalid = [
            String(repeating: "g", count: 64), String(repeating: "Ａ", count: 64),
            String(repeating: "a", count: 63) + " ", String(repeating: "a", count: 63) + ":",
            String(repeating: "a", count: 63) + "\n", " " + fingerprint
        ]

        for value in invalid {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["certificate_sha256": value])))
        }
    }

    func testRequiresExactly10PairingCodeDigits() throws {
        for code in ["", "012345678", "01234567890", " 0123456789", "0123456789 "] {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["pairing_code": code])), code)
        }
    }

    func testRequiresASCIIPairingCodeDigits() throws {
        for code in ["０１２３４５６７８９", "٠١٢٣٤٥٦٧٨٩", "01234a6789", "01234-6789", "012345678\n"] {
            XCTAssertThrowsError(try PairingSetup.parse(payload(["pairing_code": code])), code)
        }
    }

    func testNewDraftStartsEmpty() {
        let draft = PairingSetupDraft()

        XCTAssertEqual(draft.serverURL, "")
        XCTAssertEqual(draft.fingerprint, "")
        XCTAssertEqual(draft.pairCode, "")
    }

    func testValidImportReplacesAllDraftFieldsTogether() throws {
        var draft = PairingSetupDraft(serverURL: "https://old.local", fingerprint: "old pin", pairCode: "old code")
        try draft.importPayload(payload(["certificate_sha256": fingerprint.uppercased()]))

        XCTAssertEqual(draft.serverURL, "https://mac-mini.local:8765")
        XCTAssertEqual(draft.fingerprint, fingerprint)
        XCTAssertEqual(draft.pairCode, "0123456789")
    }

    func testStagingNewSetupDoesNotChangeTheOriginalDraft() throws {
        let original = PairingSetupDraft(
            serverURL: "https://old.local", fingerprint: String(repeating: "b", count: 64), pairCode: "9876543210"
        )
        var staging = original
        try staging.importPayload(payload())

        XCTAssertEqual(original.serverURL, "https://old.local")
        XCTAssertEqual(original.fingerprint, String(repeating: "b", count: 64))
        XCTAssertEqual(original.pairCode, "9876543210")
        XCTAssertEqual(staging.serverURL, "https://mac-mini.local:8765")
        XCTAssertEqual(staging.fingerprint, fingerprint)
        XCTAssertEqual(staging.pairCode, "0123456789")
    }

    func testEveryInvalidImportPreservesAllDraftFields() throws {
        let original = PairingSetupDraft(
            serverURL: "https://old.local", fingerprint: String(repeating: "b", count: 64), pairCode: "9876543210"
        )
        let inputs = try [
            payload(["version": 2]), payload(["server_url": "http://new.local"]),
            payload(["certificate_sha256": "bad pin"]), payload(["pairing_code": "bad code"]),
            payload(["token": "unexpected"]), "{"
        ]

        for input in inputs {
            var draft = original
            XCTAssertThrowsError(try draft.importPayload(input))
            XCTAssertEqual(draft.serverURL, original.serverURL)
            XCTAssertEqual(draft.fingerprint, original.fingerprint)
            XCTAssertEqual(draft.pairCode, original.pairCode)
        }
    }

    func testInvalidImportKeepsTheLastValidImportedSetup() throws {
        var draft = PairingSetupDraft()
        try draft.importPayload(payload())

        XCTAssertThrowsError(try draft.importPayload(payload([
            "server_url": "https://replacement.local", "certificate_sha256": String(repeating: "b", count: 64),
            "pairing_code": "invalid"
        ])))
        XCTAssertEqual(draft.serverURL, "https://mac-mini.local:8765")
        XCTAssertEqual(draft.fingerprint, fingerprint)
        XCTAssertEqual(draft.pairCode, "0123456789")
    }

    private func payload(_ changes: [String: Any] = [:], removing keys: [String] = []) throws -> String {
        var fields: [String: Any] = [
            "version": 1, "server_url": "https://mac-mini.local:8765",
            "certificate_sha256": fingerprint, "pairing_code": "0123456789"
        ]
        fields.merge(changes) { _, new in new }
        for key in keys { fields.removeValue(forKey: key) }
        let data = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
        return try XCTUnwrap(String(data: data, encoding: .utf8))
    }
}
