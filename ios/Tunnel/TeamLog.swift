import CryptoKit
import Foundation

// The format of the team log (docs/team-log.md): the sealed body of an entry, the welcome and the link of an invite,
// and the proof of a member. The relay sees none of the plaintext.
//
// This file uses CryptoKit and Foundation only, and its text helpers are its own. So a helper of the Mac can compile
// it without the app, as the sidecar compiles Noise.swift. TeamLogTests checks each part against the vectors of the
// contract.
enum TeamLog {
    /// The version of a body and of a welcome that this code makes and reads.
    static let version: UInt8 = 1
    /// The version of an invite link.
    static let linkVersion: UInt8 = 1
    /// A room ID, a member ID (the public member key), a room key and an invite secret have 32 bytes.
    static let longLength = 32
    static let entryLength = 16
    /// The parts of a body around its payload: version, epoch, nonce, signature and tag.
    private static let overhead = 1 + 4 + 12 + 64 + 16
    /// The smallest payload is `{}`.
    static let minimumBody = overhead + 2
    /// The relay takes a body and a welcome of at most these sizes.
    static let maximumBody = 1024 * 1024
    static let maximumWelcome = 1024
    static let maximumName = 80
    /// The bytes of a link before its relay: version, room ID, invite secret, expiry, and the length of the relay.
    private static let linkFixed = 1 + 2 * longLength + 4 + 2

    enum Failure: Error, Equatable, Sendable {
        /// The bytes or the text do not have the form that the contract gives.
        case malformed
        /// A newer Coconut made this body, welcome or link. A Mac keeps such a body and tries again after an update.
        case unknownVersion
        /// The caller has no room key for the epoch of the body.
        case unknownEpoch
        /// The seal does not open: another key, room, entry ID or member, or changed bytes.
        case seal
        /// The seal opens, but the member key did not sign this payload for this place.
        case signature
    }

    // MARK: Entry

    /// Seals a payload as the body of an entry: `version | epoch | nonce | ciphertext | tag`, where the plaintext is
    /// the signature of the member and the payload.
    static func seal(_ payload: Data, room: Data, entry: Data, epoch: UInt32, key: SymmetricKey,
                     member: Curve25519.Signing.PrivateKey) throws -> Data {
        guard room.count == longLength, entry.count == entryLength,
              (2...maximumBody - overhead).contains(payload.count) else { throw Failure.malformed }
        let signature = try member.signature(for: signed(payload, room: room, entry: entry, epoch: epoch))
        let data = additionalData(epoch: epoch, room: room, entry: entry, member: member.publicKey.rawRepresentation)
        // CryptoKit makes a new random nonce for each seal.
        return try header(epoch) + ChaChaPoly.seal(signature + payload, using: key, authenticating: data).combined
    }

    /// Opens the body of an entry, then checks that `member` signed its payload. `key` gives the room key of an epoch.
    static func open(_ body: Data, room: Data, entry: Data, member: Data,
                     key: (UInt32) -> SymmetricKey?) throws -> (epoch: UInt32, payload: Data) {
        let body = Data(body)
        guard room.count == longLength, entry.count == entryLength, member.count == longLength,
              let first = body.first else { throw Failure.malformed }
        // The version comes first: a body of a later version can have another form, and the Mac keeps it.
        guard first <= version else { throw Failure.unknownVersion }
        guard first == version, (minimumBody...maximumBody).contains(body.count) else { throw Failure.malformed }
        let epoch = uint32(body[1..<5])
        guard let key = key(epoch) else { throw Failure.unknownEpoch }
        let data = additionalData(epoch: epoch, room: room, entry: entry, member: member)
        guard let box = try? ChaChaPoly.SealedBox(combined: body[5...]),
              let plain = try? ChaChaPoly.open(box, using: key, authenticating: data) else { throw Failure.seal }
        let payload = Data(plain.dropFirst(64))
        guard let signer = try? Curve25519.Signing.PublicKey(rawRepresentation: member),
              signer.isValidSignature(plain.prefix(64), for: signed(payload, room: room, entry: entry, epoch: epoch)) else {
            throw Failure.signature
        }
        return (epoch, payload)
    }

