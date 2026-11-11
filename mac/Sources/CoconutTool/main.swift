// The `coconut` tool in Coconut.app/Contents/Helpers. The login items run `service` and `sidecar`. A person or a
// Hermes agent runs `pair`, `status` and `team`. On a team-only Mac (InstallMode), each command for a phone or for Hermes
// prints one line and exits with 1 (TeamOnlyRefusal).
import AppKit
import CoconutKit
import Foundation

let usage = """
    Usage: coconut <command>

      status    Print the version, the update settings, the last update check and error, and the state of the
                service, the relay and Hermes. On a Mac that runs Coconut for a team only: the mode, the service and
                each team. --json prints the service state as JSON.
      update    Check for a newer Coconut.app now, and install it. Prints the result and returns when the install
                starts: the service updates after its running bot work finishes, also the turn that ran this
                command. Run coconut status later to see the end. --wait waits for the end (not inside a bot turn).
      settings automatic-updates on|off
                Turn automatic updates on or off. When on, Coconut.app checks every six hours and installs.
      pair      Print a one-time iPhone setup link. Not on a Mac that runs Coconut for a team only.
      pair <code>
                Pair the iPhone that shows this code. Hermes runs this from the iPhone's message.
      team      Print the teams of this Mac: the name, the state, the admin, the members, the bots and the sync
                error. --json prints the answer of the service as JSON.
      team join [--name <text>]
                Join a team with the invite link on standard input. The link is a secret: pipe it to the command.
                --name is the name of this Mac in the member list.
      team new-bot <name> [--home <path>]
                On a Mac that runs Coconut for a team only: declare a bot for the Hermes home that it stands for.
                Without --home, the service takes its own Hermes home.
      team add-bot <bot> [--team <name or ID>]
                Add a bot of this Mac to the team. <bot> is the ID or the name of the bot. --team is necessary
                when this Mac is in more than one team.
      team remove-bot <bot> [--team <name or ID>]
                Remove a bot of this Mac from the team.
      team leave --confirm <name of the team> [--team <name or ID>]
                Leave the team, and delete its copy and its keys on this Mac. Without --confirm, print what a
                leave does.
      version   Print the version of Coconut.app.
      install-source
                Install the Coconut source that this app carries. --if-needed skips an install that is current.
                The app runs this after each update. --set-up also adds the coconut skill to Hermes (full install
                only).
      migrate   Stop the service of the earlier LaunchAgents and move them aside, so the app's login items run it.
      service   Run the Coconut service. The login item uses this command.
      sidecar   Run the relay sidecar. The login item uses this command.
      remove [--confirm]
                On a Mac that runs Coconut for a team only: remove Coconut. This cannot be undone. Without --confirm,
                print what the removal does and change nothing. With --confirm, stop Coconut.app, start it with
                --remove, and wait for the result. The app removes its login items, the data of Coconut, and the key
                of this Mac in each team, and moves the install folder to the Trash. To come back, this Mac needs a
                new invite link.

    Coconut.app --set-up sets up Coconut without a click, as Set Up Coconut in the menu does. The install script
    opens the app with it. Coconut.app --set-up --team-only sets up a Mac with no person for a team only: Coconut keeps
    its teams and the pages of its bots, and touches nothing of Hermes. See docs/install-and-update.md.
    """

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

/// The folder of this executable: Contents/Helpers in the bundle.
let helpers = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0]).resolvingSymlinksInPath()
    .deletingLastPathComponent()
let install = Install.standard()
let mode = InstallMode.standard()

/// Wait, then exit. launchd starts a login item again after each exit, also after a clean one: the service exits
/// with 0 when a bot asks for a restart. The wait keeps a login item that cannot start from a restart every few seconds.
func waitAndExit(_ message: String) -> Never {
    print(message)
    fflush(stdout)
    sleep(300)
    exit(0)
}

/// Replace this process with the program. The output goes to the log files of the service, as with the old LaunchAgents.
func run(_ launch: Launch) -> Never {
    for (log, descriptor) in [(launch.standardOutput, STDOUT_FILENO), (launch.standardError, STDERR_FILENO)] {
        guard let log else { continue }
        let path = log.path
        do {
            try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        } catch {
            fail("Coconut could not create its log folder: \(error.localizedDescription)")
        }
        let file = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard file >= 0, dup2(file, descriptor) >= 0 else { fail("Coconut could not open \(path).") }
        close(file)
    }
    guard chdir(launch.directory.path) == 0 else { fail("Coconut could not open \(launch.directory.path).") }
    let arguments = launch.arguments.map { strdup($0) } + [nil]
    let environment = launch.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
    execve(launch.executable.path, arguments, environment)
    fail("Coconut could not start \(launch.executable.path): \(String(cString: strerror(errno)))")
}

