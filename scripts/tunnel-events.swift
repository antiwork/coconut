import Foundation
import Network

// The connection events of the relay sidecar (scripts/tunnel-sidecar.swift), in <data>/tunnel/events.log.
// scripts/tunnel.py compiles this file into the sidecar. mac/Tests/CoconutKitTests/TunnelEvents.swift is a symbolic link
// to this file, so `swift test --package-path mac` tests it.

/// Why the relay socket ended, or why a connection attempt failed.
struct DisconnectCause: Equatable {
    enum Kind: String {
        /// The relay Worker closed the socket. `code` and `reason` come from its close frame.
        case relayClosed = "relay_closed"
        /// The network or TLS stack ended the socket. `domain` and `code` name the error.
        case networkError = "network_error"
        /// The relay did not answer for `silenceLimit` seconds.
        case pingTimeout = "ping_timeout"
        /// The Mac woke from sleep. The old socket is gone.
        case wake
        /// The primary network interface or the network status changed.
        case networkChanged = "network_changed"
        case invalidURL = "invalid_url"
    }

    var kind: Kind
    var domain: String?
    var code: Int?
    var reason: String?

    init(_ kind: Kind, domain: String? = nil, code: Int? = nil, reason: String? = nil) {
        self.kind = kind
        self.domain = domain
        self.code = code
        self.reason = reason.map(TunnelEventLog.clean).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The close frame of the relay. Its payload is the UTF-8 reason.
    static func relayClosed(code: NWProtocolWebSocket.CloseCode?, reason: Data?) -> DisconnectCause {
        let number: Int?
        switch code {
        case .protocolCode(let defined)?: number = Int(defined.rawValue)
        case .applicationCode(let value)?: number = Int(value)
        case .privateCode(let value)?: number = Int(value)
        case nil: number = nil
        @unknown default: number = nil
        }
        return DisconnectCause(.relayClosed, code: number, reason: reason.map { String(decoding: $0, as: UTF8.self) })
    }

    static func network(_ error: Error) -> DisconnectCause {
        let (domain, code) = errorCode(error)
        return DisconnectCause(.networkError, domain: domain, code: code, reason: errorText(error))
    }

    /// The domain and code of an error from Network.framework or Foundation, for example ("posix", 54).
    static func errorCode(_ error: Error) -> (String, Int?) {
        if let error = error as? NWError {
            switch error {
            case .posix(let code): return ("posix", Int(code.rawValue))
            case .dns(let code): return ("dns", Int(code))
            case .tls(let status): return ("tls", Int(status))
            default: return ("network", nil)
            }
        }
        let error = error as NSError
        return (error.domain, error.code)
    }

    static func errorText(_ error: Error) -> String {
        if let error = error as? NWError { return error.debugDescription }
        return (error as NSError).localizedDescription
    }

    /// The JSON fields of this cause in an event or in status.json.
    var fields: [String: Any] {
        var fields: [String: Any] = ["cause": kind.rawValue]
        if let domain { fields["domain"] = domain }
        if let code { fields["code"] = code }
        if let reason { fields["reason"] = reason }
        return fields
    }

    /// One line for a person, without the kind. For example "code 1011: internal error", or nil.
    var detail: String? {
        var parts: [String] = []
        if let domain { parts.append(domain) }
        if let code { parts.append(kind == .relayClosed ? "code \(code)" : String(code)) }
        let head = parts.joined(separator: " ")
        switch (head.isEmpty, reason) {
        case (true, nil): return nil
        case (true, let reason?): return reason
        case (false, nil): return head
        case (false, let reason?): return "\(head): \(reason)"
        }
    }

    var summary: String { detail.map { "\(kind.rawValue) (\($0))" } ?? kind.rawValue }
}

/// A bounded log of JSON lines. The file rotates at `maxBytes` and keeps one previous file, `<name>.1`, both mode 0600.
/// `record` only formats the line. A private serial queue writes it, so slow disk I/O never blocks the tunnel.
final class TunnelEventLog {
    static let defaultMaxBytes = 256 * 1024
    static let maxReason = 160

    let url: URL
    let maxBytes: Int
    private let queue = DispatchQueue(label: "coconut.tunnel.events", qos: .utility)
    private let formatter: ISO8601DateFormatter
    private var descriptor: Int32 = -1
    private var size = 0
    /// The system call that writes bytes. Tests replace it to make short or failed writes.
    var systemWrite: (Int32, UnsafeRawPointer, Int) -> Int = { Darwin.write($0, $1, $2) }

