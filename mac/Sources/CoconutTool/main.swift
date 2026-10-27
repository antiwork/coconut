// The `coconut` tool in Coconut.app/Contents/Helpers. The login items run `service` and `sidecar`. A person or a
// Hermes agent runs `pair` and `status`.
import CoconutKit
import Foundation

let usage = """
    Usage: coconut <command>

      status    Print the version, the update settings, the last update check and error, and the state of the
                service, the relay and Hermes. --json prints the service state as JSON.
      update    Check for a newer Coconut.app now, and install it. Prints the result and returns when the install
                starts: the service updates after its running bot work finishes, also the turn that ran this
                command. Run coconut status later to see the end. --wait waits for the end (not inside a bot turn).
      settings automatic-updates on|off
                Turn automatic updates on or off. When on, Coconut.app checks every six hours and installs.
      settings channel stable|nightly
                Choose the update channel.
      pair      Print a one-time iPhone setup link.
      pair <code>
                Pair the iPhone that shows this code. Hermes runs this from the iPhone's message.
      version   Print the version of Coconut.app.
      install-source
                Install the Coconut source that this app carries. --if-needed skips an install that is current.
                The app runs this after each update.
      migrate   Stop the service of the earlier LaunchAgents and move them aside, so the app's login items run it.
      service   Run the Coconut service. The login item uses this command.
      sidecar   Run the relay sidecar. The login item uses this command.
    """

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

/// The folder of this executable: Contents/Helpers in the bundle.
let helpers = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0]).resolvingSymlinksInPath()
    .deletingLastPathComponent()
let install = Install.standard()

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
    print("Checking for updates on the \(preferences.channel.rawValue) channel.")
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
        healthy = await StatusReader.checkHealth()
        semaphore.signal()
    }
    semaphore.wait()
    return StatusReader(install: install).read(healthy: healthy)
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
          let ticket = (fields["secret"] as? String).flatMap({ pairingTicketFile(install: install, secret: $0) }) else { exit(1) }
    let known = pairedPhoneKeys(install: install)
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
    let expired = "The iPhone did not pair before the code expired. Make a new code on the iPhone, then try again."
    // The sidecar removes this setup's ticket when the iPhone connects with it. Another iPhone that pairs at the same
    // time uses another ticket, so it cannot end this wait.
    while FileManager.default.fileExists(atPath: ticket.path) {
        guard Date().timeIntervalSince1970 < expires else { fail(expired) }
        sleep(1)
    }
    // The sidecar also removes an expired ticket. Then no iPhone connected.
    if Date().timeIntervalSince1970 >= expires && pairedPhoneKeys(install: install).subtracting(known).isEmpty { fail(expired) }
    // Then the iPhone sends its pairing request, and the sidecar records its key before it answers. That often happens
    // within a second of the connection, so the keys from before the send are the reference.
    for _ in 0..<60 {
        if !pairedPhoneKeys(install: install).subtracting(known).isEmpty {
            print("The iPhone is paired with this Mac.")
            exit(0)
        }
        sleep(1)
    }
    fail("The iPhone connected but did not finish pairing. Make a new code on the iPhone, then try again.")
}

let command = CommandLine.arguments.dropFirst().first ?? ""
switch command {
case "service":
    // An update replaces the source now. launchd started this tool again after the update stopped the service.
    while install.isHeld() {
        sleep(2)
    }
    guard install.isInstalled() else { waitAndExit("Coconut is not installed in \(install.root.path).") }
    let environment = LoginEnvironment.make(base: ProcessInfo.processInfo.environment,
                                            loginPath: LoginEnvironment.loginPath())
    guard let python = PythonLocator(environment: environment).locate() else {
        waitAndExit("Coconut found no Python 3.11 or later. Install Hermes, or Python 3.11 or later from Homebrew.")
    }
    run(.service(install: install, python: python, environment: environment))
case "sidecar":
    guard install.hasRelayRoute() else { waitAndExit("This Mac has no relay route yet. Pair an iPhone first.") }
    run(.sidecar(install: install, binary: helpers.appendingPathComponent("tunnel-sidecar"),
                 environment: ProcessInfo.processInfo.environment))
case "pair":
    guard install.isInstalled() else { fail("Coconut is not installed in \(install.root.path).") }
    let environment = LoginEnvironment.make(base: ProcessInfo.processInfo.environment, loginPath: nil)
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
    let environment = LoginEnvironment.make(base: ProcessInfo.processInfo.environment,
                                            loginPath: LoginEnvironment.loginPath())
    guard let python = PythonLocator(environment: environment).locate() else {
        fail("Coconut found no Python 3.11 or later. Install Hermes, or Python 3.11 or later from Homebrew.")
    }
    let launch = Launch.update(install: install, source: source, directory: directory, python: python,
                               sidecar: helpers.appendingPathComponent("tunnel-sidecar"), environment: environment)
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
case "settings":
    let arguments = Array(CommandLine.arguments.dropFirst(2))
    let preferences = UpdatePreferences()
    switch (arguments.first, arguments.dropFirst().first) {
    case ("automatic-updates", "on"?), ("automatic-updates", "off"?):
        preferences.setAutomaticUpdates(arguments[1] == "on")
    case ("channel", let value?):
        guard let channel = UpdateChannel(rawValue: value) else { fail(usage, code: 64) }
        preferences.setChannel(channel)
    case (nil, nil):
        break
    default:
        fail(usage, code: 64)
    }
    DistributedNotificationCenter.default().postNotificationName(.init(UpdatePreferences.settingsChanged), object: nil,
                                                                 userInfo: nil, deliverImmediately: true)
    print("Automatic updates: \(preferences.automaticUpdates ? "on" : "off")")
    print("Update channel: \(preferences.channel.rawValue)")
case "version":
    print(appVersion())
case "", "-h", "--help", "help":
    print(usage)
default:
    fail(usage, code: 64)
}
