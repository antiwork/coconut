import Foundation

/// The folder that holds Coconut's source and its `.runtime` data. scripts/deploy.py installs and updates it.
///
/// The default is `~/Coconut`, the folder that the setup command and the docs use. To use another folder, set it in
/// the preferences of the app, which the app and its login items both read:
///
///     defaults write com.gumroad.coconut.mac InstallDirectory /path/to/folder
///
/// `COCONUT_INSTALL_DIR` overrides it for one command in Terminal. launchd gives the login items no such variable.
public struct Install: Equatable {
    public static let preferencesDomain = "com.gumroad.coconut.mac"
    public static let preferenceKey = "InstallDirectory"

    public let root: URL

    public init(root: URL) {
        self.root = root.standardizedFileURL
    }

    /// The home folder: `HOME`, as scripts/deploy.py and launchd use it, else the folder of the account.
    public static func homeFolder(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let value = environment["HOME"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
    }

    public static func standard(environment: [String: String] = ProcessInfo.processInfo.environment,
                                preference: String? = CFPreferencesCopyAppValue(preferenceKey as CFString,
                                                                                  preferencesDomain as CFString) as? String,
                                home: URL = Install.homeFolder()) -> Install {
        for value in [environment["COCONUT_INSTALL_DIR"], preference] {
            if let value, !value.isEmpty {
                return Install(root: URL(fileURLWithPath: (value as NSString).expandingTildeInPath, isDirectory: true))
            }
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