/// The version of the app around this tool, read again each time: Sparkle can replace the app while the tool runs.
func appVersion() -> String {
    // The tool lives in Contents/Helpers. Its app's Info.plist is in Contents.
    let info = NSDictionary(contentsOf: helpers.deletingLastPathComponent().appendingPathComponent("Info.plist"))
    return "\(info?["CFBundleShortVersionString"] as? String ?? "unknown") (\(info?["CFBundleVersion"] as? String ?? "unknown"))"
}

/// `coconut update`: ask the app to check now, wait for its result, and wait for the new version to serve.
func checkForUpdates() -> Int32 {
    let preferences = UpdatePreferences()
    let start = Date()
    let before = appVersion()
    preferences.requestCheck(at: start)
    DistributedNotificationCenter.default().postNotificationName(.init(UpdatePreferences.checkRequested), object: nil,
                                                                 userInfo: nil, deliverImmediately: true)
    // A running app gets the notification. A stopped app reads the request when `open` starts it.
    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    // The tool lives in Contents/Helpers of the app, so it opens that app, also one that Launch Services does not know.
    open.arguments = ["-g", helpers.deletingLastPathComponent().deletingLastPathComponent().path]
    open.standardError = FileHandle.nullDevice
    try? open.run()
    open.waitUntilExit()
    print("Checking for updates.")
    guard let result = waitForResult(preferences, after: start, seconds: 300) else {
        print("Coconut.app did not answer within five minutes. Open the Coconut menu, then run coconut update again.")
        return 1
    }
    switch result.outcome {
    case .upToDate:
        print("Coconut.app \(before) is up to date.")
        return 0
    case .failed:
        print("The update check failed: \(result.message ?? "unknown error")")
        return 1
    case .installed:
        print("Coconut.app \(result.version ?? before) is installed.")
        return 0
    case .installing:
        print("Installing Coconut \(result.version ?? "") now. The app restarts. The service updates after its running bot work finishes.")
    }
    // A bot turn that waits here would wait for itself: the service update waits for all running turns. So the command
    // returns now, unless a person asks to wait.
    guard CommandLine.arguments.contains("--wait") else {
        print("Run coconut status later. \"Last update result\" says \"installed\" when the service runs the new version.")
        return 0
    }
    let deadline = Date().addingTimeInterval(1800)
    while Date() < deadline {
        sleep(5)
        guard let latest = preferences.lastResult, latest.time > result.time else { continue }
        switch latest.outcome {
        case .installed:
            print("Coconut.app \(latest.version ?? appVersion()) runs, and the service has its source.")
            return 0
        case .failed:
            print("The update failed: \(latest.message ?? "unknown error")")
            return 1
        case .upToDate, .installing:
            continue
        }
    }
    print("The update did not finish within 30 minutes. Run coconut status to see the last error.")
    return 1
}

func waitForResult(_ preferences: UpdatePreferences, after start: Date, seconds: TimeInterval) -> UpdateResult? {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if let result = preferences.lastResult, result.time >= start {
            return result
        }
        sleep(1)
    }
    return nil
}

func readStatus() -> Status {
    let semaphore = DispatchSemaphore(value: 0)
    var healthy = false
    // Detached: this top-level code runs on the main thread, which waits below.
    Task.detached {
        healthy = await StatusReader.checkHealth(url: mode.healthURL)
        semaphore.signal()
    }
    semaphore.wait()
    return StatusReader(install: install).read(healthy: healthy)
}

/// The processes of the app around this tool. Launch Services lists them without a permission.
func appProcesses(_ app: URL) -> [NSRunningApplication] {
    NSRunningApplication.runningApplications(withBundleIdentifier: Install.preferencesDomain)
        .filter { $0.bundleURL?.standardizedFileURL.path == app.standardizedFileURL.path && !$0.isTerminated }
}

