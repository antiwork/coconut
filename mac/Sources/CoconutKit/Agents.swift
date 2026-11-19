import Foundation

/// The two login items of Coconut.app. Each one is a launch agent plist in `Contents/Library/LaunchAgents`, which the
/// app registers with `SMAppService.agent(plistName:)`. Both start `Contents/Helpers/coconut` with one command:
/// `service` for the service and `sidecar` for the relay.
public enum Agent: String, CaseIterable, Sendable {
    case service = "com.gumroad.coconut.service"
    case relay = "com.gumroad.coconut.relay"

    public var plistName: String { "\(rawValue).plist" }
}

/// The LaunchAgents that `scripts/host.py install-service` and `scripts/tunnel.py install` write. While one of them
/// exists, it runs Coconut, and the app does not register its own login items: two copies of the service would share
/// one database.
public enum LegacyAgents {
    public static let labels = ["local.coconut.agent", "com.gumroad.coconut.tunnel"]

    public static func present(home: URL = FileManager.default.homeDirectoryForCurrentUser,
                               fileManager: FileManager = .default) -> [String] {
        labels.filter { label in
            let path = home.appendingPathComponent("Library/LaunchAgents/\(label).plist").path
            return fileManager.fileExists(atPath: path)
        }
    }
}
