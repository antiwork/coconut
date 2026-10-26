import Foundation

/// The folder that holds Coconut's source and its `.runtime` data. scripts/deploy.py installs and updates it.
///
/// The default is `~/Coconut`, the folder that the setup command and the docs use. `COCONUT_INSTALL_DIR` selects another one.
public struct Install: Equatable {
    public let root: URL

    public init(root: URL) {
        self.root = root.standardizedFileURL
    }

    public static func standard(environment: [String: String] = ProcessInfo.processInfo.environment,
                                home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Install {
        if let value = environment["COCONUT_INSTALL_DIR"], !value.isEmpty {
            return Install(root: URL(fileURLWithPath: (value as NSString).expandingTildeInPath, isDirectory: true))
        }
        return Install(root: home.appendingPathComponent("Coconut", isDirectory: true))
    }

    public var hostScript: URL { root.appendingPathComponent("scripts/host.py") }
    public var pairingScript: URL { root.appendingPathComponent("scripts/pairing.py") }
    public var backend: URL { root.appendingPathComponent("backend", isDirectory: true) }
    public var data: URL { root.appendingPathComponent(".runtime/host", isDirectory: true) }
    public var logs: URL { data.appendingPathComponent("logs", isDirectory: true) }
    public var tunnel: URL { data.appendingPathComponent("tunnel", isDirectory: true) }

    /// True when the folder holds the scripts that start the service.
    public func isInstalled(fileManager: FileManager = .default) -> Bool {
        fileManager.isReadableFile(atPath: hostScript.path)
    }

    /// True when the sidecar has a route from the relay. Without one, the sidecar has nothing to connect.
    public func hasRelayRoute(fileManager: FileManager = .default) -> Bool {
        fileManager.isReadableFile(atPath: tunnel.appendingPathComponent("route.json").path)
    }
}