/// `coconut remove` on a team-only Mac: stop the app that runs, start it with `--remove`, and wait for its result.
///
/// The app reads `--remove` only when it starts. The stop is SIGTERM to the process of this bundle, not an Apple event,
/// because an Apple event can ask for the Automation permission in a dialog that nobody sees. No login item starts the
/// app again: the login item of the app opens it only at login, and it has no KeepAlive.
func removeWithApp() -> Int32 {
    let app = helpers.deletingLastPathComponent().deletingLastPathComponent()
    for running in appProcesses(app) {
        kill(running.processIdentifier, SIGTERM)
    }
    let stopDeadline = Date().addingTimeInterval(10)
    while !appProcesses(app).isEmpty && Date() < stopDeadline {
        usleep(200_000)
    }
    for running in appProcesses(app) {
        kill(running.processIdentifier, SIGKILL)
    }
    let start = Date()
    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    open.arguments = RemoveRequest.openArguments(app: app)
    do {
        try open.run()
    } catch {
        print("Coconut could not open \(app.path): \(error.localizedDescription)")
        return 1
    }
    open.waitUntilExit()
    print("Removing Coconut. Coconut.app stops the service, removes its data, and quits.")
    fflush(stdout)
    let preferences = AppPreferences()
    let deadline = start.addingTimeInterval(RemoveRequest.wait)
    while Date() < deadline {
        sleep(1)
        let outcome = RemoveRequest.outcome(installed: install.isInstalled(),
                                            modePreference: preferences.value(forKey: InstallMode.preferenceKey) as? String,
                                            lastResult: UpdatePreferences(store: preferences).lastResult, start: start)
        switch outcome {
        case .waiting: continue
        case .removed:
            print(RemoveRequest.removed)
            return 0
        case .failed(let line):
            print(line)
            return 1
        }
    }
    print(RemoveRequest.timedOut)
    return 1
}

/// The teams of a team-only service (`GET /v1/team/status`).
func readTeamStatus() -> Result<TeamServiceStatus, TeamFailure> {
    let semaphore = DispatchSemaphore(value: 0)
    var result = Result<TeamServiceStatus, TeamFailure>.failure(.noAnswer)
    // Detached: this top-level code runs on the main thread, which waits below.
    Task.detached {
        result = await TeamServiceStatus.read(install: install)
        semaphore.signal()
    }
    semaphore.wait()
    return result
}

/// Send a new setup to the rendezvous box of the iPhone's code, then wait until that iPhone pairs. The lines are plain
/// progress for a Hermes agent that runs the command.
func pairPhone(code: PairingCode, launch: Launch) -> Never {
    let process = Process()
    process.executableURL = launch.executable
    process.arguments = Array(launch.arguments.dropFirst())
    process.environment = launch.environment
    process.currentDirectoryURL = launch.directory
    let output = Pipe()
    process.standardOutput = output
    do {
        try process.run()
    } catch {
        fail("Coconut could not start Python: \(error.localizedDescription)")
    }
    let setup = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0,
          let fields = try? JSONSerialization.jsonObject(with: setup) as? [String: Any],
          let relay = (fields["relay"] as? String).flatMap(URL.init(string:)),
          let expires = fields["expires"] as? Double,
          let secret = fields["secret"] as? String else { exit(1) }
    print("Sending the setup to the iPhone.")
    fflush(stdout)
    let semaphore = DispatchSemaphore(value: 0)
    var failure: Error?
    // Detached: this top-level code runs on the main thread, which waits below.
    Task.detached {
        do { try await Rendezvous.send(relay: relay, code: code, setup: setup) } catch { failure = error }
        semaphore.signal()
    }
    semaphore.wait()
    if let failure { fail("\(failure)") }
    print("Waiting for the iPhone to pair.")
    fflush(stdout)
    // The service marks this setup's secret when its iPhone pairs. The service refuses the secret after the expiry, so a
    // few seconds more cover a pairing request that started just before it.
    while Date().timeIntervalSince1970 < expires + 5 {
        if pairingSecretUsed(install: install, secret: secret) == true {
            print("The iPhone is paired with this Mac.")
            exit(0)
        }
        sleep(1)
    }
    fail("The iPhone did not pair before the code expired. Make a new code on the iPhone, then try again.")
}

/// The invite link from standard input. A terminal does not show it.
@Sendable func readInviteLink() -> String? {
    guard isatty(STDIN_FILENO) == 1 else {
        return String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self)
    }
    var buffer = [CChar](repeating: 0, count: 8192)
    guard readpassphrase("Invite link: ", &buffer, buffer.count, RPP_REQUIRE_TTY) != nil else { return nil }
    defer { memset_s(&buffer, buffer.count, 0, buffer.count) }
    return String(cString: buffer)
}

