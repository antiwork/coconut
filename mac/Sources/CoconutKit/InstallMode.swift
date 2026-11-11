import Foundation

/// The mode of the install of Coconut.app on this Mac.
///
/// A full install serves the iPhone and the Mac client, runs the bots of the person's Hermes and adds Coconut's entries
/// to the Hermes config. A team-only install is for a Mac with no person at it (docs/install-and-update.md, "A Mac without
/// a person"): its service keeps its teams in step and saves the pages of the bots that the Mac declares
/// (backend/coconut/team_only.py). It touches nothing of Hermes, registers no relay sidecar and pairs no phone.
///
/// The install keeps its mode in the preferences of the app, next to `InstallDirectory`, so the app, the tool, the login
/// item and each update read one value. Only `Coconut.app --set-up --team-only` writes "team". The store of the service
/// has its own mark (service_base.MODE_MARK): when the two disagree, the service refuses its data folder with the code
/// `data_folder_mode`, and `coconut status` and the menu show that cause (StartError).
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

/// `Coconut.app --remove`: remove a team-only install without a click. The app removes its login items, which needs no
/// approval, then `coconut remove` removes the data and moves the install folder to the Trash.
public enum RemoveRequest {
    public static let argument = "--remove"
    public static let fullInstall = "Coconut.app --remove removes a team-only install. On a full install, open "
        + "Uninstall Coconut.command."
    public static let toolFullInstall = "coconut remove removes a team-only install. On a full install, open "
        + "Uninstall Coconut.command."

    public static func asked(arguments: [String]) -> Bool { arguments.contains(argument) }
}

/// The commands of the `coconut` tool that are for a phone or for Hermes. On a team-only Mac each one prints one line
/// and exits with 1.
public enum TeamOnlyRefusal {
    public static let pair = "This Mac runs Coconut for a team only, so it pairs no iPhone. Run coconut team to see its teams."
    public static let migrate = "This Mac runs Coconut for a team only, so it has no LaunchAgents of a full install to move."
    public static let setUpSkill = "This Mac runs Coconut for a team only, so Coconut adds nothing to Hermes. Run coconut "
        + "install-source without --set-up."
    /// The relay login item waits with this line, as it waits without a relay route.
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