    init(url: URL, maxBytes: Int = TunnelEventLog.defaultMaxBytes) {
        self.url = url
        self.maxBytes = maxBytes
        formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    }

    deinit {
        if descriptor >= 0 { Darwin.close(descriptor) }
    }

    /// Adds one line: {"at": <UTC time>, "event": <event>, ...fields}. The event is a fixed name from the sidecar, with no
    /// quote. The values must be strings, numbers, or Booleans.
    func record(_ event: String, _ fields: [String: Any] = [:], at date: Date = Date()) {
        guard let line = line(event, fields, at: date) else { return }
        queue.async { [self] in write(line) }
    }

    func line(_ event: String, _ fields: [String: Any], at date: Date) -> Data? {
        var line = Data(#"{"at":"\#(formatter.string(from: date))","event":"\#(event)""#.utf8)
        if !fields.isEmpty {
            guard JSONSerialization.isValidJSONObject(fields),
                  let body = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]) else { return nil }
            line.append(UInt8(ascii: ","))
            line.append(body.dropFirst())
        } else {
            line.append(UInt8(ascii: "}"))
        }
        line.append(UInt8(ascii: "\n"))
        return line
    }

    /// Waits until every recorded line is on disk. Only for tests.
    func flush() { queue.sync {} }

    private func write(_ line: Data) {
        if descriptor < 0 { open() }
        if descriptor >= 0, size > 0, size + line.count > maxBytes { rotate() }
        guard descriptor >= 0 else { return }
        append(line)
    }

    private func append(_ line: Data) {
        // A write can take only part of the line. Write the rest, and remove a partial line when a write fails.
        let start = size
        let complete = line.withUnsafeBytes { bytes -> Bool in
            guard let base = bytes.baseAddress else { return true }
            var offset = 0
            while offset < bytes.count {
                let written = systemWrite(descriptor, base + offset, bytes.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && errno == EINTR {
                    continue
                } else {
                    return false
                }
            }
            return true
        }
        if complete {
            size = start + line.count
        } else {
            ftruncate(descriptor, off_t(start))
            size = start
        }
    }

    private func open() {
        descriptor = Darwin.open(url.path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { return }
        fchmod(descriptor, 0o600)
        size = Int(max(0, lseek(descriptor, 0, SEEK_END)))
    }

    private func rotate() {
        Darwin.close(descriptor)
        descriptor = -1
        let previous = url.deletingLastPathComponent().appendingPathComponent("\(url.lastPathComponent).1")
        let failure = rename(url.path, previous.path) == 0 ? nil : errno
        open()
        // Without a rename, the file would grow without a limit. Empty it and say why. The next rotation tries again.
        guard let failure, descriptor >= 0 else { return }
        ftruncate(descriptor, 0)
        size = 0
        if let line = line("rotate_failed", ["errno": Int(failure)], at: Date()) { append(line) }
    }

    /// Text from outside the sidecar, for example the close reason of the relay or an error description. Keeps
    /// printable ASCII, removes bearer tokens and long key-like strings, and keeps at most `maxReason` characters.
    static func clean(_ text: String) -> String {
        var value = String(String.UnicodeScalarView(text.unicodeScalars.map { (0x20...0x7E).contains($0.value) ? $0 : " " }))
        value = value.replacingOccurrences(of: #"(?i)bearer\s+\S+"#, with: "Bearer [removed]", options: .regularExpression)
        // Routes, tokens, keys, and hashes are 22 or more characters of hex or base64, with at least one digit.
        value = value.replacingOccurrences(of: #"(?=[A-Za-z0-9+_=-]*[0-9])[A-Za-z0-9+_=-]{22,}"#, with: "[removed]",
                                           options: .regularExpression)
        value = value.trimmingCharacters(in: .whitespaces)
        return value.count > maxReason ? String(value.prefix(maxReason)) : value
    }
}

/// The name of an error that ended one Noise session, for example "invalidRekey" or "decryptionFailed".
func sessionErrorName(_ error: Error) -> String {
    let name = String(describing: error).prefix { $0.isLetter || $0.isNumber || $0 == "_" }
    return name.isEmpty ? String(describing: type(of: error)) : String(name)
}
