import CryptoKit
import Foundation

// A one-time pairing code that this iPhone makes. The person sends it to Hermes in one message, and on the Mac
// `coconut pair <code>` seals a version 2 setup for this iPhone in a rendezvous box on the relay.
// docs/relay-transport.md, "Pairing code from the iPhone", describes the protocol.
struct PairingCode: Equatable, Sendable {
    let bytes: Data

    init(bytes: Data) { self.bytes = bytes }

    // 16 random bytes: 128 bits.
    static func make() -> Self {
        Self(bytes: Data((0..<16).map { _ in UInt8.random(in: .min ... .max) }))
    }

    // 26 characters of unpadded lowercase base32. No redaction rule of Hermes matches it.
    var text: String { bytes.base32String }

    var boxID: Data { TunnelKeys.derive(bytes, "coconut rendezvous id") }
    private var boxKey: SymmetricKey { SymmetricKey(data: TunnelKeys.derive(bytes, "coconut rendezvous key")) }

    // The message from the Mac: nonce, ciphertext and tag, with the box ID as additional data.
    func open(_ sealed: Data) throws -> Data {
        try ChaChaPoly.open(ChaChaPoly.SealedBox(combined: sealed), using: boxKey, authenticating: boxID)
    }

    // The message that the person sends to Hermes. Resources/hermes-pairing-message.txt holds the text.
    var hermesMessage: String {
        Self.template.replacingOccurrences(of: "{code}", with: text).replacingOccurrences(of: "{device}", with: Self.device)
    }

    /// The device that pairs, as the message names it.
    private static var device: String {
        #if os(macOS)
        "Mac"
        #else
        "iPhone"
        #endif
    }

    static let template: String = {
        guard let url = Bundle.main.url(forResource: "hermes-pairing-message", withExtension: "txt"),
              let text = try? String(contentsOf: url, encoding: .utf8) else { return "" }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }()
}

// The code of the pairing that waits for a Mac, with the phone token that only this iPhone knows. It survives a
// restart of the app: iOS can end Coconut while the person is in Telegram.
struct PendingPairing: Codable, Equatable, Sendable {
    static let cacheName = "pairing-code.json"
    // The relay keeps a box for 600 seconds. The iPhone stops a little earlier, so a late Mac finds no box.
    static let lifetime: TimeInterval = 590

    let code: Data
    let phoneToken: String
    let expires: Date

    var pairingCode: PairingCode { PairingCode(bytes: code) }

    static func make(now: Date = Date()) -> Self {
        Self(code: PairingCode.make().bytes, phoneToken: Data((0..<32).map { _ in UInt8.random(in: .min ... .max) }).hexString,
             expires: now.addingTimeInterval(lifetime))
    }

    func isLive(at date: Date = Date()) -> Bool { date < expires }
}

enum RendezvousError: Error, Equatable {
    // The box expired, or another phone made a box with this code.
    case expired
    case relay(Int)
}

// The phone side of the rendezvous box on the relay.
struct Rendezvous: Sendable {
    typealias Send = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)

    let relay: URL
    let send: Send

    // The relay of the app: CoconutPushRelayURL in Info.plist.
    static var appRelay: URL? {
        guard let text = Bundle.main.object(forInfoDictionaryKey: "CoconutPushRelayURL") as? String,
              let url = URL(string: text), url.scheme == "https", url.host != nil else { return nil }
        return url
    }

    static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        // A read waits up to 25 seconds on the relay.
        configuration.timeoutIntervalForRequest = 40
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        return URLSession(configuration: configuration)
    }()

    static func urlSession(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, response)
    }

    init(relay: URL, send: @escaping Send = Rendezvous.urlSession) {
        self.relay = relay
        self.send = send
    }

    private func request(_ pending: PendingPairing, method: String, wait: Int? = nil) -> URLRequest {
        var url = relay.appendingPathComponent("v1/rendezvous/\(pending.pairingCode.boxID.hexString)")
        if let wait { url.append(queryItems: [URLQueryItem(name: "wait", value: String(wait))]) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(pending.phoneToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    // Make the box. A repeat with the same phone token is fine, for example after a restart of the app.
    func open(_ pending: PendingPairing) async throws {
        let (_, response) = try await send(request(pending, method: "PUT"))
        switch response.statusCode {
        case 200, 201: return
        case 409: throw RendezvousError.expired
        default: throw RendezvousError.relay(response.statusCode)
        }
    }

    // Wait up to 25 seconds for the sealed setup. Nil means that no setup came yet.
    func read(_ pending: PendingPairing, wait: Int = 25) async throws -> Data? {
        let (data, response) = try await send(request(pending, method: "GET", wait: wait))
        switch response.statusCode {
        case 200: return data
        case 204: return nil
        case 401, 404: throw RendezvousError.expired
        default: throw RendezvousError.relay(response.statusCode)
        }
    }

    // Remove the box after the setup opened. A failure is harmless: the box expires.
    func close(_ pending: PendingPairing) async {
        _ = try? await send(request(pending, method: "DELETE"))
    }
}

extension Data {
    // Unpadded lowercase base32 (RFC 4648), the reverse of init?(base32:).
    var base32String: String {
        let alphabet = Array("abcdefghijklmnopqrstuvwxyz234567")
        var text = ""
        var buffer = 0, bits = 0
        for byte in self {
            buffer = buffer << 8 | Int(byte)
            bits += 8
            while bits >= 5 {
                bits -= 5
                text.append(alphabet[(buffer >> bits) & 31])
            }
            buffer &= (1 << bits) - 1
        }
        if bits > 0 { text.append(alphabet[(buffer << (5 - bits)) & 31]) }
        return text
    }
}