    private static func header(_ epoch: UInt32) -> Data { Data([version]) + bigEndian(epoch) }

    /// The seal covers these bytes too, so the relay cannot move a body to another room, entry ID, member or epoch.
    private static func additionalData(epoch: UInt32, room: Data, entry: Data, member: Data) -> Data {
        header(epoch) + room + entry + member
    }

    /// The bytes that the member key signs.
    private static func signed(_ payload: Data, room: Data, entry: Data, epoch: UInt32) -> Data {
        Data("coconut room entry v1".utf8) + room + entry + bigEndian(epoch) + payload
    }

    // MARK: Invite

    /// What a Mac needs to join a room. The link carries it, and the link holds no room key.
    struct Invite: Equatable, Sendable {
        var room: Data
        var secret: Data
        /// A Unix time in seconds.
        var expires: UInt32
        var relay: String
        /// The name of the team.
        var name: String
    }

    /// What the admin's Mac seals for a new member: the room key of the current epoch and the member ID of the admin.
    struct Welcome: Equatable, Sendable {
        var epoch: UInt32
        var key: Data
        var admin: Data
        var name: String
    }

    /// The ticket that a new Mac shows to the relay, from the invite secret.
    static func ticket(secret: Data) throws -> String {
        guard secret.count == longLength else { throw Failure.malformed }
        return hex(derive(secret, "coconut room ticket"))
    }

    /// The hash of a member token or a ticket, as the relay stores it.
    static func hash(_ token: String) -> String { hex(digest(token)) }

    private static func digest(_ token: String) -> Data { Data(SHA256.hash(data: Data(token.utf8))) }

    private static func welcomeKey(secret: Data) -> SymmetricKey {
        SymmetricKey(data: derive(secret, "coconut room welcome"))
    }

    /// Seals the welcome: `nonce | ciphertext | tag`, with the room ID as the additional data.
    static func seal(_ welcome: Welcome, room: Data, secret: Data) throws -> Data {
        guard room.count == longLength, secret.count == longLength, welcome.key.count == longLength,
              welcome.admin.count == longLength, validName(welcome.name) else { throw Failure.malformed }
        let fields = WelcomeFields(version: Int(version), epoch: welcome.epoch, key: base64URL(welcome.key),
                                   admin: hex(welcome.admin), name: welcome.name)
        let sealed = try ChaChaPoly.seal(JSONEncoder().encode(fields), using: welcomeKey(secret: secret),
                                         authenticating: room).combined
        guard sealed.count <= maximumWelcome else { throw Failure.malformed }
        return sealed
    }

    static func open(welcome sealed: Data, room: Data, secret: Data) throws -> Welcome {
        guard room.count == longLength, secret.count == longLength, sealed.count <= maximumWelcome else {
            throw Failure.malformed
        }
        guard let box = try? ChaChaPoly.SealedBox(combined: sealed),
              let plain = try? ChaChaPoly.open(box, using: welcomeKey(secret: secret), authenticating: room) else {
            throw Failure.seal
        }
        // The version comes first: a welcome of a later version can have other fields.
        guard let probe = try? JSONDecoder().decode(VersionProbe.self, from: plain) else { throw Failure.malformed }
        guard probe.version <= Int(version) else { throw Failure.unknownVersion }
        guard probe.version == Int(version), let fields = try? JSONDecoder().decode(WelcomeFields.self, from: plain),
              let key = bytes(base64URL: fields.key), key.count == longLength,
              let admin = bytes(hex: fields.admin), admin.count == longLength, validName(fields.name) else {
            throw Failure.malformed
        }
        return Welcome(epoch: fields.epoch, key: key, admin: admin, name: fields.name)
    }

