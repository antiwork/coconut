@testable import CoconutKit
import ServiceManagement
import XCTest

/// The two modes of an install (InstallMode), what each mode runs, and the cause of a service that does not start.
final class InstallModeTests: XCTestCase {
    var home: URL!

    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            .resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
    let python = URL(fileURLWithPath: "/opt/homebrew/bin/python3")

    // MARK: The mode

    func testTheModeIsTeamOnlyOnlyWithThePreference() {
        XCTAssertEqual(InstallMode.standard(environment: [:], preference: nil), .full)
        XCTAssertEqual(InstallMode.standard(environment: [:], preference: "team"), .team)
        XCTAssertEqual(InstallMode.standard(environment: [:], preference: "other"), .full)
        // A shell variable alone does not change the mode of the install of the app.
        XCTAssertEqual(InstallMode.standard(environment: ["COCONUT_MODE": "team"], preference: nil), .full)
    }

    func testACommandForAnotherFolderTakesTheModeFromTheEnvironmentAndReadsNoPreference() {
        let scratch = ["COCONUT_INSTALL_DIR": "/tmp/scratch"]
        var read = false
        func preference() -> String? {
            read = true
            return "team"
        }
        XCTAssertEqual(InstallMode.standard(environment: scratch, preference: preference()), .full)
        XCTAssertEqual(InstallMode.standard(environment: scratch.merging(["COCONUT_MODE": "team"]) { $1 },
                                            preference: preference()), .team)
        XCTAssertFalse(read)
    }

    func testTheModeGoesToTheEnvironmentAndAFullCommandNeverInheritsTeam() {
        var environment = ["COCONUT_MODE": "team", "PATH": "/bin"]
        InstallMode.full.apply(to: &environment)
        XCTAssertEqual(environment, ["PATH": "/bin"])
        InstallMode.team.apply(to: &environment)
        XCTAssertEqual(environment, ["PATH": "/bin", "COCONUT_MODE": "team"])
        XCTAssertNil(InstallMode.full.preferenceValue)
        XCTAssertEqual(InstallMode.team.preferenceValue, "team")
    }

    // MARK: The set-up

    func testOnlyTeamOnlyAsksForTheTeamMode() {
        XCTAssertNil(SetUpRequest.mode(arguments: ["Coconut"]))
        XCTAssertNil(SetUpRequest.mode(arguments: ["Coconut", "--team-only"]))
        XCTAssertEqual(SetUpRequest.mode(arguments: ["Coconut", "--set-up"]), .full)
        XCTAssertEqual(SetUpRequest.mode(arguments: ["Coconut", "--set-up", "--team-only"]), .team)
    }

    func testASetUpDoesNotChangeTheModeOfAnInstall() {
        XCTAssertNil(SetUpRequest.check(requested: .team, recorded: .full, installed: false))
        XCTAssertNil(SetUpRequest.check(requested: .full, recorded: .team, installed: false))
        XCTAssertNil(SetUpRequest.check(requested: .team, recorded: .team, installed: true))
        XCTAssertNil(SetUpRequest.check(requested: .full, recorded: .full, installed: true))
        XCTAssertEqual(SetUpRequest.check(requested: .team, recorded: .full, installed: true),
                       "This Mac has a full Coconut install. Remove Coconut first, then set it up for a team only.")
        XCTAssertEqual(SetUpRequest.check(requested: .full, recorded: .team, installed: true),
                       "This Mac runs Coconut for a team only. Remove Coconut first, then set it up again.")
    }

    func testTheAppRunsInstallSourceAsBeforeForAFullInstallAndWithoutTheSkillForATeam() {
        // The arguments of AppModel.update on origin/main.
        XCTAssertEqual(SetUpRequest.installSourceArguments(ifNeeded: true, setUp: false, mode: .full),
                       ["install-source", "--if-needed"])
        XCTAssertEqual(SetUpRequest.installSourceArguments(ifNeeded: false, setUp: false, mode: .full), ["install-source"])
        XCTAssertEqual(SetUpRequest.installSourceArguments(ifNeeded: false, setUp: true, mode: .full),
                       ["install-source", "--set-up"])
        XCTAssertEqual(SetUpRequest.installSourceArguments(ifNeeded: false, setUp: true, mode: .team), ["install-source"])
        XCTAssertEqual(SetUpRequest.installSourceArguments(ifNeeded: true, setUp: false, mode: .team),
                       ["install-source", "--if-needed"])
    }

