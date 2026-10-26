import Foundation
import XCTest
@testable import Coconut

final class PairingSetupTests: XCTestCase {
    private let macKey = Data((0..<32).map { UInt8($0) })
    private let secret = Data((0..<32).map { UInt8(255 - $0) })

    func testVersionTwoLinkGivesTheTunnelEndpoint() throws {
        let setup = try PairingSetup.parse(link(try payload()))

        XCTAssertEqual(setup.relay.absoluteString, "https://coconut-push.gumroad.workers.dev")
        XCTAssertEqual(setup.route, String(repeating: "ab", count: 32))
        XCTAssertEqual(setup.macKey, macKey)
        XCTAssertEqual(setup.secret, secret)
        XCTAssertEqual(setup.code, base64URL(secret))
        XCTAssertEqual(setup.expires, Date(timeIntervalSince1970: 1_800_000_000))
        XCTAssertEqual(setup.name, "Mac mini")
        XCTAssertEqual(setup.endpoint.clientURL.absoluteString,
                       "wss://coconut-push.gumroad.workers.dev/v1/tunnels/\(setup.route)/client")
    }

    func testVersionOneLinkAsksForANewLink() throws {
        let legacy = try json(["version": 1, "server_url": "https://mac-mini.tail123.ts.net:8960",
                               "certificate_sha256": String(repeating: "a", count: 64), "pairing_code": "0123456789"])
        for input in [legacy, link(legacy)] {
            XCTAssertThrowsError(try PairingSetup.parse(input)) { error in
                XCTAssertEqual(error as? PairingSetupError, .legacyLink)
                XCTAssertEqual(error.localizedDescription, "Coconut no longer needs Tailscale. Ask your Hermes for a new pairing link.")
            }
        }
    }

    func testOtherVersionsAndBadVersionTypesAreRejected() throws {
        for version in [0, 4] as [Any] {
            XCTAssertThrowsError(try PairingSetup.parse(try payload(["version": version]))) { error in
                XCTAssertEqual(error as? PairingSetupError, .unsupportedVersion)
            }
        }
        for version in ["2", true, 2.5] as [Any] {
            XCTAssertThrowsError(try PairingSetup.parse(try payload(["version": version])), "\(version)")
        }
    }

    func testEveryFieldIsRequiredAndNoOtherFieldIsAllowed() throws {
        for key in ["relay", "route", "mac_key", "secret", "expires", "name"] {
            XCTAssertThrowsError(try PairingSetup.parse(try payload(removing: [key])), key)
        }
        XCTAssertThrowsError(try PairingSetup.parse(try payload(["server_url": "https://mac.local"])))
    }

    func testRelayMustBeAnHTTPSOrigin() throws {
        for relay in ["https://relay.example.com", "https://relay.example.com:8443"] {
            XCTAssertNoThrow(try PairingSetup.parse(try payload(["relay": relay])), relay)
        }
        for relay in ["http://relay.example.com", "https://relay.example.com/path", "https://user@relay.example.com",
                      "https://relay.example.com?x=1", "https://relay.example.com#x", "wss://relay.example.com",
                      "https://relay example.com", "https://", "relay.example.com", "http://127.0.0.2:8787"] {
            XCTAssertThrowsError(try PairingSetup.parse(try payload(["relay": relay])), relay)
        }
        #if DEBUG
        // The loopback test relay from scripts/test_tunnel_loopback.py.
        XCTAssertNoThrow(try PairingSetup.parse(try payload(["relay": "http://127.0.0.1:8787"])))
        #endif
    }

    func testRouteKeysAndNameAreChecked() throws {
        let bad: [(String, Any)] = [
            ("route", String(repeating: "AB", count: 32)), ("route", String(repeating: "a", count: 63)), ("route", 7),
            ("mac_key", base64URL(Data(count: 31))), ("mac_key", macKey.base64EncodedString()), ("secret", "a+b/"),
            ("secret", base64URL(Data(count: 33))), ("expires", "soon"), ("expires", 1.5),
            ("name", ""), ("name", "   "), ("name", String(repeating: "m", count: 81)), ("name", "Mac\nmini"),
        ]
        for (key, value) in bad {
            XCTAssertThrowsError(try PairingSetup.parse(try payload([key: value])), "\(key): \(value)")
        }
        XCTAssertEqual(try PairingSetup.parse(try payload(["name": "  Office Mac "])).name, "Office Mac")
    }

