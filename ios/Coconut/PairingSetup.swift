import Foundation

// A version 2 setup from `scripts/pairing.py`, in a version 3 link: coconut://pair/<token>
// The token is lowercase base32 of the link version 3, the route (32 bytes), the Mac key (32 bytes), the one-time
// code (32 bytes), the expiry (4 bytes, big-endian Unix seconds), the relay length (2 bytes, big-endian), the relay, and the Mac
// name. Hermes masks base64url JSON as a JSON Web Token, but no Hermes rule matches this token.
// The rendezvous box of a pairing code holds the setup as JSON: {version:2, relay, route, mac_key, secret, expires, name}.
/// True for 64 lowercase hex characters: the route of a pairing, and the room ID of a team (team_keys.ROOM_ID).
func isLowercaseHex64(_ text: String) -> Bool {
    text.utf8.count == 64 && text.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

struct PairingSetup: Equatable, Sendable {
    let relay: URL
    let route: String
    let macKey: Data
    let secret: Data
    // The secret as the link spells it. POST /v1/pair sends it back as the code.
    let code: String
    let expires: Date
    let name: String

    private static let fields: Set<String> = ["version", "relay", "route", "mac_key", "secret", "expires", "name"]

    var endpoint: TunnelEndpoint { TunnelEndpoint(relay: relay, route: route, macKey: macKey) }

    static func isLink(_ url: URL) -> Bool { url.scheme?.lowercased() == "coconut" && url.host?.lowercased() == "pair" }

    // The QR code holds the same link.
    static func parse(_ payload: String) throws -> Self {
        // The size limit applies to the whole input, before any trimming.
        guard payload.utf8.count <= 4_096 else { throw PairingSetupError.invalidPayload }
        let text = payload.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("coconut://") { return try parse(link: text) }
        return try parse(json: Data(text.utf8))
    }

    private static func parse(link: String) throws -> Self {
        guard let url = URL(string: link), isLink(url),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw PairingSetupError.invalidPayload }
        let token = parts.path.hasPrefix("/") ? String(parts.path.dropFirst()) : parts.path
        guard !token.isEmpty else { throw PairingSetupError.damagedLink }
        return try parse(token: token)
    }

    private static func parse(token: String) throws -> Self {
        guard let data = Data(base32: token).map(Array.init), data.count >= 103, data[0] == 3,
              data.count >= 103 + (Int(data[101]) << 8 | Int(data[102])) else { throw PairingSetupError.damagedLink }
        let relayEnd = 103 + (Int(data[101]) << 8 | Int(data[102]))
        guard let relay = String(bytes: data[103..<relayEnd], encoding: .utf8),
              let name = String(bytes: data[relayEnd...], encoding: .utf8) else { throw PairingSetupError.damagedLink }
        let expires = data[97..<101].reduce(0) { $0 << 8 | Int($1) }
        let secret = Data(data[65..<97])
        let code = secret.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        return try make(relay: relay, route: Data(data[1..<33]).hexString, macKey: Data(data[33..<65]), secret: secret,
                        code: code, expires: expires, name: name)
    }

    private static func parse(json data: Data) throws -> Self {
        guard let probe = try? JSONDecoder().decode(VersionProbe.self, from: data) else { throw PairingSetupError.invalidPayload }
        guard probe.version == 2 else { throw PairingSetupError.unsupportedVersion }
        let fields: Fields
        do { fields = try JSONDecoder().decode(Fields.self, from: data) }
        catch { throw PairingSetupError.invalidPayload }
        guard let macKey = Data(base64URL: fields.macKey), let secret = Data(base64URL: fields.secret) else {
            throw PairingSetupError.invalidKey
        }
        return try make(relay: fields.relay, route: fields.route, macKey: macKey, secret: secret, code: fields.secret,
                        expires: fields.expires, name: fields.name)
    }

    private static func make(relay: String, route: String, macKey: Data, secret: Data, code: String, expires: Int,
                             name: String) throws -> Self {
        guard let relay = validRelay(relay) else { throw PairingSetupError.invalidRelay }
        guard isLowercaseHex64(route) else {
            throw PairingSetupError.invalidRoute
        }
        guard macKey.count == 32, secret.count == 32 else { throw PairingSetupError.invalidKey }
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 80, !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw PairingSetupError.invalidName
        }
        return Self(relay: relay, route: route, macKey: macKey, secret: secret, code: code,
                    expires: Date(timeIntervalSince1970: TimeInterval(expires)), name: name)
    }

    // An HTTPS origin without a path or account details. A debug build also accepts the loopback test relay.
    static func validRelay(_ value: String) -> URL? {
        guard value.utf8.allSatisfy({ (33...126).contains($0) }), let parts = URLComponents(string: value),
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil, parts.path.isEmpty,
              let host = parts.host, !host.isEmpty, parts.port.map({ (1...65_535).contains($0) }) ?? true,
              host.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 46 }),
              let url = parts.url else { return nil }
        if parts.scheme == "https" { return url }
        #if DEBUG
        if parts.scheme == "http" && host == "127.0.0.1" && parts.port != nil { return url }
        #endif
        return nil
    }

    private struct VersionProbe: Decodable { let version: Int }

    private struct Fields: Decodable {
        let relay: String
        let route: String
        let macKey: String
        let secret: String
        let expires: Int
        let name: String

        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: FieldKey.self)
            guard Set(values.allKeys.map(\.stringValue)) == PairingSetup.fields else { throw PairingSetupError.invalidPayload }
            relay = try values.decode(String.self, forKey: FieldKey("relay"))
            route = try values.decode(String.self, forKey: FieldKey("route"))
            macKey = try values.decode(String.self, forKey: FieldKey("mac_key"))
            secret = try values.decode(String.self, forKey: FieldKey("secret"))
            expires = try values.decode(Int.self, forKey: FieldKey("expires"))
            name = try values.decode(String.self, forKey: FieldKey("name"))
        }
    }

    private struct FieldKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init(_ value: String) { stringValue = value }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { return nil }
    }
}

enum PairingSetupError: LocalizedError, Equatable {
    case invalidPayload, unsupportedVersion, damagedLink, invalidRelay, invalidRoute, invalidKey, invalidName

    var errorDescription: String? {
        switch self {
        case .invalidPayload: "This is not a Coconut pairing link. Ask your Hermes for a new pairing link."
        case .unsupportedVersion: "This pairing link needs a different Coconut version. Update the app and try again."
        case .invalidRelay: "The pairing link has an invalid relay address. Ask your Hermes for a new pairing link."
        case .damagedLink, .invalidRoute, .invalidKey: "The pairing link is damaged. Ask your Hermes for a new pairing link."
        case .invalidName: "The pairing link has an invalid Mac name. Ask your Hermes for a new pairing link."
        }
    }
}

extension Data {
    // Unpadded lowercase base32 (RFC 4648), as in the version 3 pairing link. The unused last bits must be zero.
    init?(base32 text: String) {
        let alphabet = Array("abcdefghijklmnopqrstuvwxyz234567".utf8)
        var bytes: [UInt8] = []
        var buffer = 0, bits = 0
        for character in text.utf8 {
            guard let value = alphabet.firstIndex(of: character) else { return nil }
            buffer = buffer << 5 | value
            bits += 5
            if bits >= 8 {
                bits -= 8
                bytes.append(UInt8(buffer >> bits))
                buffer &= (1 << bits) - 1
            }
        }
        guard !text.isEmpty, bits < 5, buffer == 0 else { return nil }
        self.init(bytes)
    }
}
