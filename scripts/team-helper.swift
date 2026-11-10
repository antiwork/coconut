import CryptoKit
import Foundation

// The helper of the Mac backend for the team log (docs/team-log.md). The backend uses the Python standard library
// only, so this helper does each step that needs CryptoKit. It compiles with ios/Tunnel/TeamLog.swift, which holds
// the format. backend/coconut/team_helper.py calls it. scripts/team.py compiles it for a source install, and
// scripts/mac_app.py puts it in Coconut.app.
//
// The helper reads one JSON object from standard input and writes one JSON object to standard output. It takes no
// argument, and it reads and writes no file. So a key, a token or a secret is never in a command line or in a log.
//
// - A room ID, an entry ID, a member ID and a token are lowercase hex.
// - A key, a secret, a body, a welcome, a proof and a payload are standard base64, as the relay takes them in JSON.
// - A failure is `{"error": "<code>"}`. The codes are `malformed`, `unknown_version`, `unknown_epoch`, `seal` and
//   `signature`. A code names no value of the input.
//
// Operations (`operation` in the input), with their other input fields and their output:
// - `member`: makes the keys and the token of a member. Output: `member`, `member_key`, `agreement` (the public
//   agreement key as unpadded base64url, as a payload carries it), `agreement_key`, `token`.
// - `proof` (`room`, `token`, `member_key`): `proof` and `hash`, the hash of the token.
// - `seal` (`room`, `entry`, `epoch`, `key`, `member_key`, `payload`): `body`.
// - `open` (`room`, `keys`: epoch to room key, `entries`: a list of `id`, `member`, `body`): `entries`, one result
//   for each entry in the same order: `epoch` and `payload`, or `error`. A result with `unknown_epoch` has `epoch`.
// - `ticket` (`secret`): `ticket` and `hash`, the hash of the ticket.
// - `seal_welcome` (`room`, `secret`, `epoch`, `key`, `admin`, `name`): `welcome`.
// - `open_welcome` (`room`, `secret`, `welcome`): `epoch`, `key`, `admin`, `name`.
// - `link` (`room`, `secret`, `expires`, `relay`, `name`): `link`.
// - `invite` (`link`): `room`, `secret`, `expires`, `relay`, `name`.
@main
struct TeamHelper {
    struct Input: Decodable {
        struct Entry: Decodable {
            let id: String
            let member: String
            let body: Data
        }

        let operation: String
        var room: String?
        var entry: String?
        var epoch: UInt32?
        var key: Data?
        var keys: [String: Data]?
        var memberKey: Data?
        var payload: Data?
        var entries: [Entry]?
        var secret: Data?
        var welcome: Data?
        var admin: String?
        var name: String?
        var relay: String?
        var expires: UInt32?
        var link: String?
        var token: String?

        enum CodingKeys: String, CodingKey {
            case operation, room, entry, epoch, key, keys, payload, entries, secret, welcome, admin, name, relay
            case expires, link, token
            case memberKey = "member_key"
        }
    }

