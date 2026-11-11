import Foundation

/// What the menu bar shows about this Mac. The service writes the files that this reads; see backend/coconut/server.py
/// (process.json), backend/coconut/hermes_runtime.py (hermes.json) and scripts/tunnel-sidecar.swift (status.json).
public struct Status: Codable, Equatable {
    public enum Service: String, Codable {
        /// No Coconut folder at the install path.
        case notInstalled = "not_installed"
        case stopped
        /// The process runs, but `/health` did not answer.
        case starting
        case running
    }

    public var install: String
    public var service: Service
    /// "ok", "missing", "failed" and the other states of hermes_runtime.status.
    public var hermes: String
    public var hermesVersion: String?
    public var hermesReason: String?
    /// The state of the relay sidecar, for example "connected", or "not_installed" without a route.
    public var relay: String
    /// The end of the last connected relay socket, from status.json. The sidecar forgets it when it restarts.
    public var relayLastDisconnect: RelayDisconnect? = nil
    /// The connected relay sockets after the first one, since the sidecar started.
    public var relayReconnects: Int? = nil
    /// The notification previews of each phone that reported them, from push-previews.json (backend/coconut/push.py).
    public var previews: [Previews]? = nil
    /// Why the service does not start, while it is stopped (StartError). Nil while it runs or without a known cause.
    public var serviceError: String? = nil

    public struct Previews: Codable, Equatable {
        public var name: String
        /// For example "failing since 2026-10-30T18:07:00Z: keychain read -34018".
        public var line: String
    }

    /// `cause` is one of the causes in scripts/tunnel-events.swift, for example "ping_timeout". `detail` holds the close
    /// code and reason of the relay, or the domain and code of a network error.
    public struct RelayDisconnect: Codable, Equatable {
        public var at: String
        public var cause: String
        public var detail: String?
    }

    enum CodingKeys: String, CodingKey {
        case install, service, hermes, relay
        case hermesVersion = "hermes_version"
        case hermesReason = "hermes_reason"
        case relayLastDisconnect = "relay_last_disconnect"
        case relayReconnects = "relay_reconnects"
        case previews
        case serviceError = "service_error"
    }
}

public struct StatusReader {
    /// The tunnel listener of the service, which the relay sidecar uses. It serves plain HTTP on 127.0.0.1 (host.TUNNEL_PORT).
    public static let healthURL = URL(string: "http://127.0.0.1:8961/health")!

    public let install: Install
    public let fileManager: FileManager

    public init(install: Install, fileManager: FileManager = .default) {
        self.install = install
        self.fileManager = fileManager
    }

    /// Read the status files. `isAlive` checks a process ID, and `healthy` says whether `/health` answered.
    public func read(isAlive: (Int32) -> Bool = StatusReader.isAlive, healthy: Bool) -> Status {
        let hermes = object(at: install.data.appendingPathComponent("hermes.json"))
        var status = Status(install: install.root.path, service: service(isAlive: isAlive, healthy: healthy),
                            hermes: hermes?["status"] as? String ?? "missing",
                            hermesVersion: hermes?["version"] as? String, hermesReason: hermes?["reason"] as? String,
                            relay: relay(isAlive: isAlive))
        // A stopped sidecar leaves its last disconnect behind, and that can explain why it stopped.
        if status.relay != "not_installed", let tunnel = object(at: install.tunnel.appendingPathComponent("status.json")) {
            status.relayReconnects = (tunnel["reconnects"] as? NSNumber)?.intValue
            if let last = tunnel["last_disconnect"] as? [String: Any], let at = last["at"] as? String,
               let cause = last["cause"] as? String {
                status.relayLastDisconnect = .init(at: at, cause: cause, detail: last["detail"] as? String)
            }
        }
        if let phones = object(at: install.data.appendingPathComponent("push-previews.json"))?["phones"] as? [[String: Any]] {
            status.previews = phones.compactMap { phone in
                guard let name = phone["name"] as? String, let line = phone["line"] as? String else { return nil }
                return .init(name: name, line: line)
            }
        }
        if status.service == .stopped {
            status.serviceError = StartError(install: install)?.error
        }
        return status
    }

    func service(isAlive: (Int32) -> Bool, healthy: Bool) -> Status.Service {
        guard install.isInstalled(fileManager: fileManager) else { return .notInstalled }
        guard let record = object(at: install.data.appendingPathComponent("process.json")),
              let pid = (record["pid"] as? NSNumber)?.int32Value, pid > 0, isAlive(pid) else { return .stopped }
        return healthy ? .running : .starting
    }

    func relay(isAlive: (Int32) -> Bool) -> String {
        guard install.hasRelayRoute(fileManager: fileManager) else { return "not_installed" }
        // The sidecar leaves its last status.json when it stops. Its pid says whether that state is still true.
        guard let status = object(at: install.tunnel.appendingPathComponent("status.json")),
              let pid = (status["pid"] as? NSNumber)?.int32Value, pid > 0, isAlive(pid) else { return "stopped" }
        return status["state"] as? String ?? "stopped"
    }

    func object(at url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    public static func isAlive(_ pid: Int32) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    /// Ask the service's `/health`. A short timeout keeps the menu responsive.
    public static func checkHealth(session: URLSession = .shared, url: URL = healthURL) async -> Bool {
        var request = URLRequest(url: url, timeoutInterval: 2)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (_, response) = try? await session.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }
}

/// The cause of the last start that the service refused, which it leaves in `<data>/start-error.json`
/// (backend/coconut/server.py, write_start_error). A start that succeeds removes the file.
public struct StartError: Equatable, Sendable {
    /// The code of the refusal, for example `data_folder_mode`.
    public var code: String
    public var error: String
    public var at: Date

    /// The causes that a restart cannot heal: only a person can change the data folder.
    static let lasting: Set<String> = ["data_folder_mode"]
    /// While such a cause is new, the login item waits this long before it starts the service again. launchd would
    /// start it every 5 seconds, and each start would write the same refusal to the error log.
    public static let lastingWait: TimeInterval = 300

    public init(code: String, error: String, at: Date) {
        self.code = code
        self.error = error
        self.at = at
    }

    public init?(install: Install) {
        guard let data = try? Data(contentsOf: install.data.appendingPathComponent("start-error.json")),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let code = object["code"] as? String, let error = object["error"] as? String,
              let at = (object["at"] as? NSNumber)?.doubleValue else { return nil }
        self.init(code: code, error: error, at: Date(timeIntervalSince1970: at))
    }

    /// The code of the tool when it finds no Python for the service. The service never starts, so the tool leaves the
    /// cause itself.
    public static let noPython = "no_python"

    /// Leave a cause in the data folder, as the service does. A missing data folder gets none: Coconut is not
    /// installed, and the status says that.
    public static func write(code: String, error: String, install: Install, now: Date = Date()) {
        guard FileManager.default.fileExists(atPath: install.data.path),
              let data = try? JSONSerialization.data(withJSONObject: ["code": code, "error": error,
                                                                      "at": now.timeIntervalSince1970]) else { return }
        let file = install.data.appendingPathComponent("start-error.json")
        guard (try? data.write(to: file, options: .atomic)) != nil else { return }
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    /// How long the login item waits before it starts the service again, or nil to start at once. The wait ends after
    /// `lastingWait`, so a person who fixed the folder sees the service start within five minutes, without a click.
    public func wait(now: Date = Date()) -> TimeInterval? {
        guard Self.lasting.contains(code) else { return nil }
        let left = Self.lastingWait - now.timeIntervalSince(at)
        return left > 0 ? min(left, Self.lastingWait) : nil
    }
}
