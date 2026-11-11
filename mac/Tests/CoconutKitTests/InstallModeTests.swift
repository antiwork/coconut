@testable import CoconutKit
import ServiceManagement
import SQLite3
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

    static let bots: [String: Any] = ["bots": [["id": "b1", "name": "Reporter", "avatar": "", "color": "",
                                                "home": "/h", "teams": ["r1"], "chat": true, "adapter": true],
                                               ["id": "b2", "name": "Planner", "avatar": "", "color": "",
                                                "home": "/p", "teams": [], "chat": false, "adapter": false]]]

    func service(status: [String: Any] = answer, bots: [String: Any] = bots) -> FakeService {
        let service = FakeService()
        service.answer("GET", "/v1/team/status", status)
        service.answer("GET", "/v1/team/bots", bots)
        return service
    }

    func testTheTeamStatusComesFromTheTeamOnlyRoutes() async throws {
        let service = service()
        let status = try await TeamServiceStatus.fetch(transport: service)
        XCTAssertEqual(service.sent, ["GET /v1/team/status", "GET /v1/team/bots"])
        XCTAssertEqual(status.botNames, ["Reporter", "Planner"])
        XCTAssertEqual(status.rooms.map(\.line), ["Example Team (active): 3 members, 2 bots, connected",
                                                  "Second Team (active): 1 member, 0 bots, not connected"])
    }

    func lines(_ team: Result<TeamServiceStatus, TeamFailure>, service: Status.Service = .running) -> [String] {
        StatusText.teamLines(version: "0.1.0 (688)", preferences: UpdatePreferences(store: MemoryPreferences()),
                             status: Status(install: "/Users/example/Coconut", service: service, hermes: "missing",
                                            relay: "not_installed"),
                             team: team)
    }

    func testTheStatusOfATeamOnlyMacSaysWhatIsTrueAndWhatToRun() async throws {
        let team = try await TeamServiceStatus.fetch(transport: service())
        XCTAssertEqual(lines(.success(team)), [
            "Coconut.app: 0.1.0 (688)",
            "Automatic updates: on",
            "Last update check: never",
            "Last update error: none",
            "Mode: team only",
            "Service: running (/Users/example/Coconut)",
            "Bots: Reporter, Planner",
            "Team chat of Reporter: on. Hermes takes the messages.",
            "Team chat of Planner: off. To turn it on, run: coconut team connect-chat Planner",
            "Team: Example Team (active): 3 members, 2 bots, connected",
            "Team: Second Team (active): 1 member, 0 bots, not connected",
            "Sync error: The relay refused the request.",
        ])
        var empty = Self.answer
        empty["helper"] = false
        empty["rooms"] = [[String: Any]]()
        let nothing = try await TeamServiceStatus.fetch(transport: service(status: empty, bots: ["bots": []]))
        XCTAssertEqual(Array(lines(.success(nothing)).suffix(3)), [
            "Coconut cannot join a team: a part of the app is missing. Run coconut update.",
            "Bots: none. To add one, run: coconut team new-bot <name>",
            "Teams: none. To join one, pipe an invite link to: coconut team join",
        ])
        // A bot with its chat whose Hermes gateway does not poll, and a service before the team chat.
        let waiting = ["bots": [["id": "b1", "name": "Night Reporter", "home": "/h", "chat": true, "adapter": false],
                                ["id": "b2", "name": "Planner", "home": "/p"],
                                ["id": "b3", "name": "Writer", "home": "/w", "chat": false, "plugin": true]]]
        let older = try await TeamServiceStatus.fetch(transport: service(bots: waiting))
        XCTAssertEqual(older.chats.map(\.line), [
            "Team chat of Night Reporter: on, but Hermes does not take the messages. Restart the Hermes gateway of "
                + "Night Reporter.",
            "Team chat of Planner: off. To turn it on, run: coconut team connect-chat Planner",
            "Team chat of Writer: not finished. To turn it on, run: coconut team connect-chat Writer --confirm. To turn "
                + "it off, run: coconut team disconnect-chat Writer"])
        XCTAssertEqual(lines(.failure(.noAnswer), service: .stopped).suffix(2),
                       ["Service: stopped (/Users/example/Coconut)", "Teams: unknown, because the service does not answer."])
        // No word of the code reaches the reader.
        for line in lines(.success(team)) + lines(.success(nothing)) {
            for word in ["helper", "socket", "room", "mark", "preference"] {
                XCTAssertFalse(line.lowercased().contains(word), line)
            }
        }
    }

    func testTheJSONStatusOfATeamOnlyMacHasTheMode() async throws {
        let team = try await TeamServiceStatus.fetch(transport: service())
        let status = Status(install: "/Users/a/Coconut", service: .running, hermes: "missing", relay: "not_installed")
        let object = try XCTUnwrap(try JSONSerialization.jsonObject(
            with: Data(TeamOnlyReport(status: status, team: .success(team)).json().utf8)) as? [String: Any])
        XCTAssertEqual(object["mode"] as? String, "team")
        XCTAssertEqual(object["service"] as? String, "running")
        XCTAssertEqual(object["bots"] as? [String], ["Reporter", "Planner"])
        XCTAssertEqual((object["team_chats"] as? [[String: Any]])?.map { $0["connected"] as? Bool }, [true, false])
        XCTAssertEqual((object["teams"] as? [[String: Any]])?.first?["synced_at"] as? String, "2026-11-11T10:00:00Z")
        XCTAssertNil(object["hermes"])
        XCTAssertNil(object["relay"])
        let failed = try XCTUnwrap(try JSONSerialization.jsonObject(
            with: Data(TeamOnlyReport(status: status, team: .failure(.noAnswer)).json().utf8)) as? [String: Any])
        XCTAssertEqual(failed["team_error"] as? String, TeamFailure.noAnswer.lines.first)
    }

    func write(_ text: String, to relative: String) throws {
        let url = home.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: url)
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

    // MARK: The removal

    func testOnlyRemoveAsksForTheRemovalAndAFullInstallGetsOneLine() {
        XCTAssertTrue(RemoveRequest.asked(arguments: ["Coconut", "--remove"]))
        XCTAssertFalse(RemoveRequest.asked(arguments: ["Coconut", "--set-up", "--team-only"]))
        XCTAssertEqual(RemoveRequest.fullInstall, "Coconut.app --remove removes only a team-only install. To remove "
                       + "this install, open Uninstall Coconut.command.")
        XCTAssertFalse(RemoveRequest.fullInstall.contains("\n"))
    }

    func testARemovalDeletesThePreferencesOfTheInstall() {
        let store = MemoryPreferences(["InstallMode": "team", "startAtLogin": false, "InstallDirectory": "/srv/checkout"])
        let preferences = UpdatePreferences(store: store)
        preferences.setAutomaticUpdates(false)
        preferences.requestCheck(at: Date(timeIntervalSince1970: 1_800_000_000))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1_800_000_001), outcome: .installing))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1_800_000_002), outcome: .failed,
                                        message: "An old error."))
        store.set(Date(), forKey: UpdatePreferences.lastCheckKey)
        RemoveRequest.preferenceKeys.forEach { store.set(nil, forKey: $0) }
        // Only the setting of the developer setup stays.
        XCTAssertEqual(Array(store.values.keys), ["InstallDirectory"])
        let status = Status(install: "/Users/a/Coconut", service: .notInstalled, hermes: "missing", relay: "not_installed")
        XCTAssertTrue(StatusText.lines(version: "1.0 (1)", preferences: preferences, status: status)
            .contains("Last update error: none"))
        XCTAssertEqual(RemoveRequest.preferenceKeys, ["InstallMode", "startAtLogin", "lastUpdateResult", "pendingInstall",
                                                      "updateRequestedAt", "SUEnableAutomaticChecks",
                                                      "SUAutomaticallyUpdate", "SULastCheckTime"])
    }

    // MARK: The mark of the data folder

    func makeStore(_ sql: String) throws -> Install {
        let install = Install(root: home)
        try FileManager.default.createDirectory(at: install.data, withIntermediateDirectories: true)
        var db: OpaquePointer?
        XCTAssertEqual(sqlite3_open(install.data.appendingPathComponent("coconut.sqlite3").path, &db), SQLITE_OK)
        XCTAssertEqual(sqlite3_exec(db, sql, nil, nil, nil), SQLITE_OK)
        sqlite3_close(db)
        return install
    }

    func testTheMarkOfATeamOnlyStoreDecidesWithThePreference() throws {
        // Without a store, nothing is marked, and the read makes no file.
        XCTAssertFalse(TeamFolderMark.isTeamOnly(install: Install(root: home)))
        XCTAssertFalse(FileManager.default.fileExists(atPath: Install(root: home).data.path))
        // The store of a full install has no mark.
        let full = try makeStore("CREATE TABLE devices (id TEXT PRIMARY KEY); INSERT INTO devices VALUES ('phone');")
        XCTAssertFalse(TeamFolderMark.isTeamOnly(install: full))
        // The mark of service_base.MODE_MARK.
        let install = try makeStore("CREATE TABLE IF NOT EXISTS service_mode (mode TEXT PRIMARY KEY); "
                                    + "INSERT OR IGNORE INTO service_mode (mode) VALUES ('team');")
        let files = try FileManager.default.contentsOfDirectory(atPath: install.data.path)
        XCTAssertTrue(TeamFolderMark.isTeamOnly(install: install))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: install.data.path), files)
    }

    func testARemovalAsksFirstAndARefusalChangesNothing() throws {
        var steps: [String] = []
        func remove(_ install: Install) -> String? {
            RemoveRequest.run(install: install, unregister: { steps.append("unregister"); return nil },
                              uninstall: { steps.append("uninstall"); return nil })
        }
        // A bot whose chat is connected, and a bot whose clean-up did not finish: nothing runs.
        let install = try makeStore("CREATE TABLE bots (id TEXT PRIMARY KEY, data TEXT NOT NULL); "
                                    + "INSERT INTO bots VALUES ('a', '{\"name\": \"Reporter\", \"chat\": true}'); "
                                    + "INSERT INTO bots VALUES ('b', '{\"name\": \"Planner\", \"chat\": false}'); "
                                    + "INSERT INTO bots VALUES ('c', '{\"name\": \"Writer\", \"plugin\": true}');")
        XCTAssertEqual(remove(install), "The Hermes home of Reporter, Writer has the team chat of Coconut. For each bot, "
                       + "run coconut team disconnect-chat <bot>, then remove Coconut.")
        XCTAssertEqual(steps, [])
        // Without them, the steps run in their order.
        var db: OpaquePointer?
        sqlite3_open(install.data.appendingPathComponent("coconut.sqlite3").path, &db)
        sqlite3_exec(db, "DELETE FROM bots WHERE id != 'b'", nil, nil, nil)
        sqlite3_close(db)
        XCTAssertNil(remove(install))
        XCTAssertEqual(steps, ["unregister", "uninstall"])
        // A store without bots, and no store, have nothing to ask.
        try FileManager.default.removeItem(at: install.data)
        XCTAssertNil(RemoveRequest.refusal(install: install))
        XCTAssertNil(RemoveRequest.refusal(install: try makeStore("CREATE TABLE devices (id TEXT PRIMARY KEY);")))
    }

    // MARK: The timeouts

    /// A listener on 127.0.0.1 that takes each connection in its backlog and never answers. Return the socket and port.
    func silentListener() throws -> (Int32, UInt16) {
        let listener = socket(AF_INET, SOCK_STREAM, 0)
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(listener, $0, length) }
        }
        XCTAssertEqual(bound, 0)
        XCTAssertEqual(listen(listener, 8), 0)
        _ = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(listener, $0, &length) }
        }
        return (listener, UInt16(bigEndian: address.sin_port))
    }

    func testAServiceThatDoesNotAnswerHoldsTheStatusOnlyForTheShortTimeout() async throws {
        let (listener, port) = try silentListener()
        defer { close(listener) }
        let install = Install(root: home)
        try write("print('host')", to: "scripts/host.py")
        try write(#"{"url": "http://127.0.0.1:\#(port)", "token": "test-token"}"#, to: ".runtime/host/gateway/bridge.json")
        let started = Date()
        let team = await TeamServiceStatus.read(install: install)
        let waited = Date().timeIntervalSince(started)
        XCTAssertLessThan(waited, BridgeTransport.readTimeout + 3, "The read waited \(waited) s.")
        XCTAssertEqual(team, .failure(.noAnswer))
        XCTAssertEqual(lines(team).last, "Teams: unknown, because the service does not answer.")
        // The list of `coconut team` also runs on a busy full install, so it waits longer. A join or a leave waits longest.
        XCTAssertEqual(TeamCommand.show(json: false).timeout, 15)
        XCTAssertEqual(TeamCommand.join(name: nil).timeout, 180)
        XCTAssertEqual(TeamCommand.leave(team: nil, confirm: nil).timeout, 180)
    }
}
