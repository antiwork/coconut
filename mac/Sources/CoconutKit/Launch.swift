import Foundation

/// A program that the `coconut` tool starts in place of itself with `execve`.
public struct Launch: Equatable {
    public var executable: URL
    public var arguments: [String]
    public var environment: [String: String]
    public var directory: URL
    /// The log files. Without them, the program keeps the output of the tool.
    public var standardOutput: URL?
    public var standardError: URL?

    /// The service: `host.py serve`. host.py finds the person's Hermes and starts again under its Python.
    /// `teamHelper` is the helper of the team log in this bundle, which the service runs (team_helper.py).
    /// In team-only mode, `COCONUT_MODE=team` makes the service a team-only service (server.py), which finds no Hermes.
    public static func service(install: Install, python: URL, environment: [String: String],
                               teamHelper: URL? = nil, mode: InstallMode = .full) -> Launch {
        var environment = environment
        mode.apply(to: &environment)
        environment["PYTHONUNBUFFERED"] = "1"
        if let teamHelper {
            environment["COCONUT_TEAM_HELPER"] = teamHelper.path
        }
        // host.py asks launchd whether this login item runs this process. Then a bot can ask for a restart: the service
        // exits, and launchd starts it again.
        environment["COCONUT_LOGIN_ITEM"] = Agent.service.rawValue
        return Launch(executable: python,
                      arguments: [python.path, install.hostScript.path, "serve", "--data-dir", install.data.path,
                                  "--provider", "hermes"],
                      environment: environment, directory: install.backend,
                      standardOutput: install.logs.appendingPathComponent("service.log"),
                      standardError: install.logs.appendingPathComponent("error.log"))
    }

    /// Print a new one-time setup link with scripts/pairing.py, in the "Setup link: …" line of `pairing.py --share`.
    /// `pairing.py --share` also draws a QR image with `swift`, which needs the Command Line Tools. The app draws the QR
    /// code itself, so this command makes the link only.
    public static func pairingLink(install: Install, python: URL, environment: [String: String]) -> Launch {
        let script = """
            import subprocess, sys
            sys.path.insert(0, sys.argv[1])
            import pairing
            try:
                payload = pairing.local_payload()
                link = pairing.link(payload)
            except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
                sys.exit('Coconut could not create a setup link. Check that the service runs, then try again.')
            print('Setup link: ' + link)
            print('The setup code works once and expires after ten minutes. Send it only to the person who pairs.')
            """
        return Launch(executable: python,
                      arguments: [python.path, "-c", script, install.root.appendingPathComponent("scripts").path],
                      environment: environment, directory: install.root, standardOutput: nil, standardError: nil)
    }

    /// Print a new one-time version 2 setup as JSON, for `coconut pair <code>`. The tool seals it for the rendezvous box
    /// of the code, on the relay of the setup.
    public static func pairingSetup(install: Install, python: URL, environment: [String: String]) -> Launch {
        let script = """
            import json, subprocess, sys
            sys.path.insert(0, sys.argv[1])
            import pairing
            try:
                payload = pairing.local_payload()
            except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
                sys.exit('Coconut could not create a setup. Check that the service runs, then try again.')
            print(json.dumps(payload))
            """
        return Launch(executable: python,
                      arguments: [python.path, "-c", script, install.root.appendingPathComponent("scripts").path],
                      environment: environment, directory: install.root, standardOutput: nil, standardError: nil)
    }

    /// `coconut team connect-chat` and `disconnect-chat`: scripts/team_chat.py, which changes the Hermes home of a bot
    /// with the Python of Hermes in a process of its own. The output goes to the terminal.
    public static func teamChat(install: Install, python: URL, environment: [String: String],
                                arguments: [String]) -> Launch {
        Launch(executable: python,
               arguments: [python.path, install.root.appendingPathComponent("scripts/team_chat.py").path] + arguments,
               environment: environment, directory: install.root, standardOutput: nil, standardError: nil)
    }

    /// The relay sidecar of the bundle. It forwards the iPhone's requests from the relay to the service's tunnel
    /// listener, as the LaunchAgent of scripts/tunnel.py does.
    public static func sidecar(install: Install, binary: URL, environment: [String: String]) -> Launch {
        Launch(executable: binary,
               arguments: [binary.path, "run", "--data-dir", install.data.path, "--backend", "127.0.0.1:8961"],
               environment: environment, directory: install.data,
               standardOutput: install.logs.appendingPathComponent("tunnel.log"),
               standardError: install.logs.appendingPathComponent("tunnel-error.log"))
    }
}

/// The environment of a login item. launchd gives an agent only a short PATH, but the bots need the tools of the
/// person's shell, for example `gh` and `git` from Homebrew. The old LaunchAgent copied the PATH of the shell that
/// installed it. The tool reads it from the login shell at each start instead, so a new tool needs no new install.
public enum LoginEnvironment {
    public static let fallbackPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    static let mark = "COCONUT-PATH-7f3c"

    /// The Hermes settings that scripts/setup_coconut.py saved in the app's preferences (`ServiceEnvironment`). They
    /// choose another Hermes, as in a shell. See hermes_runtime.candidates.
    public static let hermesSettings = ["COCONUT_HERMES_PYTHON", "COCONUT_HERMES_ROOT", "HERMES_HOME"]

    public static func savedSettings(
        _ value: Any? = CFPreferencesCopyAppValue("ServiceEnvironment" as CFString, Install.preferencesDomain as CFString)
    ) -> [String: String] {
        guard let saved = value as? [String: Any] else { return [:] }
        return saved.reduce(into: [:]) { result, entry in
            if hermesSettings.contains(entry.key), let text = entry.value as? String, !text.isEmpty {
                result[entry.key] = text
            }
        }
    }

    /// The environment of a command: the base, the PATH of the login shell, and the saved Hermes settings.
    public static func make(base: [String: String], loginPath: String?,
                            saved: [String: String] = savedSettings()) -> [String: String] {
        var environment = base.merging(saved) { _, saved in saved }
        if let loginPath, !loginPath.isEmpty {
            environment["PATH"] = loginPath
        } else if (environment["PATH"] ?? "").isEmpty || environment["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin" {
            environment["PATH"] = fallbackPath
        }
        return environment
    }

    /// Read the PATH that the login shell sets, for example in ~/.zprofile. Return nil after ten seconds or on failure.
    public static func loginPath(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        let shell = environment["SHELL"].flatMap { $0.isEmpty ? nil : $0 } ?? "/bin/zsh"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-l", "-c", "printf '\\n\(mark)%s\(mark)\\n' \"$PATH\""]
        process.environment = environment.filter { ["HOME", "USER", "LOGNAME", "SHELL"].contains($0.key) }
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do {
            try process.run()
        } catch {
            return nil
        }
        if finished.wait(timeout: .now() + .seconds(10)) == .timedOut {
            process.terminate()
            return nil
        }
        let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        return parse(text)
    }

    static func parse(_ text: String) -> String? {
        let parts = text.components(separatedBy: mark)
        guard parts.count >= 3 else { return nil }
        let value = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }
}
