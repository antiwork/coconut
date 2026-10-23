import CryptoKit
import Foundation
import XCTest
@testable import Coconut

final class NoiseTests: XCTestCase {
    private struct Vectors: Decodable {
        let vectors: [Vector]
    }

    private struct Vector: Decodable {
        let protocol_name: String
        let init_prologue: String
        let init_static: String
        let init_ephemeral: String
        let init_remote_static: String
        let init_psks: [String]?
        let resp_prologue: String
        let resp_static: String
        let resp_ephemeral: String
        let resp_psks: [String]?
        let handshake_hash: String
        let messages: [Message]
    }

    private struct Message: Decodable {
        let payload: String
        let ciphertext: String
    }

    private typealias Key = Curve25519.KeyAgreement.PrivateKey

    private func vectors() throws -> [Vector] {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "noise-vectors", withExtension: "json"))
        return try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: url)).vectors
    }

    private func hex(_ text: String) -> Data {
        var data = Data()
        var index = text.startIndex
        while index < text.endIndex {
            let next = text.index(index, offsetBy: 2)
            data.append(UInt8(text[index..<next], radix: 16)!)
            index = next
        }
        return data
    }

    private func key(_ text: String) throws -> Key { try Key(rawRepresentation: hex(text)) }

    // Each vector runs a full handshake and four transport messages with fixed keys.
    func testOfficialVectorsMatchForIKAndIKpsk2() throws {
        let all = try vectors()
        XCTAssertEqual(Set(all.map(\.protocol_name)),
                       ["Noise_IK_25519_ChaChaPoly_SHA256", "Noise_IKpsk2_25519_ChaChaPoly_SHA256"])
        for vector in all {
            let pattern: NoisePattern = vector.protocol_name.contains("psk2") ? .ikpsk2 : .ik
            var initiator = try NoiseHandshake(pattern: pattern, initiator: true, localStatic: key(vector.init_static),
                                               remoteStatic: hex(vector.init_remote_static),
                                               psk: vector.init_psks?.first.map(hex), prologue: hex(vector.init_prologue),
                                               ephemeral: key(vector.init_ephemeral))
            var responder = try NoiseHandshake(pattern: pattern, initiator: false, localStatic: key(vector.resp_static),
                                               psk: vector.resp_psks?.first.map(hex), prologue: hex(vector.resp_prologue),
                                               ephemeral: key(vector.resp_ephemeral))
            let first = try initiator.writeMessage(payload: hex(vector.messages[0].payload))
            XCTAssertEqual(first, hex(vector.messages[0].ciphertext), vector.protocol_name)
            XCTAssertEqual(try responder.readMessage(first), hex(vector.messages[0].payload))
            let second = try responder.writeMessage(payload: hex(vector.messages[1].payload))
            XCTAssertEqual(second, hex(vector.messages[1].ciphertext), vector.protocol_name)
            XCTAssertEqual(try initiator.readMessage(second), hex(vector.messages[1].payload))

            var sender = try initiator.transport(), receiver = try responder.transport()
            XCTAssertEqual(sender.handshakeHash, hex(vector.handshake_hash))
            XCTAssertEqual(receiver.handshakeHash, hex(vector.handshake_hash))
            XCTAssertEqual(receiver.remoteStatic, try key(vector.init_static).publicKey.rawRepresentation)
            for (index, message) in vector.messages.dropFirst(2).enumerated() {
                // Transport messages alternate, starting with the initiator.
                if index.isMultiple(of: 2) {
                    let ciphertext = try sender.send.encrypt(hex(message.payload), ad: Data())
                    XCTAssertEqual(ciphertext, hex(message.ciphertext), "\(vector.protocol_name) message \(index + 2)")
                    XCTAssertEqual(try receiver.receive.decrypt(ciphertext, ad: Data()), hex(message.payload))
                } else {
                    let ciphertext = try receiver.send.encrypt(hex(message.payload), ad: Data())
                    XCTAssertEqual(ciphertext, hex(message.ciphertext), "\(vector.protocol_name) message \(index + 2)")
                    XCTAssertEqual(try sender.receive.decrypt(ciphertext, ad: Data()), hex(message.payload))
                }
            }
        }
    }

    private func pair(pattern: NoisePattern = .ik, psk: Data? = nil, responderPSK: Data? = nil)
        throws -> (iPhone: NoiseTransport, mac: NoiseTransport, iPhoneKey: Key) {
        let iPhoneKey = Key(), macKey = Key()
        var iPhone = try NoiseHandshake(pattern: pattern, initiator: true, localStatic: iPhoneKey,
                                        remoteStatic: macKey.publicKey.rawRepresentation, psk: psk)
        var mac = try NoiseHandshake(pattern: pattern, initiator: false, localStatic: macKey, psk: responderPSK ?? psk)
        _ = try mac.readMessage(try iPhone.writeMessage())
        _ = try iPhone.readMessage(try mac.writeMessage())
        return (try iPhone.transport(), try mac.transport(), iPhoneKey)
    }

    func testTheMacLearnsTheIPhoneKeyAndBothSidesAgree() throws {
        let (iPhone, mac, iPhoneKey) = try pair()
        XCTAssertEqual(mac.remoteStatic, iPhoneKey.publicKey.rawRepresentation)
        XCTAssertEqual(iPhone.handshakeHash, mac.handshakeHash)
    }

    func testAnotherMacCannotAnswerTheHandshake() throws {
        let iPhoneKey = Key(), macKey = Key(), impostor = Key()
        var iPhone = try NoiseHandshake(pattern: .ik, initiator: true, localStatic: iPhoneKey,
                                        remoteStatic: macKey.publicKey.rawRepresentation)
        var other = try NoiseHandshake(pattern: .ik, initiator: false, localStatic: impostor)
        XCTAssertThrowsError(try other.readMessage(try iPhone.writeMessage()))
    }

    func testAWrongPairingSecretFailsTheHandshake() throws {
        let secret = Data(repeating: 1, count: 32), wrong = Data(repeating: 2, count: 32)
        XCTAssertNoThrow(try pair(pattern: .ikpsk2, psk: secret))
        XCTAssertThrowsError(try pair(pattern: .ikpsk2, psk: secret, responderPSK: wrong))
        XCTAssertThrowsError(try NoiseHandshake(pattern: .ikpsk2, initiator: false, localStatic: Key()), "A pairing needs a secret.")
        XCTAssertThrowsError(try NoiseHandshake(pattern: .ik, initiator: false, localStatic: Key(), psk: secret))
    }

    func testChangedRepeatedOrSkippedMessagesAreRejected() throws {
        var (iPhone, mac, _) = try pair()
        let first = try iPhone.send.encrypt(Data("one".utf8), ad: Data())
        let second = try iPhone.send.encrypt(Data("two".utf8), ad: Data())
        var changed = first
        changed[changed.startIndex] ^= 1
        var copy = mac
        XCTAssertThrowsError(try copy.receive.decrypt(changed, ad: Data()))
        copy = mac
        XCTAssertThrowsError(try copy.receive.decrypt(second, ad: Data()), "A skipped message must fail.")
        XCTAssertEqual(try mac.receive.decrypt(first, ad: Data()), Data("one".utf8))
        XCTAssertThrowsError(try mac.receive.decrypt(first, ad: Data()), "A repeated message must fail.")
    }

    func testALowOrderKeyIsRejected() throws {
        let zero = Data(repeating: 0, count: 32)
        var iPhone = try NoiseHandshake(pattern: .ik, initiator: true, localStatic: Key(), remoteStatic: zero)
        XCTAssertThrowsError(try iPhone.writeMessage())
        XCTAssertThrowsError(try iPhone.writeMessage(), "A failed handshake stays failed.")
    }

    func testHandshakeKeysAreGivenOutOnce() throws {
        let iPhoneKey = Key(), macKey = Key()
        var iPhone = try NoiseHandshake(pattern: .ik, initiator: true, localStatic: iPhoneKey,
                                        remoteStatic: macKey.publicKey.rawRepresentation)
        var mac = try NoiseHandshake(pattern: .ik, initiator: false, localStatic: macKey)
        XCTAssertThrowsError(try mac.writeMessage(), "The Mac cannot speak first.")
        XCTAssertThrowsError(try mac.readMessage(Data(count: 10)))
        mac = try NoiseHandshake(pattern: .ik, initiator: false, localStatic: macKey)
        _ = try mac.readMessage(try iPhone.writeMessage())
        _ = try iPhone.readMessage(try mac.writeMessage())
        XCTAssertNoThrow(try iPhone.transport())
        XCTAssertThrowsError(try iPhone.transport(), "A second split would reuse nonces.")
        XCTAssertThrowsError(try iPhone.writeMessage(payload: Data(count: NoiseError.maxMessage)))
    }

    func testSessionsRekeyAfterTheMessageLimit() throws {
        let (iPhone, mac, _) = try pair()
        let sender = TunnelSession(transport: iPhone), receiver = TunnelSession(transport: mac)
        let ping = TunnelFrame(type: .ping, stream: 0)
        var rekeys = 0
        for _ in 0..<(Int(TunnelSession.rekeyAfterMessages) + 2) {
            for ciphertext in try sender.seal(ping) {
                if try receiver.open(ciphertext) == nil { rekeys += 1 }
            }
        }
        XCTAssertEqual(rekeys, 1)
    }

    func testAnInvalidRekeyFrameClosesTheSession() throws {
        var (iPhone, mac, _) = try pair()
        let receiver = TunnelSession(transport: mac)
        let bad = try iPhone.send.encrypt(try TunnelFrame(type: .rekey, stream: 1).encoded(), ad: Data())
        XCTAssertThrowsError(try receiver.open(bad))
        XCTAssertTrue(receiver.closed)
    }

    func testFramesRoundTripAndRejectBadInput() throws {
        let frame = TunnelFrame(type: .requestHead, stream: 0x0102_0304, flags: 7, payload: Data("{}".utf8))
        let encoded = try frame.encoded()
        XCTAssertEqual([UInt8](encoded.prefix(6)), [1, 1, 2, 3, 4, 7])
        XCTAssertEqual(try TunnelFrame.decode(encoded), frame)
        XCTAssertThrowsError(try TunnelFrame.decode(Data([1, 0, 0])))
        XCTAssertThrowsError(try TunnelFrame.decode(Data([99, 0, 0, 0, 1, 0])))
        XCTAssertThrowsError(try TunnelFrame(type: .body, stream: 1, payload: Data(count: TunnelFrame.maxPayload + 1)).encoded())
        let relay = RelayFrame(channel: 9, ciphertext: Data([1, 2]))
        XCTAssertEqual(try RelayFrame.decode(relay.encoded()), relay)
        XCTAssertThrowsError(try RelayFrame.decode(Data(count: RelayFrame.maxSize + 1)))
    }

    func testALargeBodySplitsIntoFramesThatFitOneNoiseMessage() throws {
        let body = Data((0..<(3 * 1024 * 1024 + 17)).map { UInt8(truncatingIfNeeded: $0) })
        let frames = TunnelFrame.bodyFrames(body, stream: 3)
        XCTAssertEqual(frames.count, Int((Double(body.count) / Double(TunnelFrame.maxPayload)).rounded(.up)))
        XCTAssertEqual(frames.reduce(Data()) { $0 + $1.payload }, body)
        let largest = try XCTUnwrap(frames.map { try $0.encoded().count + 16 }.max())
        XCTAssertLessThanOrEqual(largest, 65_535)
        XCTAssertLessThanOrEqual(largest + 4, RelayFrame.maxSize)
    }

    func testSessionsRekeyAfterThirtyMinutesAndKeepWorking() throws {
        let (iPhone, mac, _) = try pair()
        let start = Date(timeIntervalSince1970: 1_000)
        let sender = TunnelSession(transport: iPhone, now: start), receiver = TunnelSession(transport: mac, now: start)
        let ping = TunnelFrame(type: .ping, stream: 0)
        let early = try sender.seal(ping, now: start.addingTimeInterval(60))
        XCTAssertEqual(early.count, 1)
        XCTAssertEqual(try receiver.open(early[0]), ping)
        let late = try sender.seal(ping, now: start.addingTimeInterval(TunnelSession.rekeyAfterSeconds + 1))
        XCTAssertEqual(late.count, 2, "A REKEY frame goes first.")
        XCTAssertNil(try receiver.open(late[0]))
        XCTAssertEqual(try receiver.open(late[1]), ping)
    }

    func testASessionClosesAfterAnyBadFrame() throws {
        let (iPhone, mac, _) = try pair()
        let sender = TunnelSession(transport: iPhone), receiver = TunnelSession(transport: mac)
        var ciphertext = try sender.seal(TunnelFrame(type: .ping, stream: 0))[0]
        ciphertext[ciphertext.startIndex] ^= 0xff
        XCTAssertThrowsError(try receiver.open(ciphertext))
        XCTAssertTrue(receiver.closed)
        let next = try sender.seal(TunnelFrame(type: .ping, stream: 0))[0]
        XCTAssertThrowsError(try receiver.open(next), "A closed session needs a new handshake.")
    }
}
