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

    enum CodingKeys: String, CodingKey {
        case install, service, hermes, relay
        case hermesVersion = "hermes_version"
        case hermesReason = "hermes_reason"
    }
}

public struct StatusReader {
    /// The main listener of the service. After the relay-only change, it serves plain HTTP on 127.0.0.1 only.
    public static let healthURL = URL(string: "http://127.0.0.1:8960/health")!

    public let install: Install
    public let fileManager: FileManager

    public init(install: Install, fileManager: FileManager = .default) {
        self.install = install
        self.fileManager = fileManager
    }

    /// Read the status files. `isAlive` checks a process ID, and `healthy` says whether `/health` answered.
    public func read(isAlive: (Int32) -> Bool = StatusReader.isAlive, healthy: Bool) -> Status {
        let hermes = object(at: install.data.appendingPathComponent("hermes.json"))
        return Status(install: install.root.path, service: service(isAlive: isAlive, healthy: healthy),
                      hermes: hermes?["status"] as? String ?? "missing",
                      hermesVersion: hermes?["version"] as? String, hermesReason: hermes?["reason"] as? String,
                      relay: relay())
    }

    func service(isAlive: (Int32) -> Bool, healthy: Bool) -> Status.Service {
        guard install.isInstalled(fileManager: fileManager) else { return .notInstalled }
        guard let record = object(at: install.data.appendingPathComponent("process.json")),
              let pid = (record["pid"] as? NSNumber)?.int32Value, pid > 0, isAlive(pid) else { return .stopped }
        return healthy ? .running : .starting
    }

    func relay() -> String {
        guard install.hasRelayRoute(fileManager: fileManager) else { return "not_installed" }
        return object(at: install.tunnel.appendingPathComponent("status.json"))?["state"] as? String ?? "stopped"
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
