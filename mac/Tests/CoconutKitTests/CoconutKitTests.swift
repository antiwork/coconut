import CryptoKit
@testable import CoconutKit
import ServiceManagement
import XCTest

final class CoconutKitTests: XCTestCase {
    var home: URL!

    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            .resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    func write(_ text: String, to relative: String, executable: Bool = false) throws {
        let url = home.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: url)
        if executable {
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        }
    }

    // MARK: Install

    func testTheInstallIsCoconutInTheHomeFolderUnlessTheEnvironmentNamesAnother() {
        XCTAssertEqual(Install.standard(environment: [:], preference: nil, home: home).root.path,
                       home.appendingPathComponent("Coconut").path)
        XCTAssertEqual(Install.standard(environment: [:], preference: "/srv/saved", home: home).root.path, "/srv/saved")
        XCTAssertEqual(Install.standard(environment: ["COCONUT_INSTALL_DIR": "/srv/coconut"], preference: "/srv/saved",
                                        home: home).root.path, "/srv/coconut")
        let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
        XCTAssertEqual(install.hostScript.path, "/Users/a/Coconut/scripts/host.py")
        XCTAssertEqual(install.data.path, "/Users/a/Coconut/.runtime/host")
        XCTAssertEqual(install.tunnel.path, "/Users/a/Coconut/.runtime/host/tunnel")
    }

    // MARK: Python

    func testThePythonSearchPrefersAnExplicitPythonThenHomebrewThenHermes() throws {
        let locator = PythonLocator(environment: ["COCONUT_HERMES_PYTHON": "/custom/python"], home: home)
        XCTAssertEqual(locator.candidates().map(\.path), [
            "/custom/python", "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
            home.appendingPathComponent(".hermes/hermes-agent/venv/bin/python").path,
            home.appendingPathComponent(".hermes/hermes-agent/.venv/bin/python").path,
        ])
        XCTAssertFalse(locator.candidates().contains { $0.path == "/usr/bin/python3" })
    }

    func testThePythonSearchReadsTheEnvironmentThatTheHermesInstallerCommitted() throws {
        let source = home.appendingPathComponent(".hermes/hermes-agent", isDirectory: true)
        try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
        let key = SHA256.hash(data: Data(source.path.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16)
        let environment = home.appendingPathComponent(".hermes/installs/\(key)/env")
        try write("#!/bin/sh\n", to: ".hermes/installs/\(key)/env/bin/python", executable: true)
        try write(#"{"packages": {"venv": {"environment": "\#(environment.path)"}}}"#, to: ".hermes/installs/\(key)/facts.json")
        let candidates = PythonLocator(environment: [:], home: home).candidates()
        XCTAssertEqual(candidates.last?.path, environment.appendingPathComponent("bin/python").path)
    }

    func testThePythonSearchSkipsAMissingOrRefusedPython() throws {
        try write("#!/bin/sh\n", to: ".hermes/hermes-agent/venv/bin/python", executable: true)
        let venv = home.appendingPathComponent(".hermes/hermes-agent/venv/bin/python")
        let locator = PythonLocator(environment: [:], home: home)
        XCTAssertEqual(locator.locate(accepts: { $0 == venv })?.path, venv.path)
        XCTAssertNil(locator.locate(accepts: { _ in false }))
    }

    func testAnOldPythonIsNotSupported() throws {
        try write("#!/bin/sh\nexit 1\n", to: "old-python", executable: true)
        try write("#!/bin/sh\nexit 0\n", to: "new-python", executable: true)
        XCTAssertFalse(PythonLocator.isSupported(home.appendingPathComponent("old-python")))
        XCTAssertTrue(PythonLocator.isSupported(home.appendingPathComponent("new-python")))
        XCTAssertFalse(PythonLocator.isSupported(home.appendingPathComponent("missing-python")))
    }

    // MARK: Launch

    func testTheServiceRunsHostPyServeWithTheDataOfTheInstall() {
        let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
        let launch = Launch.service(install: install, python: URL(fileURLWithPath: "/opt/homebrew/bin/python3"),
                                    environment: ["PATH": "/bin"])
        XCTAssertEqual(launch.arguments, ["/opt/homebrew/bin/python3", "/Users/a/Coconut/scripts/host.py", "serve",
                                          "--data-dir", "/Users/a/Coconut/.runtime/host", "--provider", "hermes"])
        XCTAssertEqual(launch.directory.path, "/Users/a/Coconut/backend")
        XCTAssertEqual(launch.environment, ["PATH": "/bin", "PYTHONUNBUFFERED": "1",
                                            "COCONUT_LOGIN_ITEM": "com.gumroad.coconut.service"])
        XCTAssertEqual(launch.standardOutput?.path, "/Users/a/Coconut/.runtime/host/logs/service.log")
        XCTAssertEqual(launch.standardError?.path, "/Users/a/Coconut/.runtime/host/logs/error.log")
    }

    func testTheSidecarForwardsToTheTunnelListener() {
        let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
        let launch = Launch.sidecar(install: install, binary: URL(fileURLWithPath: "/App/Contents/Helpers/tunnel-sidecar"),
                                    environment: [:])
        XCTAssertEqual(launch.arguments, ["/App/Contents/Helpers/tunnel-sidecar", "run", "--data-dir",
                                          "/Users/a/Coconut/.runtime/host", "--backend", "127.0.0.1:8961"])
        XCTAssertEqual(launch.standardOutput?.lastPathComponent, "tunnel.log")
    }

    func testThePairingLinkNeedsNoSwiftAndKeepsTheOutputOfTheTool() {
        let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
        let launch = Launch.pairingLink(install: install, python: URL(fileURLWithPath: "/usr/local/bin/python3"), environment: [:])
        XCTAssertEqual(launch.arguments.first, "/usr/local/bin/python3")
        XCTAssertEqual(launch.arguments[1], "-c")
        XCTAssertFalse(launch.arguments[2].contains("render"))
        XCTAssertEqual(launch.arguments.last, "/Users/a/Coconut/scripts")
        XCTAssertNil(launch.standardOutput)
        XCTAssertNil(launch.standardError)
    }

    func testThePairingLinkSaysHowLongTheCodeLasts() throws {
        let python = URL(fileURLWithPath: "/usr/bin/env")
        try write("""
            def local_payload():
                return {'version': 2}
            def link(payload):
                return 'coconut://pair/abc'
            """, to: "Coconut/scripts/pairing.py")
        let launch = Launch.pairingLink(install: Install(root: home.appendingPathComponent("Coconut")), python: python,
                                        environment: [:])
        let process = Process()
        process.executableURL = python
        process.arguments = ["python3"] + launch.arguments.dropFirst()
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
        XCTAssertEqual(PairingLink(output: text)?.link, "coconut://pair/abc")
        XCTAssertTrue(text.contains("expires after ten minutes"), text)
    }

    func testTheSavedHermesSettingsReachTheService() {
        XCTAssertEqual(LoginEnvironment.savedSettings(["COCONUT_HERMES_PYTHON": "/h/python", "HERMES_HOME": "/h",
                                                       "PATH": "/evil", "COCONUT_HERMES_ROOT": ""]),
                       ["COCONUT_HERMES_PYTHON": "/h/python", "HERMES_HOME": "/h"])
        XCTAssertEqual(LoginEnvironment.savedSettings(nil), [:])
        let environment = LoginEnvironment.make(base: ["HOME": "/Users/a"], loginPath: "/bin",
                                                saved: ["COCONUT_HERMES_PYTHON": "/h/python"])
        XCTAssertEqual(environment, ["HOME": "/Users/a", "PATH": "/bin", "COCONUT_HERMES_PYTHON": "/h/python"])
    }

    func testTheLoginItemUsesThePathOfTheLoginShell() {
        XCTAssertEqual(LoginEnvironment.parse("motd\n\(LoginEnvironment.mark)/opt/homebrew/bin:/bin\(LoginEnvironment.mark)\n"),
                       "/opt/homebrew/bin:/bin")
        XCTAssertNil(LoginEnvironment.parse("no marks"))
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], loginPath: "/a:/b", saved: [:])["PATH"], "/a:/b")
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], loginPath: nil, saved: [:])["PATH"],
                       LoginEnvironment.fallbackPath)
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/mine"], loginPath: nil, saved: [:])["PATH"], "/mine")
    }

    // MARK: Login items

    func testTheLaunchAgentsOfTheBundleRunTheToolAndStayAlive() throws {
        let folder = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../LaunchAgents")
        for agent in Agent.allCases {
            let data = try Data(contentsOf: folder.appendingPathComponent(agent.plistName))
            let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
            XCTAssertEqual(plist["Label"] as? String, agent.rawValue)
            XCTAssertEqual(plist["BundleProgram"] as? String, "Contents/Helpers/coconut")
            XCTAssertEqual(plist["ProgramArguments"] as? [String], ["coconut", agent.command])
            XCTAssertEqual(plist["AssociatedBundleIdentifiers"] as? [String], ["com.gumroad.coconut.mac"])
            // A bot's restart request ends the service with exit code 0. launchd must start it again.
            XCTAssertEqual(plist["KeepAlive"] as? Bool, true)
            XCTAssertEqual(plist["RunAtLoad"] as? Bool, true)
        }
    }

    func testTheAppOpensAtLoginWithItsTwoAgents() {
        // After a restart of the Mac, the app shows its menu bar icon and Sparkle checks for updates.
        XCTAssertEqual(LoginItem.allCases, [.app, .agent(.service), .agent(.relay)])
        XCTAssertEqual(LoginItem.app.name, "Coconut.app")
        XCTAssertEqual(LoginItem.agent(.relay).name, "com.gumroad.coconut.relay")
        XCTAssertTrue((LoginItem.app.service as? SMAppService) === SMAppService.mainApp)
    }

    func testSetUpRegistersEachLoginItemThatIsNotOnAndKeepsOneThatWaitsForApproval() {
        let services = FakeLoginItems([.app: .notRegistered, .agent(.service): .enabled, .agent(.relay): .requiresApproval])
        XCTAssertNil(services.items.register())
        // An item that is on stays as it is. One that waits for approval registers again, as the agents did before.
        XCTAssertEqual(services.calls, ["register Coconut.app", "register com.gumroad.coconut.relay"])
        XCTAssertEqual(services.items.statuses()[.app], .enabled)
        XCTAssertEqual(services.items.statuses()[.agent(.relay)], .requiresApproval)
        // A later launch registers nothing more while each item is registered.
        services.calls = []
        services.statuses[.agent(.relay)] = .enabled
        XCTAssertNil(services.items.register())
        XCTAssertEqual(services.calls, [])
    }

    func testStartAtLoginOffRemovesTheAppWithItsAgents() {
        let services = FakeLoginItems([.app: .enabled, .agent(.service): .requiresApproval, .agent(.relay): .notRegistered])
        XCTAssertNil(services.items.unregister())
        XCTAssertEqual(services.calls, ["unregister Coconut.app", "unregister com.gumroad.coconut.service"])
    }

    func testAFailedLoginItemNamesTheItem() {
        let services = FakeLoginItems([.app: .notRegistered, .agent(.service): .enabled, .agent(.relay): .enabled])
        services.failing = [.app]
        XCTAssertEqual(services.items.register()?.hasPrefix("Coconut could not add its login item Coconut.app:"), true)
        services.statuses[.app] = .enabled
        XCTAssertEqual(services.items.unregister()?.hasPrefix("Coconut could not remove its login item Coconut.app:"), true)
    }

    func testAnEarlierLaunchAgentIsFound() throws {
        XCTAssertEqual(LegacyAgents.present(home: home), [])
        try write("", to: "Library/LaunchAgents/com.gumroad.coconut.tunnel.plist")
        XCTAssertEqual(LegacyAgents.present(home: home), ["com.gumroad.coconut.tunnel"])
        try write("", to: "Library/LaunchAgents/local.coconut.agent.plist")
        XCTAssertEqual(LegacyAgents.present(home: home), ["local.coconut.agent", "com.gumroad.coconut.tunnel"])
    }

    // MARK: Status

    func testTheStatusReadsTheFilesOfTheService() throws {
        let install = Install(root: home.appendingPathComponent("Coconut"))
        let reader = StatusReader(install: install)
        XCTAssertEqual(reader.read(isAlive: { _ in true }, healthy: true).service, .notInstalled)

        try write("", to: "Coconut/scripts/host.py")
        var status = reader.read(isAlive: { _ in true }, healthy: true)
        XCTAssertEqual(status.service, .stopped)
        XCTAssertEqual(status.hermes, "missing")
        XCTAssertEqual(status.relay, "not_installed")

        try write(#"{"pid": 4242, "drain": true}"#, to: "Coconut/.runtime/host/process.json")
        try write(#"{"status": "ok", "version": "0.22.5", "python": "/x"}"#, to: "Coconut/.runtime/host/hermes.json")
        try write("{}", to: "Coconut/.runtime/host/tunnel/route.json")
        XCTAssertEqual(reader.read(isAlive: { $0 == 4242 }, healthy: false).service, .starting)
        XCTAssertEqual(reader.read(isAlive: { _ in false }, healthy: true).service, .stopped)
        status = reader.read(isAlive: { $0 == 4242 }, healthy: true)
        XCTAssertEqual(status.service, .running)
        XCTAssertEqual(status.hermes, "ok")
        XCTAssertEqual(status.hermesVersion, "0.22.5")
        XCTAssertEqual(status.relay, "stopped")

        try write(#"{"state": "connected", "relay": "https://relay", "pid": 5151}"#, to: "Coconut/.runtime/host/tunnel/status.json")
        XCTAssertEqual(reader.read(isAlive: { _ in true }, healthy: true).relay, "connected")
        // A sidecar that stopped leaves its last state behind.
        XCTAssertEqual(reader.read(isAlive: { $0 != 5151 }, healthy: true).relay, "stopped")
        XCTAssertNil(reader.read(isAlive: { _ in true }, healthy: true).relayLastDisconnect)

        try write(#"{"state": "connected", "pid": 5151, "reconnects": 3, "last_disconnect": {"at": "2026-10-28T17:36:05Z", "cause": "relay_closed", "code": 1011, "detail": "code 1011: internal error"}}"#,
                  to: "Coconut/.runtime/host/tunnel/status.json")
        status = reader.read(isAlive: { $0 != 5151 }, healthy: true)
        XCTAssertEqual(status.relayLastDisconnect, .init(at: "2026-10-28T17:36:05Z", cause: "relay_closed",
                                                         detail: "code 1011: internal error"))
        XCTAssertEqual(status.relayReconnects, 3)
        let lines = StatusText.lines(version: "1", preferences: UpdatePreferences(store: MemoryPreferences()), status: status)
        XCTAssertTrue(lines.contains("Last relay disconnect: 2026-10-28T17:36:05Z, relay_closed (code 1011: internal error); "
            + "3 reconnects since the sidecar started"))

        XCTAssertNil(status.previews)
        try write(#"{"phones": [{"name": "iPhone", "line": "failing since 2026-10-30T18:07:00Z: keychain read -34018", "extension": {}}]}"#,
                  to: "Coconut/.runtime/host/push-previews.json")
        status = reader.read(isAlive: { _ in true }, healthy: true)
        XCTAssertEqual(StatusText.lines(version: "1", preferences: UpdatePreferences(store: MemoryPreferences()), status: status).last,
                       "Notification previews (iPhone): failing since 2026-10-30T18:07:00Z: keychain read -34018")
    }

    func testTheStatusJSONUsesSnakeCaseKeys() throws {
        let status = Status(install: "/c", service: .notInstalled, hermes: "failed", hermesVersion: nil,
                            hermesReason: "No Hermes", relay: "not_installed")
        let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(status)) as? [String: Any]
        XCTAssertEqual(object?["service"] as? String, "not_installed")
        XCTAssertEqual(object?["hermes_reason"] as? String, "No Hermes")
    }

    func testThisProcessIsAlive() {
        XCTAssertTrue(StatusReader.isAlive(getpid()))
    }

    // MARK: Update

    func testTheBundledSourceIsNewerUntilItsPackageIsInstalled() throws {
        try write(#"{"package_id": "abc"}"#, to: "Resources/package.json")
        let source = try XCTUnwrap(BundledSource(resources: home.appendingPathComponent("Resources")))
        XCTAssertEqual(source.packageID, "abc")
        XCTAssertEqual(source.package.lastPathComponent, "coconut-source.tar.gz")
        let install = Install(root: home.appendingPathComponent("Coconut"))
        XCTAssertTrue(source.isNewer(than: install))
        try write(#"{"package_id": "abc", "state": "pending"}"#, to: "Coconut/.coconut-install.json")
        XCTAssertTrue(source.isNewer(than: install), "An install that did not finish needs the update again.")
        try write(#"{"package_id": "abc", "state": "installed"}"#, to: "Coconut/.coconut-install.json")
        XCTAssertFalse(source.isNewer(than: install))
        try write(#"{"package_id": "old", "state": "installed"}"#, to: "Coconut/.coconut-install.json")
        XCTAssertTrue(source.isNewer(than: install))
        XCTAssertNil(BundledSource(resources: home.appendingPathComponent("Missing")))
    }

    func testAGitCheckoutIsNotAnInstallOfTheApp() throws {
        let install = Install(root: home.appendingPathComponent("Coconut"))
        XCTAssertFalse(install.isCheckout())
        try write("", to: "Coconut/.git/HEAD")
        XCTAssertTrue(install.isCheckout())
        try write(#"{"state": "installed"}"#, to: "Coconut/.coconut-install.json")
        XCTAssertFalse(install.isCheckout(), "deploy.py owns a folder with its install record.")
    }

    func testDeployNeedsAFolderInsideTheHomeFolder() {
        let home = URL(fileURLWithPath: "/Users/a")
        XCTAssertEqual(Install(root: URL(fileURLWithPath: "/Users/a/Coconut")).relativePath(home: home), "Coconut")
        XCTAssertEqual(Install(root: URL(fileURLWithPath: "/Users/a/Apps/Coconut")).relativePath(home: home), "Apps/Coconut")
        XCTAssertNil(Install(root: URL(fileURLWithPath: "/Users/ab/Coconut")).relativePath(home: home))
        XCTAssertNil(Install(root: URL(fileURLWithPath: "/srv/Coconut")).relativePath(home: home))
        XCTAssertNil(Install(root: home).relativePath(home: home))
    }

    func testTheServiceWaitsOnlyWhileTheHoldLasts() throws {
        let install = Install(root: home.appendingPathComponent("Coconut"))
        let now = Date(timeIntervalSince1970: 1000)
        XCTAssertFalse(install.isHeld(now: now))
        try write("1600.5\n", to: "Coconut/.runtime/host/update-hold")
        XCTAssertTrue(install.isHeld(now: now))
        XCTAssertFalse(install.isHeld(now: Date(timeIntervalSince1970: 1601)), "A hold ends by itself.")
        try write("garbage", to: "Coconut/.runtime/host/update-hold")
        XCTAssertFalse(install.isHeld(now: now))
    }

    func testTheUpdateRunsDeployWithTheAppModeAndTheBundledSidecar() throws {
        try write(#"{"package_id": "abc"}"#, to: "Resources/package.json")
        let source = try XCTUnwrap(BundledSource(resources: home.appendingPathComponent("Resources")))
        let install = Install(root: URL(fileURLWithPath: "/Users/a/Coconut"))
        let launch = Launch.update(install: install, source: source, directory: "Coconut",
                                   python: URL(fileURLWithPath: "/opt/homebrew/bin/python3"),
                                   sidecar: URL(fileURLWithPath: "/App/Contents/Helpers/tunnel-sidecar"), environment: ["PATH": "/bin"],
                                   home: URL(fileURLWithPath: "/Users/a"))
        XCTAssertEqual(launch.arguments, ["/opt/homebrew/bin/python3", source.deployScript.path, "receive", "--directory", "Coconut"])
        XCTAssertEqual(launch.environment["COCONUT_APP_MANAGED"], "1")
        XCTAssertEqual(launch.environment["COCONUT_SIDECAR"], "/App/Contents/Helpers/tunnel-sidecar")
        XCTAssertEqual(launch.environment["PATH"], "/bin")
        // Only a setup that the person asked for lets deploy.py change the Hermes config.
        XCTAssertNil(launch.environment["COCONUT_SET_UP"])
        let setUp = Launch.update(install: install, source: source, directory: "Coconut",
                                  python: URL(fileURLWithPath: "/opt/homebrew/bin/python3"),
                                  sidecar: URL(fileURLWithPath: "/App/Contents/Helpers/tunnel-sidecar"), environment: [:],
                                  home: URL(fileURLWithPath: "/Users/a"), setUp: true)
        XCTAssertEqual(setUp.environment["COCONUT_SET_UP"], "1")
        // The install folder and its parents may not exist yet. deploy.py creates them.
        XCTAssertEqual(launch.directory.path, "/Users/a")
        XCTAssertNil(launch.standardOutput)
        let migrate = Launch.migrate(install: install, python: URL(fileURLWithPath: "/usr/local/bin/python3"), environment: [:])
        XCTAssertEqual(migrate.arguments, ["/usr/local/bin/python3", "/Users/a/Coconut/scripts/host.py", "migrate-to-app"])
    }

    func testTheDeployResultIsTheLastResultLine() {
        let failed = DeployResult(output: "log\nCOCONUT_DEPLOY_RESULT={\"ok\": false, \"error\": \"The update failed.\"}\n")
        XCTAssertEqual(failed?.ok, false)
        XCTAssertEqual(failed?.error, "The update failed.")
        let done = DeployResult(output: "COCONUT_DEPLOY_RESULT={\"ok\": true, \"package_id\": \"abc\", \"mode\": \"app\"}")
        XCTAssertEqual(done?.ok, true)
        XCTAssertEqual(done?.packageID, "abc")
        XCTAssertNil(done?.relayError)
        let relay = DeployResult(output: #"COCONUT_DEPLOY_RESULT={"ok": true, "tunnel": {"ok": false, "error": "Tunnel sidecar build failed (exit 1)."}}"#)
        XCTAssertEqual(relay?.ok, true)
        XCTAssertEqual(relay?.relayError, "Tunnel sidecar build failed (exit 1).")
        XCTAssertNil(DeployResult(output: "Traceback"))
    }

    // MARK: Update preferences

    func testAutomaticUpdatesAreOnUntilTurnedOff() {
        let store = MemoryPreferences()
        let preferences = UpdatePreferences(store: store)
        XCTAssertTrue(preferences.automaticUpdates)
        preferences.setAutomaticUpdates(false)
        // Sparkle reads the same two keys.
        XCTAssertEqual(store.values["SUEnableAutomaticChecks"] as? Bool, false)
        XCTAssertEqual(store.values["SUAutomaticallyUpdate"] as? Bool, false)
        XCTAssertFalse(preferences.automaticUpdates)
        preferences.setAutomaticUpdates(true)
        XCTAssertTrue(preferences.automaticUpdates)
    }

    func testARequestInstallsWithoutAQuestionOrFailsAtOnce() {
        XCTAssertEqual(UpdateRequestAction.decide(sessionInProgress: false, downloadsAutomatically: true), .check)
        // Sparkle would show its window and wait for a click that nobody sees, for example over SSH.
        guard case .fail(let question) = UpdateRequestAction.decide(sessionInProgress: false, downloadsAutomatically: false) else {
            return XCTFail("A check that would ask must not start.")
        }
        XCTAssertTrue(question.contains("coconut settings automatic-updates on"))
        guard case .fail(let busy) = UpdateRequestAction.decide(sessionInProgress: true, downloadsAutomatically: true) else {
            return XCTFail("Sparkle ignores a check while another one runs.")
        }
        XCTAssertTrue(busy.contains("in progress"))
        // The failure answers the request at once, so `coconut update` does not wait five minutes.
        let preferences = UpdatePreferences(store: MemoryPreferences())
        preferences.requestCheck(at: Date(timeIntervalSince1970: 100))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 101), outcome: .failed, message: question))
        XCTAssertNil(preferences.pendingRequest)
    }

    func testARequestWaitsUntilAResultAnswersIt() {
        let preferences = UpdatePreferences(store: MemoryPreferences())
        XCTAssertNil(preferences.pendingRequest)
        preferences.requestCheck(at: Date(timeIntervalSince1970: 100))
        XCTAssertEqual(preferences.pendingRequest, Date(timeIntervalSince1970: 100))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 99), outcome: .upToDate))
        XCTAssertNotNil(preferences.pendingRequest, "An older result does not answer a newer request.")
        let result = UpdateResult(time: Date(timeIntervalSince1970: 101), outcome: .installing, version: "0.2.0 (510)")
        preferences.record(result)
        XCTAssertNil(preferences.pendingRequest)
        XCTAssertEqual(preferences.lastResult, result)
    }

    func testALaterCheckDoesNotEndAPendingInstall() {
        let preferences = UpdatePreferences(store: MemoryPreferences())
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1), outcome: .installing, version: "0.2.0 (510)"))
        XCTAssertEqual(preferences.pendingInstall, "0.2.0 (510)")
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 2), outcome: .upToDate))
        XCTAssertEqual(preferences.pendingInstall, "0.2.0 (510)")
        let status = Status(install: "/c", service: .running, hermes: "ok", hermesVersion: nil, hermesReason: nil, relay: "connected")
        XCTAssertTrue(StatusText.lines(version: "0.2.0 (510)", preferences: preferences, status: status)
            .contains("Pending install: 0.2.0 (510) (the service updates after its running bot work)"))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 3), outcome: .installed, version: "0.2.0 (510)"))
        XCTAssertNil(preferences.pendingInstall)
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 4), outcome: .installing))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 5), outcome: .failed, message: "x"))
        XCTAssertNil(preferences.pendingInstall)
    }

    func testTheStatusTextNamesTheSettingsAndTheLastError() {
        let store = MemoryPreferences(["SULastCheckTime": Date(timeIntervalSince1970: 1_793_000_000),
                                       "SUAutomaticallyUpdate": false])
        let preferences = UpdatePreferences(store: store)
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1_793_000_100), outcome: .failed,
                                        message: "The feed is unavailable."))
        let status = Status(install: "/Users/a/Coconut", service: .running, hermes: "ok", hermesVersion: "0.22.5",
                            hermesReason: nil, relay: "connected")
        let lines = StatusText.lines(version: "0.1.0 (502)", preferences: preferences, status: status)
        XCTAssertEqual(lines, [
            "Coconut.app: 0.1.0 (502)",
            "Automatic updates: off",
            "Last update check: 2026-10-26T07:33:20Z",
            "Last update result: failed (2026-10-26T07:35:00Z)",
            "Last update error: The feed is unavailable.",
            "Service: running (/Users/a/Coconut)",
            "Relay: connected",
            "Hermes: 0.22.5",
        ])
        let fresh = StatusText.lines(version: "0.1.0 (502)", preferences: UpdatePreferences(store: MemoryPreferences()), status: status)
        XCTAssertTrue(fresh.contains("Last update check: never"))
        preferences.record(UpdateResult(time: Date(timeIntervalSince1970: 1_793_000_200), outcome: .installed, version: "0.1.0 (510)"))
        let done = StatusText.lines(version: "0.1.0 (510)", preferences: preferences, status: status)
        XCTAssertTrue(done.contains("Last update result: installed 0.1.0 (510) (2026-10-26T07:36:40Z)"))
        XCTAssertTrue(done.contains("Last update error: none"))
        XCTAssertTrue(fresh.contains("Last update error: none"))
    }

    // MARK: Pairing

    func testThePairingOutputGivesTheLinkAndTheImage() {
        let output = """
            Setup link: coconut://pair/mfrggzdf
            QR image: /Users/a/Coconut/.runtime/pairing/iphone-setup.png
            On the iPhone, tap the link, or scan the QR image in Coconut.
            """
        XCTAssertEqual(PairingLink(output: output),
                       PairingLink(link: "coconut://pair/mfrggzdf", image: "/Users/a/Coconut/.runtime/pairing/iphone-setup.png"))
        XCTAssertNil(PairingLink(output: "Setup link: coconut://pair?setup=abc\n"))
        XCTAssertNil(PairingLink(output: "Coconut could not create the setup QR."))
        XCTAssertNil(PairingLink(output: "Setup link: https://example.com"))
    }

    // MARK: Pairing code

    // The same vector is in docs/relay-transport.md, from the HKDF of scripts/pairing.py.
    func testThePairingCodeGivesTheBoxIDOfTheProtocol() throws {
        let code = try XCTUnwrap(PairingCode("aaaqeayeaudaocajbifqydiob4"))
        XCTAssertEqual(code.bytes, Data(0..<16))
        XCTAssertEqual(code.boxIDHex, "1d228ab884d178483a0e0e42db07204474f127216879f5b13ccca51c1a965173")
        XCTAssertEqual(PairingCode("AAAQEAYEAUDAOCAJBIFQYDIOB4"), code)
    }

    func testOnlyAWellFormedCodeIsAccepted() {
        XCTAssertNil(PairingCode(""))
        XCTAssertNil(PairingCode("aaaqeayeaudaocajbifqydiob"))
        XCTAssertNil(PairingCode("aaaqeayeaudaocajbifqydiob44"))
        XCTAssertNil(PairingCode("aaaqeayeaudaocajbifqydiob1"))
        XCTAssertNil(PairingCode("aaaqeayeaudaocajbifqydiob-"))
        // The last character holds 2 bits that must be zero.
        XCTAssertNil(PairingCode("aaaqeayeaudaocajbifqydiob5"))
    }

    func testTheSealedSetupOpensOnlyWithTheSameCode() throws {
        let code = try XCTUnwrap(PairingCode("aaaqeayeaudaocajbifqydiob4"))
        let other = try XCTUnwrap(PairingCode("aaaqeayeaudaocajbifqydioba"))
        let setup = Data(#"{"version":2}"#.utf8)
        let sealed = try code.seal(setup)
        XCTAssertEqual(sealed.count, 12 + setup.count + 16)
        XCTAssertNotEqual(try code.seal(setup), sealed)
        XCTAssertEqual(try code.open(sealed), setup)
        XCTAssertThrowsError(try other.open(sealed))
        // The box ID is the additional data: the same key with another box ID fails.
        let key = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: code.bytes), salt: Data(),
                                         info: Data("coconut rendezvous key".utf8), outputByteCount: 32)
        XCTAssertEqual(try ChaChaPoly.open(ChaChaPoly.SealedBox(combined: sealed), using: key, authenticating: code.boxID), setup)
        XCTAssertThrowsError(try ChaChaPoly.open(ChaChaPoly.SealedBox(combined: sealed), using: key, authenticating: Data()))
    }

    func testTheRendezvousRequestGoesToTheBoxOfTheCode() throws {
        let code = try XCTUnwrap(PairingCode("aaaqeayeaudaocajbifqydiob4"))
        let request = Rendezvous.request(relay: URL(string: "https://relay.example.com")!, code: code, sealed: Data([1, 2]))
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString,
                       "https://relay.example.com/v1/rendezvous/1d228ab884d178483a0e0e42db07204474f127216879f5b13ccca51c1a965173/message")
        XCTAssertEqual(request.httpBody, Data([1, 2]))
        XCTAssertNil(Rendezvous.failure(status: 204))
        XCTAssertEqual(Rendezvous.failure(status: 404), .expired)
        XCTAssertEqual(Rendezvous.failure(status: 409), .used)
        XCTAssertEqual(Rendezvous.failure(status: 429), .busy)
        XCTAssertEqual(Rendezvous.failure(status: 500), .relay(500))
    }

    func testThePairingCheckReadsTheSecretThatTheServiceMarked() throws {
        let install = Install(root: home)
        XCTAssertNil(pairingSecretUsed(install: install, secret: "a"))
        try FileManager.default.createDirectory(at: install.data, withIntermediateDirectories: true)
        let sqlite = Process()
        sqlite.executableURL = URL(fileURLWithPath: "/usr/bin/sqlite3")
        // The service stores the SHA-256 of the secret (backend/coconut/service.py).
        let used = SHA256.hash(data: Data("used".utf8)).map { String(format: "%02x", $0) }.joined()
        let open = SHA256.hash(data: Data("open".utf8)).map { String(format: "%02x", $0) }.joined()
        sqlite.arguments = [install.data.appendingPathComponent("coconut.sqlite3").path,
                            "CREATE TABLE pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);" +
                            "INSERT INTO pairing VALUES ('\(used)', 0, 1), ('\(open)', 0, 0);"]
        try sqlite.run()
        sqlite.waitUntilExit()
        XCTAssertEqual(pairingSecretUsed(install: install, secret: "used"), true)
        XCTAssertEqual(pairingSecretUsed(install: install, secret: "open"), false)
        XCTAssertEqual(pairingSecretUsed(install: install, secret: "other"), false)
    }

    func testThePairingSetupPrintsTheSetupAsJSON() throws {
        let python = URL(fileURLWithPath: "/usr/bin/env")
        try write("""
            def local_payload():
                return {'version': 2, 'relay': 'https://relay.example.com', 'expires': 5}
            """, to: "CoconutSetup/scripts/pairing.py")
        let launch = Launch.pairingSetup(install: Install(root: home.appendingPathComponent("CoconutSetup")), python: python,
                                         environment: [:])
        let process = Process()
        process.executableURL = python
        process.arguments = ["python3"] + launch.arguments.dropFirst()
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(fields["relay"] as? String, "https://relay.example.com")
        XCTAssertEqual(fields["expires"] as? Int, 5)
    }
}


/// Stand-ins for the ServiceManagement services of the login items. Each records its calls.
final class FakeLoginItems {
    var statuses: [LoginItem: SMAppService.Status]
    var calls: [String] = []
    var failing: Set<LoginItem> = []

    init(_ statuses: [LoginItem: SMAppService.Status]) {
        self.statuses = statuses
    }

    var items: LoginItems { LoginItems { item in Service(owner: self, item: item) } }

    struct Failure: LocalizedError {
        var errorDescription: String? { "Operation not permitted" }
    }

    struct Service: LoginItemService {
        let owner: FakeLoginItems
        let item: LoginItem

        var status: SMAppService.Status { owner.statuses[item] ?? .notFound }

        func register() throws {
            owner.calls.append("register \(item.name)")
            if owner.failing.contains(item) { throw Failure() }
            // Approval stays pending until the person allows the item.
            if owner.statuses[item] != .requiresApproval { owner.statuses[item] = .enabled }
        }

        func unregister() throws {
            owner.calls.append("unregister \(item.name)")
            if owner.failing.contains(item) { throw Failure() }
            owner.statuses[item] = .notRegistered
        }
    }
}
