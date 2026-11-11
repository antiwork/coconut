import Foundation

/// The Coconut source that this app bundle carries: `Contents/Resources/coconut-source.tar.gz` from scripts/remote.py,
/// its package ID, and the scripts/deploy.py that installs it. scripts/mac_app.py writes all three.
public struct BundledSource: Equatable {
    public let package: URL
    public let deployScript: URL
    public let packageID: String

    public init?(resources: URL) {
        package = resources.appendingPathComponent("coconut-source.tar.gz")
        deployScript = resources.appendingPathComponent("deploy.py")
        guard let data = try? Data(contentsOf: resources.appendingPathComponent("package.json")),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let packageID = object["package_id"] as? String, !packageID.isEmpty else { return nil }
        self.packageID = packageID
    }

    /// True when the install folder has no complete install of this package.
    public func isNewer(than install: Install) -> Bool {
        install.installedPackageID() != packageID
    }
}

extension Install {
    /// The package that scripts/deploy.py installed last, from `.coconut-install.json`. Nil without a complete install.
    public func installedPackageID() -> String? {
        guard let data = try? Data(contentsOf: root.appendingPathComponent(".coconut-install.json")),
              let marker = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              marker["state"] as? String == "installed" else { return nil }
        return marker["package_id"] as? String
    }

    /// True for a Git checkout that scripts/setup_coconut.py set up: it has `.git` and no install of deploy.py. Its owner
    /// updates the source with git pull, so the app does not install its own source over it.
    public func isCheckout(fileManager: FileManager = .default) -> Bool {
        fileManager.fileExists(atPath: root.appendingPathComponent(".git").path)
            && !fileManager.fileExists(atPath: root.appendingPathComponent(".coconut-install.json").path)
    }

    /// The install folder relative to the home folder, as `deploy.py --directory` needs it. Nil outside the home folder.
    public func relativePath(home: URL = Install.homeFolder()) -> String? {
        let base = home.standardizedFileURL.path
        let path = root.path
        guard path.hasPrefix(base + "/"), path.count > base.count + 1 else { return nil }
        return String(path.dropFirst(base.count + 1))
    }

    /// The hold of scripts/host.py (UPDATE_HOLD): while an update replaces the source, the service must not start.
    public var updateHold: URL { data.appendingPathComponent("update-hold") }

    /// True while the hold exists and its end time is in the future. A hold without a valid time does not hold.
    public func isHeld(now: Date = Date()) -> Bool {
        guard let text = try? String(contentsOf: updateHold, encoding: .utf8),
              let end = Double(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        return end > now.timeIntervalSince1970
    }
}

extension Launch {
    /// Install or update the bundled source with scripts/deploy.py. The package goes to standard input.
    ///
    /// deploy.py drains the running turns, stops the service, replaces the source, checks Hermes and health, and
    /// restores the earlier source when a check fails. With COCONUT_APP_MANAGED, a first install starts the login item
    /// instead of a LaunchAgent. COCONUT_SIDECAR names the sidecar of this bundle for scripts/tunnel.py, and
    /// COCONUT_TEAM_HELPER names its team helper for scripts/team.py, so neither script compiles. With
    /// `setUp`, the person asked for the setup. deploy.py now sets Coconut's own Hermes entries at each install, so
    /// COCONUT_SET_UP no longer changes what it does. In team-only mode, COCONUT_MODE=team makes deploy.py skip each
    /// step that finds, checks or changes Hermes, and the relay sidecar.
    public static func update(install: Install, source: BundledSource, directory: String, python: URL, sidecar: URL,
                              teamHelper: URL? = nil, environment: [String: String],
                              home: URL = Install.homeFolder(), setUp: Bool = false,
                              mode: InstallMode = .full) -> Launch {
        var environment = environment
        mode.apply(to: &environment)
        environment["COCONUT_APP_MANAGED"] = "1"
        if setUp {
            environment["COCONUT_SET_UP"] = "1"
        }
        environment["COCONUT_SIDECAR"] = sidecar.path
        if let teamHelper {
            environment["COCONUT_TEAM_HELPER"] = teamHelper.path
        }
        environment["PYTHONUNBUFFERED"] = "1"
        return Launch(executable: python,
                      arguments: [python.path, source.deployScript.path, "receive", "--directory", directory],
                      // deploy.py creates the install folder and its parents. The home folder exists already.
                      environment: environment, directory: home, standardOutput: nil, standardError: nil)
    }

    /// Hand the service from the LaunchAgents of host.py and tunnel.py to the login items (host.migrate_to_app).
    public static func migrate(install: Install, python: URL, environment: [String: String]) -> Launch {
        Launch(executable: python, arguments: [python.path, install.hostScript.path, "migrate-to-app"],
               environment: environment, directory: install.root, standardOutput: nil, standardError: nil)
    }
}

/// The last line `COCONUT_DEPLOY_RESULT=<json>` of scripts/deploy.py.
public struct DeployResult: Equatable {
    public let ok: Bool
    public let error: String?
    public let packageID: String?
    /// The error of the relay step. deploy.py keeps a complete update when only that step fails.
    public let relayError: String?

    public init?(output: String) {
        guard let line = output.split(whereSeparator: \.isNewline).last(where: { $0.hasPrefix("COCONUT_DEPLOY_RESULT=") }),
              let object = (try? JSONSerialization.jsonObject(with: Data(line.dropFirst("COCONUT_DEPLOY_RESULT=".count).utf8)))
                as? [String: Any],
              let ok = object["ok"] as? Bool else { return nil }
        self.ok = ok
        error = object["error"] as? String
        packageID = object["package_id"] as? String
        let tunnel = object["tunnel"] as? [String: Any]
        relayError = tunnel?["ok"] as? Bool == false ? (tunnel?["error"] as? String ?? "The relay step failed.") : nil
    }
}