    static func main() {
        var output: [String: Any]
        do {
            // JSONDecoder reads each Data field as standard base64.
            output = try run(JSONDecoder().decode(Input.self, from: FileHandle.standardInput.readDataToEndOfFile()))
        } catch let failure as TeamLog.Failure {
            output = ["error": code(failure)]
        } catch {
            // Input that is not the JSON of an operation, or a key that CryptoKit refuses.
            output = ["error": "malformed"]
        }
        let data = (try? JSONSerialization.data(withJSONObject: output)) ?? Data(#"{"error":"malformed"}"#.utf8)
        FileHandle.standardOutput.write(data)
    }

    static func run(_ input: Input) throws -> [String: Any] {
        switch input.operation {
        case "member":
            let member = Curve25519.Signing.PrivateKey(), agreement = Curve25519.KeyAgreement.PrivateKey()
            // CryptoKit fills a new symmetric key with random bytes.
            let token = SymmetricKey(size: .bits256).withUnsafeBytes { Data($0) }
            return ["member": TeamLog.hex(member.publicKey.rawRepresentation),
                    "member_key": member.rawRepresentation.base64EncodedString(),
                    "agreement": TeamLog.base64URL(agreement.publicKey.rawRepresentation),
                    "agreement_key": agreement.rawRepresentation.base64EncodedString(),
                    "token": TeamLog.hex(token)]
        case "proof":
            let token = try need(input.token)
            let proof = try TeamLog.proof(room: bytes(input.room), token: token, member: signer(input.memberKey))
            return ["proof": proof.base64EncodedString(), "hash": TeamLog.hash(token)]
        case "seal":
            let body = try TeamLog.seal(need(input.payload), room: bytes(input.room), entry: bytes(input.entry),
                                        epoch: need(input.epoch), key: SymmetricKey(data: roomKey(input.key)),
                                        member: signer(input.memberKey))
            return ["body": body.base64EncodedString()]
        case "open":
            let room = try bytes(input.room)
            var keys: [UInt32: SymmetricKey] = [:]
            for (epoch, key) in try need(input.keys) {
                guard let epoch = UInt32(epoch) else { throw TeamLog.Failure.malformed }
                keys[epoch] = try SymmetricKey(data: roomKey(key))
            }
            return ["entries": try need(input.entries).map { open($0, room: room, keys: keys) }]
        case "ticket":
            let ticket = try TeamLog.ticket(secret: need(input.secret))
            return ["ticket": ticket, "hash": TeamLog.hash(ticket)]
        case "seal_welcome":
            let welcome = try TeamLog.Welcome(epoch: need(input.epoch), key: roomKey(input.key), admin: bytes(input.admin),
                                              name: need(input.name))
            let sealed = try TeamLog.seal(welcome, room: bytes(input.room), secret: need(input.secret))
            return ["welcome": sealed.base64EncodedString()]
        case "open_welcome":
            let welcome = try TeamLog.open(welcome: need(input.welcome), room: bytes(input.room), secret: need(input.secret))
            return ["epoch": Int(welcome.epoch), "key": welcome.key.base64EncodedString(), "admin": TeamLog.hex(welcome.admin),
                    "name": welcome.name]
        case "link":
            let invite = try TeamLog.Invite(room: bytes(input.room), secret: need(input.secret), expires: need(input.expires),
                                            relay: need(input.relay), name: need(input.name))
            return ["link": try TeamLog.link(invite)]
        case "invite":
            let invite = try TeamLog.invite(link: need(input.link))
            return ["room": TeamLog.hex(invite.room), "secret": invite.secret.base64EncodedString(),
                    "expires": Int(invite.expires), "relay": invite.relay, "name": invite.name]
        default:
            throw TeamLog.Failure.malformed
        }
    }

    /// Opens one entry of a batch. A failure of one entry does not stop the batch.
    static func open(_ entry: Input.Entry, room: Data, keys: [UInt32: SymmetricKey]) -> [String: Any] {
        // TeamLog asks for the room key of the epoch that the body names.
        var epoch: UInt32?
        do {
            let opened = try TeamLog.open(entry.body, room: room, entry: bytes(entry.id), member: bytes(entry.member)) {
                epoch = $0
                return keys[$0]
            }
            return ["epoch": Int(opened.epoch), "payload": opened.payload.base64EncodedString()]
        } catch let failure as TeamLog.Failure {
            var result: [String: Any] = ["error": code(failure)]
            if failure == .unknownEpoch, let epoch { result["epoch"] = Int(epoch) }
            return result
        } catch {
            return ["error": "malformed"]
        }
    }

    static func code(_ failure: TeamLog.Failure) -> String {
        switch failure {
        case .malformed: "malformed"
        case .unknownVersion: "unknown_version"
        case .unknownEpoch: "unknown_epoch"
        case .seal: "seal"
        case .signature: "signature"
        }
    }

    static func need<Value>(_ value: Value?) throws -> Value {
        guard let value else { throw TeamLog.Failure.malformed }
        return value
    }

    /// The bytes of an ID in lowercase hex. TeamLog checks the length of each ID.
    static func bytes(_ hex: String?) throws -> Data {
        guard let data = TeamLog.bytes(hex: try need(hex)) else { throw TeamLog.Failure.malformed }
        return data
    }

    static func roomKey(_ key: Data?) throws -> Data {
        let key = try need(key)
        guard key.count == TeamLog.longLength else { throw TeamLog.Failure.malformed }
        return key
    }

    static func signer(_ key: Data?) throws -> Curve25519.Signing.PrivateKey {
        try Curve25519.Signing.PrivateKey(rawRepresentation: need(key))
    }
}
