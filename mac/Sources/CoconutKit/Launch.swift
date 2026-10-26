import Foundation

/// A program that the `coconut` tool starts in place of itself with `execve`.
public struct Launch: Equatable {
    public var executable: URL
    public var arguments: [String]
    public var environment: [String: String]
    public var directory: URL
    public var standardOutput: URL
    public var standardError: URL

    /// The service: `host.py serve`. host.py finds the person's Hermes and starts again under its Python.
    public static func service(install: Install, python: URL, environment: [String: String]) -> Launch {
        var environment = environment
        environment["PYTHONUNBUFFERED"] = "1"
        return Launch(executable: python,
                      arguments: [python.path, install.hostScript.path, "serve", "--data-dir", install.data.path,
                                  "--provider", "hermes"],
                      environment: environment, directory: install.backend,
                      standardOutput: install.logs.appendingPathComponent("service.log"),
                      standardError: install.logs.appendingPathComponent("error.log"))
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

    public static func make(base: [String: String], loginPath: String?) -> [String: String] {
        var environment = base
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