/// `coconut team`: see TeamTool in CoconutKit.
func team(_ arguments: [String]) -> Never {
    let command: TeamCommand
    do {
        command = try TeamCommand.parse(arguments)
    } catch let failure as TeamFailure {
        fail(failure.lines.joined(separator: "\n"), code: failure.status)
    } catch {
        fail(TeamCommand.usage, code: 64)
    }
    let tool = TeamTool(transport: BridgeTransport(install: install, timeout: command.timeout), readLink: readInviteLink)
    let semaphore = DispatchSemaphore(value: 0)
    var output = TeamOutput(lines: [])
    // Detached: this top-level code runs on the main thread, which waits below.
    Task.detached {
        output = await tool.run(command)
        semaphore.signal()
    }
    semaphore.wait()
    guard output.status == 0 else { fail(output.lines.joined(separator: "\n"), code: output.status) }
    output.lines.forEach { print($0) }
    exit(0)
}

/// The environment of a command that starts Python. A team-only Mac takes no saved Hermes setting.
func commandEnvironment(loginPath: String?) -> [String: String] {
    LoginEnvironment.make(base: ProcessInfo.processInfo.environment, loginPath: loginPath,
                          saved: mode == .team ? [:] : LoginEnvironment.savedSettings())
}

/// What the tool says when it finds no Python. A team-only Mac takes no Python of Hermes.
let noPython = PythonLocator.missingLine(mode: mode)

