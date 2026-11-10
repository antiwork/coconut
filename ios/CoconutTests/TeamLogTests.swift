import CryptoKit
import Foundation
import XCTest
@testable import Coconut

/// The test vectors of docs/team-log.md. Node made them with deterministic Ed25519 signatures (RFC 8032), so each
/// value here is one that another implementation must also get.
final class TeamLogTests: XCTestCase {
    private func range(_ first: UInt8, _ count: Int) -> Data { Data((0..<count).map { first + UInt8($0) }) }
    private var secret: Data { range(0x00, 32) }
    private var room: Data { range(0x20, 32) }
    private var entry: Data { range(0x40, 16) }
    private var seed: Data { range(0x50, 32) }
    private var roomKey: Data { range(0x70, 32) }
    private var key: SymmetricKey { SymmetricKey(data: roomKey) }
    private let payload = Data(#"{"kind":"member","rev":1,"name":"Ada"}"#.utf8)
    private let memberID = TeamLog.bytes(hex: "3f7708d5f5cc2bc633b59d2b3a2ed92e7479220c6f08ade208bebcd8580ab93b")!
    private let body = TeamLog.bytes(hex: "0100000000909192939495969798999a9b3dc6466e523666ef6e740edf8ec9e63d7cb603f63968e207c181b8a81597764cc292af10ed9bd3b58b46badbf645e0b0a2bb6315ccba94c7b089151d14927bf4494f5ccec298f0d72467ae0d11cca33a42029f69407325375b2dc8da1e906baa85545c47a14ef9ab7c4a29555af5b8ffd519c73fd373")!
    private let welcomeText = #"{"version":1,"epoch":0,"key":"cHFyc3R1dnd4eXp7fH1-f4CBgoOEhYaHiImKi4yNjo8","admin":"3f7708d5f5cc2bc633b59d2b3a2ed92e7479220c6f08ade208bebcd8580ab93b","name":"Example Team"}"#
    private let welcome = TeamLog.bytes(hex: "a0a1a2a3a4a5a6a7a8a9aaabda8922b84e01f1b512a2cf53cff6685c32a45a60612033abab0e79079016c0888927292eeeb3cc2d461f9c01fffdba1769b9351e70d02f7fb5d2111569322ca3b369c82d5c3fc250b9dac19e1b908c7bb52d8528191575c5337909dedb65a86dddba2d39407cce1a90e75318e7825291e5f0543b4085f777e121daa5157c498170b6cee22dc9d7c8f4444acd196b02877565f016aefc07ba26506462f1ae40e555299323d31f25e0ef44f5563707469473e4f402151b7e88aff20ed3")!
    private let link = "coconut://team/aeqccirdeqssmjzifevcwlbnfyxtamjsgm2dknrxha4tuoz4hu7d6aabaibqibiga4eascqlbqgq4dyqcejbgfavcylrqgi2dmob2hq7nne5eaaacvuhi5dqom5c6l3smvwgc6jomv4gc3lqnrsuk6dbnvygyzjakrswc3i"

    private func member() throws -> Curve25519.Signing.PrivateKey { try Curve25519.Signing.PrivateKey(rawRepresentation: seed) }
    /// The welcome key, as the contract derives it. The tests use it to seal a welcome with their own plaintext.
    private var welcomeKey: SymmetricKey {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: secret), info: Data("coconut room welcome".utf8), outputByteCount: 32)
    }
    /// A link with these parts, packed as the contract says, also when a part is not valid.
    private func packedLink(version: UInt8 = 1, relay: String = "https://relay.example", name: Data = Data("Example Team".utf8)) -> String {
        let relayBytes = Data(relay.utf8)
        let packed = Data([version]) + room + secret + Data([0x6b, 0x49, 0xd2, 0x00])
            + Data([UInt8(relayBytes.count >> 8), UInt8(relayBytes.count & 0xff)]) + relayBytes + name
        return "coconut://team/" + packed.base32String
    }
    private func open(_ body: Data, room: Data? = nil, entry: Data? = nil, member: Data? = nil,
                      key: ((UInt32) -> SymmetricKey?)? = nil) throws -> (epoch: UInt32, payload: Data) {
        try TeamLog.open(body, room: room ?? self.room, entry: entry ?? self.entry, member: member ?? memberID,
                         key: key ?? { _ in self.key })
    }
    private func failure(_ work: () throws -> Void) -> TeamLog.Failure? {
        do { try work(); return nil } catch { return error as? TeamLog.Failure }
    }
    private func changed(_ data: Data, at index: Int) -> Data {
        var data = data
        data[index] ^= 1
        return data
    }

    func testTheValuesOfAnInviteComeFromItsSecret() throws {
        XCTAssertEqual(try TeamLog.ticket(secret: secret), "c1bcb375f34955ef6b9e75cc15059d55bf46a85b0e61fa98e4d7a9de16e3b5e1")
        XCTAssertEqual(try TeamLog.hash(TeamLog.ticket(secret: secret)), "f22e44d35d61359d57e918b9e46b805a3591a804b845080fd7416cdc2a612eb0")
        XCTAssertEqual(welcomeKey.withUnsafeBytes { TeamLog.hex(Data($0)) },
                       "7055dc78eb078daebc2c831cfd49248924bbc842d8b7b4aaa244737974aa3b83")
        XCTAssertEqual(try member().publicKey.rawRepresentation, memberID)
        XCTAssertEqual(failure { _ = try TeamLog.ticket(secret: Data()) }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.ticket(secret: secret + Data([0])) }, .malformed)
    }

    func testItOpensTheBodyOfTheContract() throws {
        let opened = try open(body)
        XCTAssertEqual(opened.epoch, 0)
        XCTAssertEqual(opened.payload, payload)
    }

    func testABodyThatItSealsOpens() throws {
        // CryptoKit adds random bytes to a signature and makes a new nonce, so two seals of one payload differ.
        let sealed = try TeamLog.seal(payload, room: room, entry: entry, epoch: 0, key: key, member: member())
        let again = try TeamLog.seal(payload, room: room, entry: entry, epoch: 0, key: key, member: member())
        XCTAssertNotEqual(sealed, again)
        XCTAssertEqual(sealed.count, body.count)
        XCTAssertEqual(try open(sealed).payload, payload)
        // The smallest payload, a later epoch, and a body that is a slice of other bytes.
        let small = try TeamLog.seal(Data("{}".utf8), room: room, entry: entry, epoch: 7, key: key, member: member())
        XCTAssertEqual(small.count, TeamLog.minimumBody)
        let opened = try open((Data([9, 9]) + small).dropFirst(2), key: { $0 == 7 ? self.key : nil })
        XCTAssertEqual(opened.epoch, 7)
        XCTAssertEqual(opened.payload, Data("{}".utf8))
    }

    func testItRefusesABodyOfAnotherPlaceOrWithChangedBytes() throws {
        let stranger = Curve25519.Signing.PrivateKey().publicKey.rawRepresentation
        XCTAssertEqual(failure { _ = try open(body, entry: changed(entry, at: 0)) }, .seal)
        XCTAssertEqual(failure { _ = try open(body, room: changed(room, at: 31)) }, .seal)
        XCTAssertEqual(failure { _ = try open(body, member: stranger) }, .seal)
        XCTAssertEqual(failure { _ = try open(body, key: { _ in SymmetricKey(size: .bits256) }) }, .seal)
        XCTAssertEqual(failure { _ = try open(changed(body, at: body.count - 20)) }, .seal)
        XCTAssertEqual(failure { _ = try open(changed(body, at: 12)) }, .seal)
        // The epoch of the header is part of what the seal covers.
        XCTAssertEqual(failure { _ = try open(changed(body, at: 4)) }, .seal)
        XCTAssertEqual(failure { _ = try open(body, key: { _ in nil }) }, .unknownEpoch)
        XCTAssertEqual(failure { _ = try open(body.prefix(TeamLog.minimumBody - 1)) }, .malformed)
        XCTAssertEqual(failure { _ = try open(Data()) }, .malformed)
        // The relay takes no larger body, so the Mac opens none.
        XCTAssertEqual(failure { _ = try open(body + Data(count: TeamLog.maximumBody)) }, .malformed)
        // A body of a later version can have another form, also a shorter one. A Mac keeps it for a later update.
        XCTAssertEqual(failure { _ = try open(Data([2]) + body.dropFirst()) }, .unknownVersion)
        XCTAssertEqual(failure { _ = try open(Data([2, 0, 0])) }, .unknownVersion)
        XCTAssertEqual(failure { _ = try open(Data([0]) + body.dropFirst()) }, .malformed)
        XCTAssertEqual(failure { _ = try open(body, entry: entry + Data([0])) }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.seal(payload, room: room.dropLast(), entry: entry, epoch: 0, key: key, member: member()) },
                       .malformed)
        // The relay and `open` refuse a body with a payload under two bytes or over the largest body, so `seal` does too.
        for size in [0, 1, TeamLog.maximumBody - 96] {
            XCTAssertEqual(failure { _ = try TeamLog.seal(Data(count: size), room: room, entry: entry, epoch: 0, key: key, member: member()) },
                           .malformed, "\(size) bytes")
        }
        let largest = try TeamLog.seal(Data(count: TeamLog.maximumBody - 97), room: room, entry: entry, epoch: 0, key: key, member: member())
        XCTAssertEqual(largest.count, TeamLog.maximumBody)
        XCTAssertEqual(try open(largest).payload.count, TeamLog.maximumBody - 97)
    }

    func testItRefusesAPayloadThatAnotherMemberSigned() throws {
        // A member with the room key seals the signed payload of another member under its own member ID.
        let other = Curve25519.Signing.PrivateKey()
        let signed = Data("coconut room entry v1".utf8) + room + entry + Data([0, 0, 0, 0]) + payload
        let additional = Data([1, 0, 0, 0, 0]) + room + entry + other.publicKey.rawRepresentation
        let forged = try Data([1, 0, 0, 0, 0])
            + ChaChaPoly.seal(member().signature(for: signed) + payload, using: key, authenticating: additional).combined
        XCTAssertEqual(failure { _ = try open(forged, member: other.publicKey.rawRepresentation) }, .signature)
        // The same payload with the signature of that member opens.
        let honest = try Data([1, 0, 0, 0, 0])
            + ChaChaPoly.seal(other.signature(for: signed) + payload, using: key, authenticating: additional).combined
        XCTAssertEqual(try open(honest, member: other.publicKey.rawRepresentation).payload, payload)
    }

    func testTheWelcomeOfTheContract() throws {
        let expected = TeamLog.Welcome(epoch: 0, key: roomKey, admin: memberID, name: "Example Team")
        XCTAssertEqual(try TeamLog.open(welcome: welcome, room: room, secret: secret), expected)
        // A seal of the plaintext of the contract with its nonce gives exactly its bytes.
        let exact = try ChaChaPoly.seal(Data(welcomeText.utf8), using: welcomeKey,
                                        nonce: ChaChaPoly.Nonce(data: range(0xa0, 12)), authenticating: room).combined
        XCTAssertEqual(exact, welcome)
        let sealed = try TeamLog.seal(expected, room: room, secret: secret)
        XCTAssertEqual(try TeamLog.open(welcome: sealed, room: room, secret: secret), expected)
        XCTAssertEqual(failure { _ = try TeamLog.open(welcome: sealed, room: changed(room, at: 0), secret: secret) }, .seal)
        XCTAssertEqual(failure { _ = try TeamLog.open(welcome: sealed, room: room, secret: changed(secret, at: 0)) }, .seal)
        XCTAssertEqual(failure { _ = try TeamLog.open(welcome: changed(sealed, at: 20), room: room, secret: secret) }, .seal)
    }

    func testItRefusesAWelcomeWithOtherFields() throws {
        func sealed(_ text: String) throws -> Data {
            try ChaChaPoly.seal(Data(text.utf8), using: welcomeKey, authenticating: room).combined
        }
        func refusal(_ text: String) throws -> TeamLog.Failure? {
            let welcome = try sealed(text)
            return failure { _ = try TeamLog.open(welcome: welcome, room: room, secret: secret) }
        }
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: #""version":1"#, with: #""version":2"#)), .unknownVersion)
        // A welcome of a later version can have other fields.
        XCTAssertEqual(try refusal(#"{"version":2}"#), .unknownVersion)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: #""version":1"#, with: #""version":0"#)), .malformed)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: "cHFy", with: "cHF")), .malformed)
        // The last character of the key has unused bits. With another value in them, the text is not the one of the key.
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: "jo8", with: "jo9")), .malformed)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: "Example Team", with: " Example Team")), .malformed)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: "Example Team", with: "")), .malformed)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: "3f7708", with: "3F7708")), .malformed)
        XCTAssertEqual(try refusal(welcomeText.replacingOccurrences(of: #","name":"Example Team""#, with: "")), .malformed)
        XCTAssertEqual(try refusal("not json"), .malformed)
        // The relay takes no larger welcome, so the Mac opens none, also when its seal is valid.
        let large = #"{"version":1,"epoch":0,"pad":""# + String(repeating: "a", count: 1024) + #""}"#
        XCTAssertGreaterThan(try sealed(large).count, TeamLog.maximumWelcome)
        XCTAssertEqual(try refusal(large), .malformed)
        // A name that the rule for a name refuses.
        let long = TeamLog.Welcome(epoch: 0, key: roomKey, admin: memberID, name: String(repeating: "a", count: 81))
        XCTAssertEqual(failure { _ = try TeamLog.seal(long, room: room, secret: secret) }, .malformed)
        // The longest name, with characters of four bytes each, still fits in the welcome that the relay takes.
        let wide = TeamLog.Welcome(epoch: 0, key: roomKey, admin: memberID, name: String(repeating: "🥥", count: 80))
        XCTAssertLessThanOrEqual(try TeamLog.seal(wide, room: room, secret: secret).count, TeamLog.maximumWelcome)
    }

    func testTheProofOfTheContract() throws {
        let token = TeamLog.hex(range(0xb0, 32))
        XCTAssertEqual(TeamLog.hash(token), "796b2e4cd51a1e5c8c5346d578100dd667264b6c26b5a1dfca876f2e0fb104a5")
        func signedBytes(_ room: Data, _ token: String) -> Data {
            Data("coconut room member v1".utf8) + room + Data(SHA256.hash(data: Data(token.utf8)))
        }
        let signed = signedBytes(room, token)
        let proof = try XCTUnwrap(Data(base64Encoded: "9//Nt2eHY2C17r4Mb2qhzMYlw/Frovnl8OfRJZc6eTNDOFiq9+nfJ4umm2EeFWWcIcE/YnFIhBuQr7Cf7akrBg=="))
        let publicKey = try member().publicKey
        XCTAssertTrue(publicKey.isValidSignature(proof, for: signed))
        let made = try TeamLog.proof(room: room, token: token, member: member())
        XCTAssertEqual(made.count, 64)
        XCTAssertTrue(publicKey.isValidSignature(made, for: signed))
        // A proof is for one room and one member token.
        XCTAssertFalse(publicKey.isValidSignature(made, for: signedBytes(changed(room, at: 0), token)))
        XCTAssertFalse(publicKey.isValidSignature(made, for: signedBytes(room, TeamLog.hex(range(0xb1, 32)))))
        XCTAssertEqual(failure { _ = try TeamLog.proof(room: room.dropLast(), token: token, member: member()) }, .malformed)
    }

    func testTheLinkOfTheContract() throws {
        let invite = TeamLog.Invite(room: room, secret: secret, expires: 1_800_000_000, relay: "https://relay.example", name: "Example Team")
        XCTAssertEqual(try TeamLog.invite(link: link), invite)
        XCTAssertEqual(try TeamLog.link(invite), link)
        XCTAssertEqual(packedLink(), link)
        // A name with other characters, and the longest name.
        for name in ["Équipe 🥥", String(repeating: "é", count: 80)] {
            var other = invite
            other.name = name
            XCTAssertEqual(try TeamLog.invite(link: TeamLog.link(other)), other)
        }
    }

    func testItRefusesALinkThatIsNotAnInvite() throws {
        let token = String(link.dropFirst("coconut://team/".count))
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://pair/\(token)") }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://team/") }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://team/\(token.uppercased())") }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://team/\(token)=") }, .malformed)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://team/\(token.prefix(100))") }, .malformed)
        // A link of a later version can have another form, also a shorter one.
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: "coconut://team/" + Data([2]).base32String) }, .unknownVersion)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(version: 2)) }, .unknownVersion)
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(version: 0)) }, .malformed)
        var invite = try TeamLog.invite(link: link)
        invite.secret = secret.dropLast()
        XCTAssertEqual(failure { _ = try TeamLog.link(invite) }, .malformed)
    }

    func testALinkSendsAMacOnlyToAnHTTPSOriginAndShowsOnlyAPlainName() throws {
        // A crafted link must not send the ticket of a Mac to a local address, over plain HTTP, or to another host.
        var invite = try TeamLog.invite(link: link)
        for relay in ["relay.example", "http://relay.example", "http://169.254.169.254/latest", "https://relay.example/path",
                      "https://relay.example?x=1", "https://user@relay.example", "https://relay.example:99999",
                      "https://relay.example#part", "https://rel ay.example", "https://", "ftp://relay.example",
                      "https://127.0.0.1:8443", "https://10.0.0.5", "https://localhost", "https://localhost:8443",
                      "https://relay..example", "https://relay.example.", "https://[::1]"] {
            XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(relay: relay)) }, .malformed, relay)
            invite.relay = relay
            XCTAssertEqual(failure { _ = try TeamLog.link(invite) }, .malformed, relay)
        }
        XCTAssertEqual(try TeamLog.invite(link: packedLink(relay: "https://relay.example:8443")).relay, "https://relay.example:8443")
        // The loopback relay of the tunnel test is valid only in a debug build, which the tests are.
        XCTAssertEqual(try TeamLog.invite(link: packedLink(relay: "http://127.0.0.1:8787")).relay, "http://127.0.0.1:8787")
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(relay: "http://127.0.0.1")) }, .malformed)
        // The app shows the name before the person joins.
        invite.relay = "https://relay.example"
        for name in ["", " Example Team", "Example Team\n", "Example\u{202E}Team", "\u{FEFF}Example Team", "Example\u{0}Team", String(repeating: "a", count: 81)] {
            XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(name: Data(name.utf8))) }, .malformed, name.debugDescription)
            invite.name = name
            XCTAssertEqual(failure { _ = try TeamLog.link(invite) }, .malformed, name.debugDescription)
        }
        // Bytes that are not UTF-8.
        XCTAssertEqual(failure { _ = try TeamLog.invite(link: packedLink(name: Data([0x41, 0xff]))) }, .malformed)
    }

    func testHexAndBase64URLReadOnlyTheirOwnForm() {
        XCTAssertEqual(TeamLog.bytes(hex: "00ff10"), Data([0, 255, 16]))
        XCTAssertEqual(TeamLog.hex(Data([0, 255, 16])), "00ff10")
        XCTAssertNil(TeamLog.bytes(hex: "0"))
        XCTAssertNil(TeamLog.bytes(hex: "0G"))
        XCTAssertNil(TeamLog.bytes(hex: "FF"))
        XCTAssertEqual(TeamLog.bytes(hex: ""), Data())
        XCTAssertEqual(TeamLog.base64URL(Data([251, 255, 254])), "-__-")
        XCTAssertEqual(TeamLog.bytes(base64URL: "-__-"), Data([251, 255, 254]))
        XCTAssertEqual(TeamLog.bytes(base64URL: TeamLog.base64URL(roomKey)), roomKey)
        XCTAssertNil(TeamLog.bytes(base64URL: "+//+"))
        XCTAssertNil(TeamLog.bytes(base64URL: "AA=="))
        XCTAssertNil(TeamLog.bytes(base64URL: ""))
        // "AA" is the byte 0. "AB" has another value in its unused bits, so it is not the text of a value.
        XCTAssertEqual(TeamLog.bytes(base64URL: "AA"), Data([0]))
        XCTAssertNil(TeamLog.bytes(base64URL: "AB"))
        XCTAssertNil(TeamLog.bytes(base64URL: "A"))
    }
}