    func testLinkAndJSONGiveTheSameSetup() throws {
        let json = try payload()
        XCTAssertEqual(try PairingSetup.parse(link(json)), try PairingSetup.parse(json))
        XCTAssertEqual(try PairingSetup.parse("  " + link(json) + "\n"), try PairingSetup.parse(json))
        let padded = PairingSetup.linkPrefix + "setup=" + Data(json.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        XCTAssertEqual(try PairingSetup.parse(padded), try PairingSetup.parse(json))
        XCTAssertTrue(PairingSetup.isLink(try XCTUnwrap(URL(string: link(json)))))
    }

    func testRejectsMalformedSetupLinks() throws {
        for input in ["coconut://pair", "coconut://pair?setup=", "coconut://pair?setup=%%%", "coconut://other?setup=e30",
                      "coconut://pair?setup=e30&extra=1", "coconut://pair?setup=e30", "{}", "not json",
                      String(repeating: " ", count: 4_097)] {
            XCTAssertThrowsError(try PairingSetup.parse(input), input)
        }
        XCTAssertFalse(PairingSetup.isLink(try XCTUnwrap(URL(string: "https://pair?setup=e30"))))
    }

    // MARK: Version 3 links

    // From `scripts/pairing.py`, for the setup in `payload(["name": "Gianfranco’s Mac mini"])`.
    private let versionThreeLink = "coconut://pair/aov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2waabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi2dmob2hq7777p37h37l47r57w6x2ph4xr6dx653pm5pvot2hh43s6jy7c4hqgwsosaaacq2duoryhgorpf5rw6y3pnz2xillqovzwqlthovwxe33bmqxho33snnsxe4zomrsxmr3jmfxgm4tbnzrw7yuatfzsatlbmmqg22lone?setup=IHsidmVyc2lvbiI6Mywic2VydmVyX3VybCI6IiIsImNlcnRpZmljYXRlX3NoYTI1NiI6IiIsInBhaXJpbmdfY29kZSI6IiJ9"

    func testVersionThreeLinkGivesTheSameSetupAsTheEarlierLink() throws {
        let json = try payload(["name": "Gianfranco’s Mac mini"])
        let setup = try PairingSetup.parse(versionThreeLink)
        XCTAssertEqual(setup, try PairingSetup.parse(link(json)))
        XCTAssertEqual(setup.code, base64URL(secret))
        XCTAssertEqual(setup.name, "Gianfranco’s Mac mini")
        XCTAssertEqual(try PairingSetup.parse("  " + versionThreeLink + "\n"), setup)
        XCTAssertTrue(PairingSetup.isLink(try XCTUnwrap(URL(string: versionThreeLink))))
    }

    func testVersionThreeLinkHoldsTheLongestRelayHostName() throws {
        // From `scripts/pairing.py`: a host name of 253 characters and a port, longer than 255 bytes.
        let host = Array(repeating: String(repeating: "a", count: 63), count: 3).joined(separator: ".") + "." + String(repeating: "b", count: 61)
        let relay = "https://\(host):65535"
        let link = "coconut://pair/aov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2xk5lvov2waabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi2dmob2hq7777p37h37l47r57w6x2ph4xr6dx653pm5pvot2hh43s6jy7c4hqgwsosaaaqw2duoryhgorpf5qwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcyjomfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcltbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqwcylbmfqs4ytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeytcmjrgeyr2gy2tkmzvjvqwgidnnfxgs?setup=IHsidmVyc2lvbiI6Mywic2VydmVyX3VybCI6IiIsImNlcnRpZmljYXRlX3NoYTI1NiI6IiIsInBhaXJpbmdfY29kZSI6IiJ9"
        XCTAssertEqual(try PairingSetup.parse(link), try PairingSetup.parse(try payload(["relay": relay])))
    }

    func testEarlierBuildsReadAnUpdateRequestFromAVersionThreeLink() throws {
        // Builds before the version 3 link read only the setup field. Both kinds tell the person to update the app.
        let parts = try XCTUnwrap(URLComponents(string: versionThreeLink))
        let encoded = try XCTUnwrap(parts.queryItems?.first(where: { $0.name == "setup" })?.value)
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(Data(base64URL: encoded))) as? [String: Any])
        XCTAssertEqual(Set(fields.keys), ["version", "server_url", "certificate_sha256", "pairing_code"])
        XCTAssertEqual(fields["version"] as? Int, 3)
        XCTAssertFalse(encoded.hasPrefix("eyJ"))
    }

    func testDamagedVersionThreeLinksAreRefused() throws {
        let token = try XCTUnwrap(versionThreeLink.split(separator: "/").last?.split(separator: "?").first.map(String.init))
        let damaged = [
            versionThreeLink.replacingOccurrences(of: token, with: String(token.prefix(80))),
            versionThreeLink.replacingOccurrences(of: token, with: token.prefix(6) + "..." + token.suffix(4)),
            versionThreeLink.replacingOccurrences(of: token, with: token.uppercased()),
            versionThreeLink.replacingOccurrences(of: token, with: "b" + token.dropFirst()),
            versionThreeLink.replacingOccurrences(of: "/" + token, with: ""),
            "coconut://pair/a",
        ]
        for input in damaged {
            XCTAssertThrowsError(try PairingSetup.parse(input), input) { error in
                XCTAssertEqual(error as? PairingSetupError, .damagedLink)
                XCTAssertEqual(error.localizedDescription, "The pairing link is damaged. Ask your Hermes for a new pairing link.")
            }
        }
    }

    func testBase32IsStrict() {
        XCTAssertEqual(Data(base32: "mzxw6"), Data("foo".utf8))
        XCTAssertEqual(Data(base32: "mzxw6ytboi"), Data("foobar".utf8))
        for input in ["", "m", "mzx", "mzxw6y", "MZXW6", "mzxw7", "mzxw6="] {
            XCTAssertNil(Data(base32: input), input)
        }
    }

    // MARK: Saved pairings

    func testVersionOnePairingFileLoadsAsALegacyPairing() throws {
        // The phone keeps the pairing, so the app can ask for a new pairing link instead of starting over.
        let saved = Data(#"{"serverURL":"https://mac.tail1.ts.net:8960","fingerprint":"\#(String(repeating: "a", count: 64))","deviceId":"phone"}"#.utf8)
        let pairing = try JSONDecoder().decode(PairingConfiguration.self, from: saved)
        XCTAssertFalse(pairing.usesTunnel)
        XCTAssertTrue(pairing.isLegacy)
        XCTAssertNil(pairing.tunnelEndpoint)
        // A pending checkout recovery from before the relay still matches, so Settings keeps its warning.
        XCTAssertEqual(pairing.checkoutIdentity, CheckoutRecoveryPairing(deviceId: "phone", route: "https://mac.tail1.ts.net:8960",
                                                                         macKey: String(repeating: "a", count: 64)))
    }

    func testVersionTwoPairingRoundTripsAndNamesTheRouteAndKey() throws {
        let setup = try PairingSetup.parse(try payload())
        let pairing = PairingConfiguration(setup: setup, deviceId: "phone")
        let decoded = try JSONDecoder().decode(PairingConfiguration.self, from: try JSONEncoder().encode(pairing))
        XCTAssertEqual(decoded, pairing)
        XCTAssertTrue(decoded.usesTunnel)
        XCTAssertEqual(decoded.tunnelEndpoint, setup.endpoint)
        XCTAssertEqual(decoded.checkoutIdentity.route, setup.route)
        XCTAssertEqual(decoded.checkoutIdentity.macKey, setup.macKey.base64EncodedString())
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: try JSONEncoder().encode(pairing)) as? [String: Any])
        XCTAssertEqual(fields["version"] as? Int, 2)
        XCTAssertNil(fields["serverURL"])
        // An incomplete version 2 file is not a pairing.
        XCTAssertThrowsError(try JSONDecoder().decode(PairingConfiguration.self, from: Data(#"{"version":2,"deviceId":"phone"}"#.utf8)))
    }

    func testCheckoutConfirmationFromBeforeTheTunnelStillMatches() throws {
        let saved = Data(#"{"deviceId":"phone","serverURL":"https://mac:8960","fingerprint":"pin"}"#.utf8)
        let identity = try JSONDecoder().decode(CheckoutRecoveryPairing.self, from: saved)
        XCTAssertEqual(identity, CheckoutRecoveryPairing(deviceId: "phone", route: "https://mac:8960", macKey: "pin"))
        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: try JSONEncoder().encode(identity)) as? [String: String])
        XCTAssertEqual(encoded, ["deviceId": "phone", "route": "https://mac:8960", "macKey": "pin"])
    }

    // MARK: Helpers

    private func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private func link(_ json: String) -> String {
        PairingSetup.linkPrefix + "setup=" + base64URL(Data(json.utf8))
    }

    private func json(_ fields: [String: Any]) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]), as: UTF8.self)
    }

    private func payload(_ changes: [String: Any] = [:], removing keys: [String] = []) throws -> String {
        var fields: [String: Any] = [
            "version": 2, "relay": "https://coconut-push.gumroad.workers.dev", "route": String(repeating: "ab", count: 32),
            "mac_key": base64URL(macKey), "secret": base64URL(secret), "expires": 1_800_000_000, "name": "Mac mini",
        ]
        fields.merge(changes) { $1 }
        keys.forEach { fields.removeValue(forKey: $0) }
        return try json(fields)
    }
}
