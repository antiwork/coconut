// The `coconut` tool in Coconut.app/Contents/Helpers. The login items run `service` and `sidecar`. A person or a
// Hermes agent runs `pair` and `status`.
import CoconutKit
import Foundation

let usage = """
    Usage: coconut <command>

      pair      Print a one-time iPhone setup link and the path of its QR image.
      status    Print the state of the service, Hermes and the relay as JSON.
      version   Print the version of Coconut.app.
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
    do {
        try FileManager.default.createDirectory(at: launch.standardOutput.deletingLastPathComponent(),
                                                withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    } catch {
        fail("Coconut could not create its log folder: \(error.localizedDescription)")
    }
    for (path, descriptor) in [(launch.standardOutput.path, STDOUT_FILENO), (launch.standardError.path, STDERR_FILENO)] {
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

let command = CommandLine.arguments.dropFirst().first ?? ""
switch command {
case "service":
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
    let process = Process()
    process.executableURL = python
    process.arguments = [install.pairingScript.path, "--share"]
    process.currentDirectoryURL = install.root
    do {
        try process.run()
    } catch {
        fail("Coconut could not run \(install.pairingScript.path): \(error.localizedDescription)")
    }
    process.waitUntilExit()
    exit(process.terminationStatus)
case "status":
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    print(String(decoding: try encoder.encode(readStatus()), as: UTF8.self))
case "version":
    // The tool lives in Contents/Helpers. Its app's Info.plist is in Contents.
    let info = NSDictionary(contentsOf: helpers.deletingLastPathComponent().appendingPathComponent("Info.plist"))
    print("\(info?["CFBundleShortVersionString"] as? String ?? "unknown") (\(info?["CFBundleVersion"] as? String ?? "unknown"))")
case "", "-h", "--help", "help":
    print(usage)
default:
    fail(usage, code: 64)
}
