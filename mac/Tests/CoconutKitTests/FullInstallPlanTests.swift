@testable import CoconutKit
import ServiceManagement
import XCTest

/// A full install sets up, starts and updates as it did before the team-only mode. `FullInstallPlanTests.fixture` holds
/// what origin/main gave for each step before that change. The plan reads only the calls that origin/main had, with
/// their default arguments, so a new argument must keep its full-install default.
final class FullInstallPlanTests: XCTestCase {
    static let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
    static let python = URL(fileURLWithPath: "/opt/homebrew/bin/python3")
    static let helpers = URL(fileURLWithPath: "/App/Contents/Helpers", isDirectory: true)

    static func launch(_ launch: Launch) -> [String: Any] {
        ["arguments": launch.arguments, "environment": launch.environment, "directory": launch.directory.path,
         "output": launch.standardOutput?.path ?? "", "errors": launch.standardError?.path ?? ""]
    }

    static func plan(resources: URL) throws -> [String: Any] {
        let source = try XCTUnwrap(BundledSource(resources: resources))
        let home = URL(fileURLWithPath: "/Users/a")
        let update = { (setUp: Bool) in
            Launch.update(install: install, source: source, directory: "Coconut", python: python,
                          sidecar: helpers.appendingPathComponent("tunnel-sidecar"),
                          teamHelper: helpers.appendingPathComponent("team-helper"), environment: ["PATH": "/bin"],
                          home: home, setUp: setUp)
        }
        let register = FakeLoginItems([.app: .notRegistered, .agent(.service): .notRegistered,
                                       .agent(.relay): .notRegistered])
        _ = register.items.register()
        let unregister = FakeLoginItems([.app: .enabled, .agent(.service): .enabled, .agent(.relay): .enabled])
        _ = unregister.items.unregister()
        let preferences = UpdatePreferences(store: MemoryPreferences())
        let status = Status(install: install.root.path, service: .running, hermes: "ok", hermesVersion: "0.9.0",
                            hermesReason: nil, relay: "connected")
        return [
            "login items": LoginItem.allCases.map(\.name),
            "register": register.calls,
            "unregister": unregister.calls,
            "service": launch(.service(install: install, python: python, environment: ["PATH": "/bin"],
                                       teamHelper: helpers.appendingPathComponent("team-helper"))),
            "sidecar": launch(.sidecar(install: install, binary: helpers.appendingPathComponent("tunnel-sidecar"),
                                       environment: ["PATH": "/bin"])),
            "update": launch(update(false)),
            "set up": launch(update(true)),
            "health": StatusReader.healthURL.absoluteString,
            "python": PythonLocator(environment: ["COCONUT_HERMES_PYTHON": "/h/python", "HERMES_HOME": "/h"],
                                    home: home).candidates().map(\.path),
            "environment": LoginEnvironment.make(base: ["PATH": "/bin", "HOME": "/Users/a"], loginPath: "/opt/bin",
                                                 saved: ["HERMES_HOME": "/h"]),
            "status": StatusText.lines(version: "1.0 (1)", preferences: preferences, status: status),
        ]
    }