    func testATeamOnlySetUpRegistersNoRelayAndRemovesOne() {
        let services = FakeLoginItems([.app: .notRegistered, .agent(.service): .notRegistered, .agent(.relay): .enabled])
        XCTAssertNil(services.items.register(for: .team))
        XCTAssertEqual(services.calls, ["register Coconut.app", "register com.gumroad.coconut.service",
                                        "unregister com.gumroad.coconut.relay"])
        // A full install registers the three items, as register() did before.
        let full = FakeLoginItems([.app: .notRegistered, .agent(.service): .enabled, .agent(.relay): .requiresApproval])
        XCTAssertNil(full.items.register(for: .full))
        XCTAssertEqual(full.calls, ["register Coconut.app", "register com.gumroad.coconut.relay"])
        XCTAssertEqual(InstallMode.team.loginItems, [.app, .agent(.service)])
        XCTAssertEqual(InstallMode.full.loginItems, LoginItem.allCases)
    }

    func testTheTeamOnlyServiceAndUpdateNameTheMode() throws {
        let service = Launch.service(install: install, python: python, environment: ["PATH": "/bin"], mode: .team)
        XCTAssertEqual(service.environment["COCONUT_MODE"], "team")
        XCTAssertEqual(service.arguments, Launch.service(install: install, python: python, environment: [:]).arguments)
        let full = Launch.service(install: install, python: python, environment: ["COCONUT_MODE": "team"])
        XCTAssertNil(full.environment["COCONUT_MODE"])
        try Data(#"{"package_id": "abc"}"#.utf8).write(to: home.appendingPathComponent("package.json"))
        let source = try XCTUnwrap(BundledSource(resources: home))
        let update = Launch.update(install: install, source: source, directory: "Coconut", python: python,
                                   sidecar: URL(fileURLWithPath: "/App/Contents/Helpers/tunnel-sidecar"),
                                   environment: ["PATH": "/bin"], home: URL(fileURLWithPath: "/Users/a"), mode: .team)
        XCTAssertEqual(update.environment["COCONUT_MODE"], "team")
        XCTAssertEqual(update.environment["COCONUT_APP_MANAGED"], "1")
    }

    func testATeamOnlyMacTakesNoPythonOfHermes() {
        let locator = PythonLocator(environment: ["COCONUT_HERMES_PYTHON": "/h/python", "HERMES_HOME": "/h"],
                                    home: URL(fileURLWithPath: "/Users/a"), mode: .team)
        XCTAssertEqual(locator.candidates().map(\.path), ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"])
    }

    func testATeamOnlyServiceAnswersOnTheMainListener() {
        XCTAssertEqual(InstallMode.team.healthURL.absoluteString, "http://127.0.0.1:8960/health")
        XCTAssertEqual(InstallMode.full.healthURL, StatusReader.healthURL)
    }

    // MARK: The tool

    func testEachCommandForAPhoneOrHermesRefusesOnATeamOnlyMac() {
        let commands: [[String]] = [["pair"], ["pair", "ABCD-EFGH"], ["migrate"], ["install-source", "--set-up"],
                                    ["sidecar"]]
        for arguments in commands {
            XCTAssertNil(TeamOnlyRefusal.line(arguments, mode: .full), "\(arguments)")
        }
        XCTAssertEqual(TeamOnlyRefusal.line(["pair"], mode: .team),
                       "This Mac runs Coconut for a team only, so it pairs no iPhone. Run coconut team to see its teams.")
        XCTAssertEqual(TeamOnlyRefusal.line(["pair", "ABCD-EFGH"], mode: .team), TeamOnlyRefusal.pair)
        XCTAssertEqual(TeamOnlyRefusal.line(["migrate"], mode: .team),
                       "This Mac runs Coconut for a team only, so it has no LaunchAgents of a full install to move.")
        XCTAssertEqual(TeamOnlyRefusal.line(["install-source", "--set-up"], mode: .team),
                       "This Mac runs Coconut for a team only, so Coconut adds nothing to Hermes. Run coconut "
                       + "install-source without --set-up.")
        XCTAssertEqual(TeamOnlyRefusal.line(["sidecar"], mode: .team),
                       "This Mac runs Coconut for a team only, so it runs no relay sidecar.")
        // The other commands run in each mode.
        for arguments in [["status"], ["status", "--json"], ["update"], ["settings"], ["version"], ["team"],
                          ["team", "join"], ["install-source"], ["install-source", "--if-needed"], ["service"]] {
            XCTAssertNil(TeamOnlyRefusal.line(arguments, mode: .team), "\(arguments)")
        }
    }

    static let answer: [String: Any] = [
        "mode": "team", "version": String(repeating: "a", count: 40), "helper": true, "bots": 2,
        "rooms": [["room": "r1", "name": "Example Team", "state": "active", "members": 3, "bots": 2,
                   "synced_at": "2026-11-11T10:00:00Z", "error": NSNull(), "socket": true],
                  ["room": "r2", "name": "Second Team", "state": "active", "members": 1, "bots": 0,
                   "synced_at": NSNull(), "error": "The relay refused the request.", "socket": false]],
    ]

    func testTheTeamStatusComesFromTheTeamOnlyRoute() async throws {
        let service = FakeService()
        service.answer("GET", "/v1/team/status", Self.answer)
        let status = try await TeamServiceStatus.fetch(transport: service)
        XCTAssertEqual(service.sent, ["GET /v1/team/status"])
        XCTAssertEqual(status.rooms.map(\.line), ["Example Team (active): 3 members, 2 bots, socket open",
                                                  "Second Team (active): 1 member, 0 bots, socket closed"])
        XCTAssertEqual(status.rooms[1].error, "The relay refused the request.")
    }

    func testTheStatusOfATeamOnlyMacNamesTheModeAndEachTeamAndNothingOfAPhoneOrHermes() async throws {
        let service = FakeService()
        service.answer("GET", "/v1/team/status", Self.answer)
        let team = try await TeamServiceStatus.fetch(transport: service)
        let preferences = UpdatePreferences(store: MemoryPreferences())
        let status = Status(install: "/Users/a/Coconut", service: .running, hermes: "missing", relay: "not_installed")
        XCTAssertEqual(StatusText.teamLines(version: "1.0 (1)", preferences: preferences, status: status,
                                            team: .success(team)), [
            "Coconut.app: 1.0 (1)",
            "Automatic updates: on",
            "Last update check: never",
            "Last update error: none",
            "Mode: team only",
            "Service: running (/Users/a/Coconut)",
            "Team helper: yes",
            "Bots of this Mac: 2",
            "Team: Example Team (active): 3 members, 2 bots, socket open",
            "Team: Second Team (active): 1 member, 0 bots, socket closed",
            "Sync error: The relay refused the request.",
        ])
        let stopped = Status(install: "/Users/a/Coconut", service: .stopped, hermes: "missing", relay: "not_installed",
                             serviceError: "This data folder belongs to a full Coconut install.")
        XCTAssertEqual(Array(StatusText.teamLines(version: "1.0 (1)", preferences: preferences, status: stopped,
                                                  team: .failure(.noAnswer)).suffix(4)), [
            "Mode: team only",
            "Service: stopped (/Users/a/Coconut)",
            "Service error: This data folder belongs to a full Coconut install.",
            "Teams: unknown while the service does not answer",
        ])
    }

    func testTheJSONStatusOfATeamOnlyMacHasTheMode() async throws {
        let service = FakeService()
        service.answer("GET", "/v1/team/status", Self.answer)
        let team = try await TeamServiceStatus.fetch(transport: service)
        let status = Status(install: "/Users/a/Coconut", service: .running, hermes: "missing", relay: "not_installed")
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(
            with: Data(TeamOnlyReport(status: status, team: .success(team)).json().utf8)) as? [String: Any])
        XCTAssertEqual(object["mode"] as? String, "team")
        XCTAssertEqual(object["service"] as? String, "running")
        XCTAssertEqual(object["helper"] as? Bool, true)
        XCTAssertEqual((object["teams"] as? [[String: Any]])?.first?["synced_at"] as? String, "2026-11-11T10:00:00Z")
        XCTAssertNil(object["hermes"])
        XCTAssertNil(object["relay"])
        let failed = try XCTUnwrap(try JSONSerialization.jsonObject(
            with: Data(TeamOnlyReport(status: status, team: .failure(.noAnswer)).json().utf8)) as? [String: Any])
        XCTAssertEqual(failed["team_error"] as? String, TeamFailure.noAnswer.lines.first)
    }

    // MARK: The cause of a service that does not start

    func write(_ text: String, to relative: String) throws {
        let url = home.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: url)
    }

