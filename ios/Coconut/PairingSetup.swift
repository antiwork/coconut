import Foundation

// A version 2 setup link from `scripts/pairing.py`:
// coconut://pair?setup=<base64url {version:2, relay, route, mac_key, secret, expires, name}>
struct PairingSetup: Equatable, Sendable {
    let relay: URL
    let route: String
    let macKey: Data
    let secret: Data
    // The secret as the link spells it. POST /v1/pair sends it back as the code.
    let code: String
    let expires: Date
    let name: String

    static let linkPrefix = "coconut://pair?"
    private static let fields: Set<String> = ["version", "relay", "route", "mac_key", "secret", "expires", "name"]

    var endpoint: TunnelEndpoint { TunnelEndpoint(relay: relay, route: route, macKey: macKey) }

    static func isLink(_ url: URL) -> Bool { url.scheme?.lowercased() == "coconut" && url.host?.lowercased() == "pair" }

    // A setup link carries the same JSON as the QR code, encoded as base64url in its `setup` field.
    static func parse(_ payload: String) throws -> Self {
        // The size limit applies to the whole input, before any trimming.
        guard payload.utf8.count <= 4_096 else { throw PairingSetupError.invalidPayload }
        let text = payload.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("coconut://") { return try parse(json: try json(fromLink: text)) }
        return try parse(json: Data(text.utf8))
    }

    private static func json(fromLink link: String) throws -> Data {
        guard let url = URL(string: link), isLink(url),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let items = parts.queryItems, items.count == 1, items[0].name == "setup",
              let encoded = items[0].value, !encoded.isEmpty else { throw PairingSetupError.invalidPayload }
        // Earlier links can keep their base64 padding.
        let unpadded = encoded.hasSuffix("==") ? String(encoded.dropLast(2)) : encoded.hasSuffix("=") ? String(encoded.dropLast()) : encoded
        guard let data = Data(base64URL: unpadded) else { throw PairingSetupError.invalidPayload }
        return data
    }

    private static func parse(json data: Data) throws -> Self {
        // A version 1 setup used Tailscale and a TLS certificate. This app pairs only through the relay.
        guard let probe = try? JSONDecoder().decode(VersionProbe.self, from: data) else { throw PairingSetupError.invalidPayload }
        if probe.version == 1 { throw PairingSetupError.legacyLink }
        guard probe.version == 2 else { throw PairingSetupError.unsupportedVersion }
        let fields: Fields
        do { fields = try JSONDecoder().decode(Fields.self, from: data) }
        catch { throw PairingSetupError.invalidPayload }
        guard let relay = validRelay(fields.relay) else { throw PairingSetupError.invalidRelay }
        guard fields.route.utf8.count == 64, fields.route.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
            throw PairingSetupError.invalidRoute
        }
        guard let macKey = Data(base64URL: fields.macKey), macKey.count == 32,
              let secret = Data(base64URL: fields.secret), secret.count == 32 else { throw PairingSetupError.invalidKey }
        let name = fields.name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name.count <= 80, !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw PairingSetupError.invalidName
        }
        return Self(relay: relay, route: fields.route, macKey: macKey, secret: secret, code: fields.secret,
                    expires: Date(timeIntervalSince1970: TimeInterval(fields.expires)), name: name)
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
    case invalidPayload, unsupportedVersion, legacyLink, invalidRelay, invalidRoute, invalidKey, invalidName

    var errorDescription: String? {
        switch self {
        case .invalidPayload: "This is not a Coconut setup code. Ask your Hermes for a new pairing link."
        case .unsupportedVersion: "This setup code needs a different Coconut version. Update the app and try again."
        case .legacyLink: "Coconut no longer needs Tailscale. Ask your Hermes for a new pairing link."
        case .invalidRelay: "The setup code has an invalid relay address. Ask your Hermes for a new pairing link."
        case .invalidRoute, .invalidKey: "The setup code is damaged. Ask your Hermes for a new pairing link."
        case .invalidName: "The setup code has an invalid Mac name. Ask your Hermes for a new pairing link."
        }
    }
}
