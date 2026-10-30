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