let command = CommandLine.arguments.dropFirst().first ?? ""
if let line = TeamOnlyRefusal.line(Array(CommandLine.arguments.dropFirst()), mode: mode) {
    // launchd starts the relay login item again after each exit, so it waits as it does without a relay route.
    if command == "sidecar" { waitAndExit(line) }
    fail(line)
}
switch command {
case "service":
    // An update replaces the source now. launchd started this tool again after the update stopped the service.
    while install.isHeld() {
        sleep(2)
    }
    guard install.isInstalled() else { waitAndExit("Coconut is not installed in \(install.root.path).") }
    // A start that a restart cannot heal, such as a data folder of the other mode, waits. The status shows its cause.
    if let wait = StartError(install: install)?.wait() {
        sleep(UInt32(wait.rounded(.up)))
    }
    let environment = commandEnvironment(loginPath: LoginEnvironment.loginPath())
    guard let python = PythonLocator(environment: environment, mode: mode).locate() else {
        // The service never starts, so no service leaves this cause. `coconut status` and the menu show it.
        StartError.write(code: StartError.noPython, error: noPython, install: install)
        waitAndExit(noPython)
    }
    run(.service(install: install, python: python, environment: environment,
                 teamHelper: helpers.appendingPathComponent("team-helper"), mode: mode))
case "sidecar":
    guard install.hasRelayRoute() else { waitAndExit("This Mac has no relay route yet. Pair an iPhone first.") }
    run(.sidecar(install: install, binary: helpers.appendingPathComponent("tunnel-sidecar"),
                 environment: ProcessInfo.processInfo.environment))
case "pair":
    guard install.isInstalled() else { fail("Coconut is not installed in \(install.root.path).") }
    let environment = commandEnvironment(loginPath: nil)
    guard let python = PythonLocator(environment: environment).locate() else {
        fail("Coconut found no Python 3.11 or later.")
    }
    guard CommandLine.arguments.count > 2 else { run(.pairingLink(install: install, python: python, environment: environment)) }
    guard let code = PairingCode(CommandLine.arguments[2]) else {
        fail("This is not a Coconut pairing code. Copy the message from the iPhone again.")
    }
    pairPhone(code: code, launch: .pairingSetup(install: install, python: python, environment: environment))
case "install-source":
    let resources = helpers.deletingLastPathComponent().appendingPathComponent("Resources")
    guard let source = BundledSource(resources: resources) else { fail("This app has no Coconut source.") }
    guard let directory = install.relativePath() else {
        fail("The install folder \(install.root.path) must be inside your home folder.")
    }
    if install.isCheckout() {
        print("\(install.root.path) is a Git checkout. Update its source with git pull.")
        exit(CommandLine.arguments.contains("--if-needed") ? 0 : 1)
    }
    if CommandLine.arguments.contains("--if-needed") && !source.isNewer(than: install) {
        print("Coconut is up to date.")
        exit(0)
    }
    let environment = commandEnvironment(loginPath: LoginEnvironment.loginPath())
    guard let python = PythonLocator(environment: environment, mode: mode).locate() else { fail(noPython) }
    let launch = Launch.update(install: install, source: source, directory: directory, python: python,
                               sidecar: helpers.appendingPathComponent("tunnel-sidecar"),
                               teamHelper: helpers.appendingPathComponent("team-helper"), environment: environment,
                               setUp: CommandLine.arguments.contains("--set-up"), mode: mode)
    let process = Process()
    process.executableURL = launch.executable
    process.arguments = Array(launch.arguments.dropFirst())
    process.environment = launch.environment
    process.currentDirectoryURL = launch.directory
    do {
        process.standardInput = try FileHandle(forReadingFrom: source.package)
        try process.run()
    } catch {
        fail("Coconut could not start its update: \(error.localizedDescription)")
    }
    process.waitUntilExit()
    exit(process.terminationStatus)
case "migrate":
    guard install.isInstalled() else { fail("Coconut is not installed in \(install.root.path).") }
    let environment = LoginEnvironment.make(base: ProcessInfo.processInfo.environment, loginPath: nil)
    guard let python = PythonLocator(environment: environment).locate() else {
        fail("Coconut found no Python 3.11 or later.")
    }
    run(.migrate(install: install, python: python, environment: environment))
case "status" where mode == .team:
    let status = readStatus()
    // A stopped service has no listener to ask.
    let team = status.service == .stopped || status.service == .notInstalled ? .failure(.noAnswer) : readTeamStatus()
    if CommandLine.arguments.contains("--json") {
        print(try TeamOnlyReport(status: status, team: team).json())
    } else {
        StatusText.teamLines(version: appVersion(), preferences: UpdatePreferences(), status: status, team: team)
            .forEach { print($0) }
    }
case "status":
    if CommandLine.arguments.contains("--json") {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        print(String(decoding: try encoder.encode(readStatus()), as: UTF8.self))
    } else {
        StatusText.lines(version: appVersion(), preferences: UpdatePreferences(), status: readStatus()).forEach { print($0) }
    }
case "update":
    exit(checkForUpdates())
case "team":
    team(Array(CommandLine.arguments.dropFirst(2)))
case "settings":
    let arguments = Array(CommandLine.arguments.dropFirst(2))
    let preferences = UpdatePreferences()
    switch (arguments.first, arguments.dropFirst().first) {
    case ("automatic-updates", "on"?), ("automatic-updates", "off"?):
        preferences.setAutomaticUpdates(arguments[1] == "on")
    case (nil, nil):
        break
    default:
        fail(usage, code: 64)
    }
    DistributedNotificationCenter.default().postNotificationName(.init(UpdatePreferences.settingsChanged), object: nil,
                                                                 userInfo: nil, deliverImmediately: true)
    print("Automatic updates: \(preferences.automaticUpdates ? "on" : "off")")
case "remove" where !CommandLine.arguments.contains(RemoveRequest.fromApp):
    guard mode == .team else { fail(RemoveRequest.toolFullInstall) }
    guard install.isInstalled() else { fail("Coconut is not installed in \(install.root.path).") }
    guard CommandLine.arguments.contains(RemoveRequest.confirm) else {
        // Nothing changes without the flag. The bot shows these lines to the person and asks for a clear yes.
        let teams = try? readTeamStatus().get().rooms.map(\.name)
        fail(RemoveRequest.preview(teams: teams).joined(separator: "\n"))
    }
    exit(removeWithApp())
case "remove":
    guard mode == .team else { fail(RemoveRequest.toolFullInstall) }
    guard install.isInstalled() else { fail("Coconut is not installed in \(install.root.path).") }
    guard !install.isCheckout() else {
        fail("\(install.root.path) is a source checkout. coconut remove removes only a folder that Coconut.app installed.")
    }
    guard let python = PythonLocator(environment: commandEnvironment(loginPath: nil), mode: mode).locate() else {
        fail(noPython)
    }
    // desktop.py stops while the login item of the service runs, removes the data, and edits nothing in Hermes.
    let uninstall = Process()
    uninstall.executableURL = python
    uninstall.arguments = [install.root.appendingPathComponent("scripts/desktop.py").path, "uninstall", "--yes"]
    uninstall.currentDirectoryURL = install.root
    uninstall.standardInput = FileHandle.nullDevice
    do {
        try uninstall.run()
    } catch {
        fail("Coconut could not start Python: \(error.localizedDescription)")
    }
    uninstall.waitUntilExit()
    guard uninstall.terminationStatus == 0 else { exit(uninstall.terminationStatus) }
    do {
        try FileManager.default.trashItem(at: install.root, resultingItemURL: nil)
    } catch {
        fail("Coconut removed its data, but it could not move \(install.root.path) to the Trash: "
             + error.localizedDescription)
    }
    print("Coconut is removed. \(install.root.path) is in the Trash.")
case "version":
    print(appVersion())
case "", "-h", "--help", "help":
    print(usage)
default:
    fail(usage, code: 64)
}