    static func text(_ plan: [String: Any]) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: plan, options: [.prettyPrinted, .sortedKeys,
                                                                               .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self)
    }

    func testAFullInstallKeepsEachStepOfTheFixture() throws {
        let resources = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: resources) }
        try Data(#"{"package_id": "abc"}"#.utf8).write(to: resources.appendingPathComponent("package.json"))
        let plan = try Self.text(Self.plan(resources: resources)).replacingOccurrences(of: resources.path, with: "<resources>")
        XCTAssertEqual(plan, Self.fixture)
    }

    /// The new arguments, given as `.full`, change nothing of the fixture.
    func testTheFullModeGivesTheStepsOfTheFixture() throws {
        let resources = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: resources) }
        try Data(#"{"package_id": "abc"}"#.utf8).write(to: resources.appendingPathComponent("package.json"))
        let source = try XCTUnwrap(BundledSource(resources: resources))
        let plan = try Self.plan(resources: resources)
        let service = Launch.service(install: Self.install, python: Self.python, environment: ["PATH": "/bin"],
                                     teamHelper: Self.helpers.appendingPathComponent("team-helper"), mode: .full)
        XCTAssertEqual(Self.launch(service) as NSDictionary, plan["service"] as? NSDictionary)
        for setUp in [false, true] {
            let update = Launch.update(install: Self.install, source: source, directory: "Coconut", python: Self.python,
                                       sidecar: Self.helpers.appendingPathComponent("tunnel-sidecar"),
                                       teamHelper: Self.helpers.appendingPathComponent("team-helper"),
                                       environment: ["PATH": "/bin"], home: URL(fileURLWithPath: "/Users/a"),
                                       setUp: setUp, mode: .full)
            XCTAssertEqual(Self.launch(update) as NSDictionary, plan[setUp ? "set up" : "update"] as? NSDictionary)
        }
        let register = FakeLoginItems([.app: .notRegistered, .agent(.service): .notRegistered,
                                       .agent(.relay): .notRegistered])
        _ = register.items.register(for: .full)
        XCTAssertEqual(register.calls, plan["register"] as? [String])
        XCTAssertEqual(PythonLocator(environment: ["COCONUT_HERMES_PYTHON": "/h/python", "HERMES_HOME": "/h"],
                                     home: URL(fileURLWithPath: "/Users/a"), mode: .full).candidates().map(\.path),
                       plan["python"] as? [String])
        XCTAssertEqual(InstallMode.full.healthURL.absoluteString, plan["health"] as? String)
        // A full install keeps its Dock icon (Info.plist has LSUIElement false). Only a team-only Mac hides it.
        XCTAssertTrue(InstallMode.full.showsDockIcon)
        XCTAssertFalse(InstallMode.team.showsDockIcon)
    }

    static let fixture = """
        {
          "environment" : {
            "HERMES_HOME" : "/h",
            "HOME" : "/Users/a",
            "PATH" : "/opt/bin"
          },
          "health" : "http://127.0.0.1:8961/health",
          "login items" : [
            "Coconut.app",
            "com.gumroad.coconut.service",
            "com.gumroad.coconut.relay"
          ],
          "python" : [
            "/h/python",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/Users/a/.hermes/hermes-agent/venv/bin/python",
            "/Users/a/.hermes/hermes-agent/.venv/bin/python"
          ],
          "register" : [
            "register Coconut.app",
            "register com.gumroad.coconut.service",
            "register com.gumroad.coconut.relay"
          ],
          "service" : {
            "arguments" : [
              "/opt/homebrew/bin/python3",
              "/Users/a/Coconut/scripts/host.py",
              "serve",
              "--data-dir",
              "/Users/a/Coconut/.runtime/host",
              "--provider",
              "hermes"
            ],
            "directory" : "/Users/a/Coconut/backend",
            "environment" : {
              "COCONUT_LOGIN_ITEM" : "com.gumroad.coconut.service",
              "COCONUT_TEAM_HELPER" : "/App/Contents/Helpers/team-helper",
              "PATH" : "/bin",
              "PYTHONUNBUFFERED" : "1"
            },
            "errors" : "/Users/a/Coconut/.runtime/host/logs/error.log",
            "output" : "/Users/a/Coconut/.runtime/host/logs/service.log"
          },
          "set up" : {
            "arguments" : [
              "/opt/homebrew/bin/python3",
              "<resources>/deploy.py",
              "receive",
              "--directory",
              "Coconut"
            ],
            "directory" : "/Users/a",
            "environment" : {
              "COCONUT_APP_MANAGED" : "1",
              "COCONUT_SET_UP" : "1",
              "COCONUT_SIDECAR" : "/App/Contents/Helpers/tunnel-sidecar",
              "COCONUT_TEAM_HELPER" : "/App/Contents/Helpers/team-helper",
              "PATH" : "/bin",
              "PYTHONUNBUFFERED" : "1"
            },
            "errors" : "",
            "output" : ""
          },
          "sidecar" : {
            "arguments" : [
              "/App/Contents/Helpers/tunnel-sidecar",
              "run",
              "--data-dir",
              "/Users/a/Coconut/.runtime/host",
              "--backend",
              "127.0.0.1:8961"
            ],
            "directory" : "/Users/a/Coconut/.runtime/host",
            "environment" : {
              "PATH" : "/bin"
            },
            "errors" : "/Users/a/Coconut/.runtime/host/logs/tunnel-error.log",
            "output" : "/Users/a/Coconut/.runtime/host/logs/tunnel.log"
          },
          "status" : [
            "Coconut.app: 1.0 (1)",
            "Automatic updates: on",
            "Last update check: never",
            "Last update error: none",
            "Service: running (/Users/a/Coconut)",
            "Relay: connected",
            "Hermes: 0.9.0"
          ],
          "unregister" : [
            "unregister Coconut.app",
            "unregister com.gumroad.coconut.service",
            "unregister com.gumroad.coconut.relay"
          ],
          "update" : {
            "arguments" : [
              "/opt/homebrew/bin/python3",
              "<resources>/deploy.py",
              "receive",
              "--directory",
              "Coconut"
            ],
            "directory" : "/Users/a",
            "environment" : {
              "COCONUT_APP_MANAGED" : "1",
              "COCONUT_SIDECAR" : "/App/Contents/Helpers/tunnel-sidecar",
              "COCONUT_TEAM_HELPER" : "/App/Contents/Helpers/team-helper",
              "PATH" : "/bin",
              "PYTHONUNBUFFERED" : "1"
            },
            "errors" : "",
            "output" : ""
          }
        }
        """
}
