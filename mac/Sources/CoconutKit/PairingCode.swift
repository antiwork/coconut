import CryptoKit
import Foundation
import SQLite3

/// A one-time pairing code from the iPhone, for `coconut pair <code>`. docs/relay-transport.md, "Pairing code from the
/// iPhone", describes the protocol.
///
/// The code is 16 bytes in 26 characters of unpadded lowercase base32. The box ID and the box key come from the code
/// with HKDF-SHA256, an empty salt, and a 32-byte output. The relay sees only the box ID, never the code or the key.
public struct PairingCode: Equatable {
    static let alphabet = Array("abcdefghijklmnopqrstuvwxyz234567")

    public let bytes: Data

    /// Read a code. Capital letters are accepted, because a chat app can change them. Return nil for anything else.
    public init?(_ text: String) {
        let characters = Array(text.lowercased())
        guard characters.count == 26 else { return nil }
        var bits = 0, count = 0
        var bytes = Data()
        for character in characters {
            guard let value = Self.alphabet.firstIndex(of: character) else { return nil }
            bits = ((bits << 5) | value) & 0xffff
            count += 5
            if count >= 8 {
                count -= 8
                bytes.append(UInt8(truncatingIfNeeded: bits >> count))
            }
        }
        // 26 characters hold 130 bits. The last 2 bits must be zero, so each code has one spelling.
        guard bits & ((1 << count) - 1) == 0 else { return nil }
        self.bytes = bytes
    }

    private func derive(_ label: String) -> Data {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: bytes), salt: Data(), info: Data(label.utf8),
                               outputByteCount: 32).withUnsafeBytes { Data($0) }
    }

    /// The 32 bytes of the box ID. The relay route uses them in lowercase hex.
    public var boxID: Data { derive("coconut rendezvous id") }
    public var boxIDHex: String { boxID.map { String(format: "%02x", $0) }.joined() }

    /// Seal a version 2 setup for the box: nonce, ciphertext and tag, with the box ID as additional data.
    public func seal(_ setup: Data) throws -> Data {
        try ChaChaPoly.seal(setup, using: SymmetricKey(data: derive("coconut rendezvous key")), authenticating: boxID).combined
    }

    /// Open a sealed setup. The iPhone does this. The tests use it to check `seal`.
    public func open(_ message: Data) throws -> Data {
        try ChaChaPoly.open(ChaChaPoly.SealedBox(combined: message), using: SymmetricKey(data: derive("coconut rendezvous key")),
                            authenticating: boxID)
    }
}

/// Sends a sealed setup to the rendezvous box of a pairing code on the relay.
public enum Rendezvous {
    public enum Failure: Error, Equatable, CustomStringConvertible {
        case expired, used, busy, relay(Int), network(String)

        public var description: String {
            switch self {
            case .expired: "The pairing code is wrong or expired. Make a new code on the iPhone, then try again."
            case .used: "The pairing code was already used. Make a new code on the iPhone, then try again."
            case .busy: "The relay is busy. Wait one minute, then try again."
            case .relay(let status): "The relay refused the setup with status \(status)."
            case .network(let message): "Coconut could not reach the relay: \(message)"
            }
        }
    }

    public static func request(relay: URL, code: PairingCode, sealed: Data) -> URLRequest {
        var request = URLRequest(url: relay.appendingPathComponent("v1/rendezvous/\(code.boxIDHex)/message"))
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.httpBody = sealed
        request.timeoutInterval = 30
        return request
    }

    public static func failure(status: Int) -> Failure? {
        switch status {
        case 200..<300: nil
        case 404: .expired
        case 409: .used
        case 429: .busy
        default: .relay(status)
        }
    }

    public static func send(relay: URL, code: PairingCode, setup: Data, session: URLSession = .shared) async throws {
        let response: URLResponse
        do {
            (_, response) = try await session.data(for: request(relay: relay, code: code, sealed: try code.seal(setup)))
        } catch let error as URLError {
            throw Failure.network(error.localizedDescription)
        }
        if let failure = failure(status: (response as? HTTPURLResponse)?.statusCode ?? 0) { throw failure }
    }
}

/// True when the service used the secret of a version 2 setup: the iPhone that got this setup paired. The service marks
/// the secret in its `pairing` table (backend/coconut/service.py, `pair_device`), so another iPhone that pairs at the same
/// time with another setup cannot look like this one. Nil when the database cannot be read.
public func pairingSecretUsed(install: Install, secret: String) -> Bool? {
    var db: OpaquePointer?
    defer { sqlite3_close(db) }
    let path = install.data.appendingPathComponent("coconut.sqlite3").path
    guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return nil }
    sqlite3_busy_timeout(db, 2000)
    var statement: OpaquePointer?
    defer { sqlite3_finalize(statement) }
    guard sqlite3_prepare_v2(db, "SELECT used FROM pairing WHERE hash = ?", -1, &statement, nil) == SQLITE_OK else { return nil }
    // The service keeps only the SHA-256 of the secret, as the iPhone sends it in POST /v1/pair.
    let hash = SHA256.hash(data: Data(secret.utf8)).map { String(format: "%02x", $0) }.joined()
    let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    sqlite3_bind_text(statement, 1, hash, -1, transient)
    switch sqlite3_step(statement) {
    case SQLITE_ROW: return sqlite3_column_int(statement, 0) == 1
    case SQLITE_DONE: return false
    default: return nil
    }
}
