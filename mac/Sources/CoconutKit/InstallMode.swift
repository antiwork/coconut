import Foundation
import SQLite3

/// The mode of the install of Coconut.app on this Mac.
///
/// A full install serves the iPhone and the Mac client, runs the bots of the person's Hermes and adds Coconut's entries
/// to the Hermes config. A team-only install is for a Mac with no person at it (docs/install-and-update.md, "A Mac without
/// a person"): its service keeps its teams in step and saves the pages of the bots that the Mac declares
/// (backend/coconut/team_only.py). It touches nothing of Hermes, registers no relay sidecar and pairs no phone.
///
/// The install keeps its mode in the preferences of the app, next to `InstallDirectory`, so the app, the tool, the login
/// item and each update read one value. Only `Coconut.app --set-up --team-only` writes "team". The store of the service
/// has its own mark (service_base.MODE_MARK). Before a step that can reach Hermes, the app and the tool read that mark
/// too (TeamFolderMark), so a lost preference never makes a team-only Mac a full install.
public enum InstallMode: String, Sendable, Equatable {
    case full
    case team

    public static let preferenceKey = "InstallMode"
    /// The variable of the service (server.py), of deploy.py and of a command that `COCONUT_INSTALL_DIR` points at a
    /// scratch folder.
    public static let environmentKey = "COCONUT_MODE"

    /// The rule of Install.standard: a command for another folder (`COCONUT_INSTALL_DIR`) takes `COCONUT_MODE` and reads
    /// no preference of the app. Each other command reads the preference.
    public static func standard(environment: [String: String] = ProcessInfo.processInfo.environment,
                                preference: @autoclosure () -> String? = CFPreferencesCopyAppValue(
                                    preferenceKey as CFString, Install.preferencesDomain as CFString) as? String)
        -> InstallMode {
        if let folder = environment["COCONUT_INSTALL_DIR"], !folder.isEmpty {
            return environment[environmentKey] == team.rawValue ? .team : .full
        }
        return preference() == team.rawValue ? .team : .full
    }

    /// The value of `COCONUT_MODE` for a command of this mode. Nil removes the variable, so a full command never takes
    /// "team" from the shell that started the tool.
    public var environmentValue: String? { self == .team ? rawValue : nil }

    /// The value of the preference. Nil removes it: a full install has none, as before this mode existed.
    public var preferenceValue: String? { self == .team ? rawValue : nil }

    /// Whether the app has a Dock icon. Nobody sits at a team-only Mac, so its app is a menu bar item only (the
    /// accessory activation policy). A full install keeps the Dock icon of its chat window.
    public var showsDockIcon: Bool { self == .full }

    /// The login items of the mode. A team-only Mac has no phone, so it has no relay sidecar.
    public var loginItems: [LoginItem] { self == .team ? [.app, .agent(.service)] : LoginItem.allCases }

    /// The listener that answers `/health`. A team-only service has no tunnel listener (server.py), only the main one.
    public var healthURL: URL {
        self == .team ? URL(string: "http://127.0.0.1:8960/health")! : StatusReader.healthURL
    }

    /// Put the variable of the mode into an environment.
    public func apply(to environment: inout [String: String]) {
        environment[Self.environmentKey] = environmentValue
    }
}

/// `Coconut.app --set-up`, with `--team-only` for a Mac with no person. The first-run screens and "Set Up Coconut" in
/// the menu make a full install.
public enum SetUpRequest {
    public static let argument = "--set-up"
    public static let teamOnlyArgument = "--team-only"

    /// The mode that the launch arguments ask for, or nil without `--set-up`.
    public static func mode(arguments: [String]) -> InstallMode? {
        guard arguments.contains(argument) else { return nil }
        return arguments.contains(teamOnlyArgument) ? .team : .full
    }

    /// Whether a set-up of `requested` may run. A mode does not change in place: the service would refuse the data
    /// folder of the other mode, and a full install would keep its entries in Hermes. Without an install, the request
    /// chooses the mode.
    public static func check(requested: InstallMode, recorded: InstallMode, installed: Bool) -> String? {
        guard installed, requested != recorded else { return nil }
        switch recorded {
        case .full:
            return "This Mac has a full Coconut install. Remove Coconut first, then set it up for a team only."
        case .team:
            return "This Mac runs Coconut for a team only. Remove Coconut first, then set it up again."
        }
    }

