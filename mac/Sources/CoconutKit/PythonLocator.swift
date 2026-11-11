import CryptoKit
import Foundation

/// Finds a Python that can start `host.py`.
///
/// host.py needs only the standard library. It then finds the person's Hermes and starts the service again under the
/// Python of that Hermes (backend/coconut/hermes_runtime.py). So this search does not decide which Hermes runs. It looks
/// for a Python that does not move with a Hermes update first, and then for the Python of
/// the Hermes from the official installer. It never uses /usr/bin/python3: without the Command Line Tools, that
/// command opens an installation dialog, and their Python is older than 3.11.
public struct PythonLocator {
    public static let minimumVersion = (3, 11)

    public let environment: [String: String]
    public let home: URL
    public let fileManager: FileManager
    /// A team-only install searches no Hermes, also not for its Python: only the Python of Homebrew.
    public let mode: InstallMode

    public init(environment: [String: String] = ProcessInfo.processInfo.environment,
                home: URL = FileManager.default.homeDirectoryForCurrentUser, fileManager: FileManager = .default,
                mode: InstallMode = .full) {
        self.environment = environment
        self.home = home
        self.fileManager = fileManager
        self.mode = mode
    }

    /// The Pythons to try, in order, without duplicates. Nothing here runs a Python.
    public func candidates() -> [URL] {
        let homebrew = [URL(fileURLWithPath: "/opt/homebrew/bin/python3"), URL(fileURLWithPath: "/usr/local/bin/python3")]
        guard mode == .full else { return homebrew }
        var found: [URL] = []
        if let explicit = environment["COCONUT_HERMES_PYTHON"], !explicit.isEmpty {
            found.append(URL(fileURLWithPath: (explicit as NSString).expandingTildeInPath))
        }
        found += homebrew
        let source = home.appendingPathComponent(".hermes/hermes-agent", isDirectory: true)
        for name in ["venv", ".venv"] {
            found.append(source.appendingPathComponent("\(name)/bin/python"))
        }
        if let committed = committedPython(source: source) {
            found.append(committed)
        }
        var seen = Set<String>()
        return found.filter { seen.insert($0.path).inserted }
    }

    /// The Python of the dependency environment that the Hermes installer committed for its source folder.
    /// The installer keeps it in `<Hermes home>/installs/<first 16 hex digits of sha256(source folder)>/facts.json`.
    func committedPython(source: URL) -> URL? {
        let digest = SHA256.hash(data: Data(source.resolvingSymlinksInPath().path.utf8))
        let key = digest.map { String(format: "%02x", $0) }.joined().prefix(16)
        var homes: [URL] = []
        if let value = environment["HERMES_HOME"], !value.isEmpty {
            homes.append(URL(fileURLWithPath: (value as NSString).expandingTildeInPath, isDirectory: true))
        }
        homes.append(home.appendingPathComponent(".hermes", isDirectory: true))
        for folder in homes {
            let facts = folder.appendingPathComponent("installs/\(key)/facts.json")
            guard let data = try? Data(contentsOf: facts),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let packages = object["packages"] as? [String: Any],
                  let venv = packages["venv"] as? [String: Any],
                  let environment = venv["environment"] as? String else { continue }
            let python = URL(fileURLWithPath: environment).appendingPathComponent("bin/python")
            if fileManager.isExecutableFile(atPath: python.path) {
                return python
            }
        }
        return nil
    }

    /// The first candidate that exists and that `accepts` approves. The default check runs the Python once.
    public func locate(accepts: (URL) -> Bool = PythonLocator.isSupported) -> URL? {
        candidates().first { fileManager.isExecutableFile(atPath: $0.path) && accepts($0) }
    }

    /// True when the Python runs and is at least `minimumVersion`.
    public static func isSupported(_ python: URL) -> Bool {
        let process = Process()
        process.executableURL = python
        let (major, minor) = minimumVersion
        process.arguments = ["-I", "-c", "import sys; sys.exit(0 if sys.version_info >= (\(major), \(minor)) else 1)"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return false
        }
        process.waitUntilExit()
        return process.terminationStatus == 0
    }
}