    private struct VersionProbe: Decodable { let version: Int }

    private struct WelcomeFields: Codable {
        let version: Int
        let epoch: UInt32
        let key: String
        let admin: String
        let name: String
    }

    /// The link of an invite: `coconut://team/<token>`, where the token is lowercase base32 of the packed invite.
    static func link(_ invite: Invite) throws -> String {
        let relay = Data(invite.relay.utf8)
        guard invite.room.count == longLength, invite.secret.count == longLength, validRelay(invite.relay),
              validName(invite.name) else { throw Failure.malformed }
        let packed = Data([linkVersion]) + invite.room + invite.secret + bigEndian(invite.expires)
            + Data([UInt8(relay.count >> 8), UInt8(relay.count & 0xff)]) + relay + Data(invite.name.utf8)
        return linkPrefix + base32(packed)
    }

    static func invite(link: String) throws -> Invite {
        guard link.hasPrefix(linkPrefix), let packed = bytes(base32: String(link.dropFirst(linkPrefix.count))),
              let first = packed.first else { throw Failure.malformed }
        guard first <= linkVersion else { throw Failure.unknownVersion }
        guard first == linkVersion, packed.count >= linkFixed else { throw Failure.malformed }
        let secretStart = 1 + longLength, expiresStart = secretStart + longLength
        let length = Int(uint32(packed[(linkFixed - 2)..<linkFixed]))
        guard packed.count >= linkFixed + length else { throw Failure.malformed }
        let relayBytes = packed[linkFixed..<linkFixed + length], nameBytes = packed[(linkFixed + length)...]
        // A text that is not UTF-8 does not give its own bytes back, and neither does a text with a byte order mark.
        let relay = String(decoding: relayBytes, as: UTF8.self), name = String(decoding: nameBytes, as: UTF8.self)
        guard Data(relay.utf8) == relayBytes, Data(name.utf8) == nameBytes, validRelay(relay), validName(name) else {
            throw Failure.malformed
        }
        return Invite(room: Data(packed[1..<secretStart]), secret: Data(packed[secretStart..<expiresStart]),
                      expires: uint32(packed[expiresStart..<expiresStart + 4]), relay: relay, name: name)
    }

    private static let linkPrefix = "coconut://team/"