    func testAStoppedServiceShowsTheCauseThatItLeft() throws {
        let install = Install(root: home)
        try write("print('host')", to: "scripts/host.py")
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        try write(#"{"code": "data_folder_mode", "error": "This data folder belongs to a full Coconut install.", "#
                  + #""mode": "team", "at": 1800000000}"#, to: ".runtime/host/start-error.json")
        let cause = try XCTUnwrap(StartError(install: install))
        XCTAssertEqual(cause, StartError(code: "data_folder_mode",
                                         error: "This data folder belongs to a full Coconut install.", at: at))
        let stopped = StatusReader(install: install).read(isAlive: { _ in false }, healthy: false)
        XCTAssertEqual(stopped.serviceError, "This data folder belongs to a full Coconut install.")
        XCTAssertTrue(StatusText.lines(version: "1.0 (1)", preferences: UpdatePreferences(store: MemoryPreferences()),
                                       status: stopped)
            .contains("Service error: This data folder belongs to a full Coconut install."))
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(stopped)) as? [String: Any]
        XCTAssertEqual(encoded?["service_error"] as? String, "This data folder belongs to a full Coconut install.")
        // A service that runs left the old cause behind only until its start removed it. The status shows none.
        try write(#"{"pid": 42}"#, to: ".runtime/host/process.json")
        XCTAssertNil(StatusReader(install: install).read(isAlive: { _ in true }, healthy: true).serviceError)
    }

    func testOnlyALastingCauseSlowsTheRestartsAndOnlyWhileItIsNew() {
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        let lasting = StartError(code: "data_folder_mode", error: "", at: at)
        XCTAssertEqual(lasting.wait(now: at.addingTimeInterval(5)), 295)
        XCTAssertNil(lasting.wait(now: at.addingTimeInterval(300)))
        // A clock that went back waits no more than the full wait.
        XCTAssertEqual(lasting.wait(now: at.addingTimeInterval(-600)), 300)
        XCTAssertNil(StartError(code: "hermes_unavailable", error: "", at: at).wait(now: at))
    }

    // MARK: A Mac without Python

    static let noPython = "Coconut found no Python 3.11 or later in /opt/homebrew/bin or /usr/local/bin. Install it "
        + "with Homebrew: brew install python"

    func testATeamOnlyMacWithoutPythonSaysWhatToInstall() {
        XCTAssertEqual(PythonLocator.missingLine(mode: .team), Self.noPython)
        // The line of a full install stays as it was.
        XCTAssertEqual(PythonLocator.missingLine(mode: .full),
                       "Coconut found no Python 3.11 or later. Install Hermes, or Python 3.11 or later from Homebrew.")
        let none = PythonLocator(environment: [:], home: home, mode: .team)
        XCTAssertNil(none.locate(accepts: { _ in false }))
    }

    func testASetUpWithoutPythonShowsTheLineAsTheLastUpdateError() {
        // The app keeps the last line of `coconut install-source` (AppModel.update), and coconut status prints it.
        let line = DeployResult.failureLine(output: Self.noPython + "\n")
        XCTAssertEqual(line, Self.noPython)
        XCTAssertEqual(DeployResult.failureLine(output: #"COCONUT_DEPLOY_RESULT={"ok": false, "error": "Bad."}"#), "Bad.")
        let preferences = UpdatePreferences(store: MemoryPreferences())
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1_800_000_000), outcome: .failed,
                                        message: "The service update failed: \(line)"))
        let status = Status(install: "/Users/a/Coconut", service: .notInstalled, hermes: "missing", relay: "not_installed")
        let lines = StatusText.teamLines(version: "1.0 (1)", preferences: preferences, status: status,
                                         team: .failure(.noAnswer))
        XCTAssertTrue(lines.contains("Last update error: The service update failed: \(Self.noPython)"), "\(lines)")
    }