    /// The arguments of `coconut install-source` that the app runs. Only a full setup that the person asked for adds the
    /// coconut skill to Hermes.
    public static func installSourceArguments(ifNeeded: Bool, setUp: Bool, mode: InstallMode) -> [String] {
        if ifNeeded { return ["install-source", "--if-needed"] }
        return ["install-source"] + (setUp && mode == .full ? ["--set-up"] : [])
    }
}

/// `Coconut.app --remove` on a team-only Mac: the app removes the login items of the mode (no approval needed), runs the
/// uninstaller of its own install folder, moves that folder to the Trash, removes its preference, and quits. The app reads
/// the argument only when it starts. On a full install it changes nothing and starts as always.
public enum RemoveRequest {
    public static let argument = "--remove"
    public static let fullInstall = "Coconut.app --remove removes only a team-only install. To remove this install, "
        + "open Uninstall Coconut.command."

    public static func asked(arguments: [String]) -> Bool { arguments.contains(argument) }

    /// The preferences that Coconut writes for an install, which a removal deletes, so a later set-up starts clean: the
    /// mode, Start at Login, the update state of `coconut status` and `coconut update`, and the automatic update
    /// setting of `coconut settings`. `InstallDirectory` and `ServiceEnvironment` stay: only the developer setup
    /// (scripts/setup_coconut.py) writes them, and they name a checkout.
    public static let preferenceKeys = [
        InstallMode.preferenceKey, "startAtLogin", UpdatePreferences.resultKey, UpdatePreferences.pendingInstallKey,
        UpdatePreferences.requestKey, UpdatePreferences.automaticChecksKey, UpdatePreferences.automaticInstallKey,
        UpdatePreferences.lastCheckKey,
    ]
}

/// The mark of a team-only service in the store of an install (backend/coconut/service_base.py, MODE_MARK). It decides
/// with the preference: if the preference is lost on a team-only Mac, the mark keeps every Hermes step away.
public enum TeamFolderMark {
    /// True when the store of the install has the mark. The store is opened read-only, with no wait for a lock: in WAL
    /// mode a reader does not wait for the writer, and a store that is busy for another reason counts as no mark. A
    /// missing store, or a store without the table, has no mark. Nothing is written and no file is made.
    public static func isTeamOnly(install: Install) -> Bool {
        let path = install.data.appendingPathComponent("coconut.sqlite3").path
        guard FileManager.default.fileExists(atPath: path) else { return false }
        var db: OpaquePointer?
        defer { sqlite3_close(db) }
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return false }
        var statement: OpaquePointer?
        defer { sqlite3_finalize(statement) }
        guard sqlite3_prepare_v2(db, "SELECT 1 FROM service_mode WHERE mode = 'team' LIMIT 1", -1, &statement,
                                 nil) == SQLITE_OK else { return false }
        return sqlite3_step(statement) == SQLITE_ROW
    }
}

/// The commands of the `coconut` tool that are for a phone or for Hermes. On a team-only Mac each one prints one line
/// and exits with 1.
public enum TeamOnlyRefusal {
    public static let pair = "This Mac runs Coconut for a team only, so it pairs no iPhone. Run coconut team to see its teams."
    public static let migrate = "This Mac runs Coconut for a team only, so it has no LaunchAgents of a full install to move."
    public static let setUpSkill = "This Mac runs Coconut for a team only, so Coconut adds nothing to Hermes. Run coconut "
        + "install-source without --set-up."
    public static let sidecar = "This Mac runs Coconut for a team only, so it runs no relay sidecar."

    /// The line for the arguments of the tool (without the name of the tool), or nil when the command runs.
    public static func line(_ arguments: [String], mode: InstallMode) -> String? {
        guard mode == .team else { return nil }
        switch arguments.first {
        case "pair": return pair
        case "migrate": return migrate
        case "install-source" where arguments.contains(SetUpRequest.argument): return setUpSkill
        case "sidecar": return sidecar
        default: return nil
        }
    }
}
