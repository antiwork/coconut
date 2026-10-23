import Foundation
import Darwin

struct PairingSetup: Equatable, Sendable {
    let serverURL: String
    let certificateSHA256: String
    let pairingCode: String

    static let linkPrefix = "coconut://pair?"

    // The Mac address the person checks before pairing: host and port, without the scheme.
    var origin: String { String(serverURL.dropFirst("https://".count)).trimmingCharacters(in: CharacterSet(charactersIn: "/")) }

    static func isLink(_ url: URL) -> Bool { url.scheme?.lowercased() == "coconut" && url.host?.lowercased() == "pair" }

    // A setup link carries the same JSON as the QR code, encoded as base64url in its `setup` field.
    static func parse(_ payload: String) throws -> Self {
        // The size limit applies to the whole input, before any trimming.
        guard payload.utf8.count <= 4_096 else { throw PairingSetupError.invalidPayload }
        let text = payload.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("coconut://") { return try parse(json: try json(fromLink: text)) }
        return try parse(json: text)
    }

    private static func json(fromLink link: String) throws -> String {
        guard let url = URL(string: link), isLink(url),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let items = parts.queryItems, items.count == 1, items[0].name == "setup",
              var encoded = items[0].value, !encoded.isEmpty else { throw PairingSetupError.invalidPayload }
        encoded = encoded.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
        guard let data = Data(base64Encoded: encoded), let json = String(data: data, encoding: .utf8) else {
            throw PairingSetupError.invalidPayload
        }
        return json
    }

    private static func parse(json payload: String) throws -> Self {
        guard let data = payload.data(using: .utf8) else { throw PairingSetupError.invalidPayload }
        let fields: Fields
        do { fields = try JSONDecoder().decode(Fields.self, from: data) }
        catch { throw PairingSetupError.invalidPayload }
        guard fields.version == 1 else { throw PairingSetupError.unsupportedVersion }
        guard validServerURL(fields.serverURL) else { throw PairingSetupError.invalidAddress }
        guard fields.certificateSHA256.utf8.count == 64,
              fields.certificateSHA256.utf8.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) }) else {
            throw PairingSetupError.invalidFingerprint
        }
        guard fields.pairingCode.utf8.count == 10,
              fields.pairingCode.utf8.allSatisfy({ (48...57).contains($0) }) else {
            throw PairingSetupError.invalidCode
        }
        return Self(serverURL: fields.serverURL, certificateSHA256: fields.certificateSHA256.lowercased(), pairingCode: fields.pairingCode)
    }

    private static func validServerURL(_ value: String) -> Bool {
        guard value.utf8.allSatisfy({ (33...126).contains($0) }),
              !value.contains("\\"), !value.contains("%"),
              value.lowercased().hasPrefix("https://"),
              let parts = URLComponents(string: value), parts.scheme?.lowercased() == "https",
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/", let url = parts.url,
              let rawHost = parts.host, !rawHost.isEmpty else { return false }
        let authority = value.dropFirst(8).split(separator: "/", omittingEmptySubsequences: false).first ?? ""
        guard !authority.hasSuffix(":"), parts.port.map({ (1...65_535).contains($0) }) ?? true,
              let urlHost = url.host else { return false }
        let host = rawHost.hasPrefix("[") && rawHost.hasSuffix("]") ? String(rawHost.dropFirst().dropLast()) : rawHost
        let resolvedHost = urlHost.hasPrefix("[") && urlHost.hasSuffix("]") ? String(urlHost.dropFirst().dropLast()) : urlHost
        guard host == resolvedHost else { return false }
        if host.contains(":") {
            var address = in6_addr()
            return host.withCString { inet_pton(AF_INET6, $0, &address) == 1 }
        }
        if host.utf8.allSatisfy({ (48...57).contains($0) || $0 == 46 }) {
            var address = in_addr()
            return host.withCString { inet_pton(AF_INET, $0, &address) == 1 }
        }
        guard host.utf8.count <= 253 else { return false }
        return host.split(separator: ".", omittingEmptySubsequences: false).allSatisfy { label in
            !label.isEmpty && label.utf8.count <= 63 && !label.hasPrefix("-") && !label.hasSuffix("-")
                && label.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 }
        }
    }

    private struct Fields: Decodable {
        let version: Int
        let serverURL: String
        let certificateSHA256: String
        let pairingCode: String

        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: FieldKey.self)
            guard Set(values.allKeys.map(\.stringValue)) == ["version", "server_url", "certificate_sha256", "pairing_code"] else {
                throw PairingSetupError.invalidPayload
            }
            version = try values.decode(Int.self, forKey: FieldKey("version"))
            serverURL = try values.decode(String.self, forKey: FieldKey("server_url"))
            certificateSHA256 = try values.decode(String.self, forKey: FieldKey("certificate_sha256"))
            pairingCode = try values.decode(String.self, forKey: FieldKey("pairing_code"))
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

struct PairingSetupDraft: Equatable, Sendable {
    var serverURL = ""
    var fingerprint = ""
    var pairCode = ""

    mutating func importPayload(_ payload: String) throws {
        let setup = try PairingSetup.parse(payload)
        self = Self(serverURL: setup.serverURL, fingerprint: setup.certificateSHA256, pairCode: setup.pairingCode)
    }
}

enum PairingSetupError: LocalizedError {
    case invalidPayload, unsupportedVersion, invalidAddress, invalidFingerprint, invalidCode

    var errorDescription: String? {
        switch self {
        case .invalidPayload: "This is not a Coconut setup code. Get a new code from your Mac."
        case .unsupportedVersion: "This setup code needs a different Coconut version. Update the app and try again."
        case .invalidAddress: "The setup code must contain a secure Mac address without a path or account details."
        case .invalidFingerprint: "The setup code has an invalid certificate fingerprint. Get a new code from your Mac."
        case .invalidCode: "The setup code must contain the ten-digit pairing code from your Mac."
        }
    }
}