    /// An HTTPS origin without a path or account details, as for a pairing link, and with a host name: not an
    /// address in numbers and not a name without a dot, such as `localhost`. So a changed link cannot send a Mac
    /// over plain HTTP or to an address that the link gives in numbers. The check does not know where a host name
    /// points. A debug build also accepts the loopback test relay. The watch mode of the Mac helper uses the same rule
    /// before it sends a member token to a relay.
    static func validRelay(_ value: String) -> Bool {
        guard value.utf8.count <= 255, value.utf8.allSatisfy({ (33...126).contains($0) }),
              let parts = URLComponents(string: value), parts.user == nil, parts.password == nil, parts.query == nil,
              parts.fragment == nil, parts.path.isEmpty, let host = parts.host, !host.isEmpty,
              parts.port.map({ (1...65_535).contains($0) }) ?? true,
              host.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 46 })
        else { return false }
        // The last part of a host name has a letter. The last part of an IPv4 address has only digits.
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        if parts.scheme == "https" {
            return labels.count >= 2 && !labels.contains(where: \.isEmpty) && labels.last?.contains(where: \.isLetter) == true
        }
        #if DEBUG
        if parts.scheme == "http" && host == "127.0.0.1" && parts.port != nil { return true }
        #endif
        return false
    }

    /// The name of a team, as the name of a Mac in a pairing link: 1 to 80 characters, no space around it, and no
    /// control character, because the app shows it before the person joins.
    private static func validName(_ name: String) -> Bool {
        !name.isEmpty && name.count <= maximumName && name == name.trimmingCharacters(in: .whitespacesAndNewlines)
            && !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) })
    }

    // MARK: Member

    /// The proof that a Mac has the key of its member ID: the signature of that key over the room ID and the hash of
    /// its member token. The relay asks for it when the Mac makes or joins a room.
    static func proof(room: Data, token: String, member: Curve25519.Signing.PrivateKey) throws -> Data {
        guard room.count == longLength else { throw Failure.malformed }
        return try member.signature(for: Data("coconut room member v1".utf8) + room + digest(token))
    }

    // MARK: Bytes and text

    /// HKDF-SHA256 with an empty salt and a 32-byte output, as for the values of a pairing.
    private static func derive(_ input: Data, _ label: String) -> Data {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: input), info: Data(label.utf8), outputByteCount: 32)
            .withUnsafeBytes { Data($0) }
    }

    private static func bigEndian(_ value: UInt32) -> Data { withUnsafeBytes(of: value.bigEndian) { Data($0) } }

    /// Reads up to four bytes as a big-endian number.
    private static func uint32(_ bytes: Data) -> UInt32 { bytes.reduce(0) { $0 << 8 | UInt32($1) } }

    private static let hexDigits = Array("0123456789abcdef".utf8)

    /// Lowercase hex, as each ID of the contract.
    static func hex(_ data: Data) -> String {
        String(unsafeUninitializedCapacity: data.count * 2) { text in
            var index = 0
            for byte in data {
                text[index] = hexDigits[Int(byte >> 4)]
                text[index + 1] = hexDigits[Int(byte & 15)]
                index += 2
            }
            return index
        }
    }

    static func bytes(hex text: String) -> Data? {
        let digits = Array(text.utf8)
        guard digits.count % 2 == 0 else { return nil }
        var data = Data(capacity: digits.count / 2)
        for index in stride(from: 0, to: digits.count, by: 2) {
            guard let high = nibble(digits[index]), let low = nibble(digits[index + 1]) else { return nil }
            data.append(high << 4 | low)
        }
        return data
    }

    private static func nibble(_ digit: UInt8) -> UInt8? {
        switch digit {
        case 48...57: digit - 48
        case 97...102: digit - 87
        default: nil
        }
    }

    /// Unpadded base64url, as a key in a payload.
    static func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// One value has one text: the text must be what `base64URL` makes for its bytes.
    static func bytes(base64URL text: String) -> Data? {
        let standard = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        guard !text.isEmpty, let data = Data(base64Encoded: standard + String(repeating: "=", count: (4 - standard.count % 4) % 4)),
              base64URL(data) == text else { return nil }
        return data
    }

    private static let base32Alphabet = Array("abcdefghijklmnopqrstuvwxyz234567".utf8)

    /// Unpadded lowercase base32 (RFC 4648), as in a pairing link.
    private static func base32(_ data: Data) -> String {
        var text: [UInt8] = []
        var buffer = 0, bits = 0
        for byte in data {
            buffer = buffer << 8 | Int(byte)
            bits += 8
            while bits >= 5 {
                bits -= 5
                text.append(base32Alphabet[(buffer >> bits) & 31])
            }
            buffer &= (1 << bits) - 1
        }
        if bits > 0 { text.append(base32Alphabet[(buffer << (5 - bits)) & 31]) }
        return String(decoding: text, as: UTF8.self)
    }

    /// The unused last bits must be zero, so one token has one text.
    private static func bytes(base32 text: String) -> Data? {
        var data = Data()
        var buffer = 0, bits = 0
        for character in text.utf8 {
            guard let value = base32Alphabet.firstIndex(of: character) else { return nil }
            buffer = buffer << 5 | value
            bits += 5
            if bits >= 8 {
                bits -= 8
                data.append(UInt8(buffer >> bits))
                buffer &= (1 << bits) - 1
            }
        }
        return !text.isEmpty && bits < 5 && buffer == 0 ? data : nil
    }
}