    func testAnInstalledServiceWithoutPythonShowsTheCause() throws {
        let install = Install(root: home)
        try write("print('host')", to: "scripts/host.py")
        // Without a data folder, Coconut is not installed yet, and the tool leaves no file.
        StartError.write(code: StartError.noPython, error: Self.noPython, install: install)
        XCTAssertNil(StartError(install: install))
        try FileManager.default.createDirectory(at: install.data, withIntermediateDirectories: true)
        StartError.write(code: StartError.noPython, error: Self.noPython, install: install,
                         now: Date(timeIntervalSince1970: 1_800_000_000))
        XCTAssertEqual(StartError(install: install),
                       StartError(code: "no_python", error: Self.noPython, at: Date(timeIntervalSince1970: 1_800_000_000)))
        let mode = try FileManager.default.attributesOfItem(
            atPath: install.data.appendingPathComponent("start-error.json").path)[.posixPermissions] as? Int
        XCTAssertEqual(mode, 0o600)
        let status = StatusReader(install: install).read(isAlive: { _ in false }, healthy: false)
        XCTAssertTrue(StatusText.teamLines(version: "1.0 (1)", preferences: UpdatePreferences(store: MemoryPreferences()),
                                           status: status, team: .failure(.noAnswer))
            .contains("Service error: \(Self.noPython)"))
        // A missing Python is not a lasting cause: the login item waits as before.
        XCTAssertNil(StartError(install: install)?.wait(now: Date(timeIntervalSince1970: 1_800_000_000)))
    }

    func testOnlyRemoveAsksForTheRemoval() {
        XCTAssertTrue(RemoveRequest.asked(arguments: ["Coconut", "--remove"]))
        XCTAssertFalse(RemoveRequest.asked(arguments: ["Coconut", "--set-up", "--team-only"]))
    }

    func testAServiceWithoutACauseShowsNone() throws {
        let install = Install(root: home)
        try write("print('host')", to: "scripts/host.py")
        XCTAssertNil(StartError(install: install))
        XCTAssertNil(StatusReader(install: install).read(isAlive: { _ in false }, healthy: false).serviceError)
    }
}
