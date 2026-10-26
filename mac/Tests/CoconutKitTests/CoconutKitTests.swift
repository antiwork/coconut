import CryptoKit
@testable import CoconutKit
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

    func testThePairingLinkSaysHowLongEachKindOfCodeLasts() throws {
        let python = URL(fileURLWithPath: "/usr/bin/env")
        // Each case has its own folder: Python would reuse the cached module of a file with the same size and time.
        for (version, minutes) in [(2, "ten"), (1, "five")] {
            try write("""
                def local_payload():
                    return {'version': \(version)}
                def link(payload):
                    return 'coconut://pair/abc?setup=x'
                """, to: "Coconut\(version)/scripts/pairing.py")
            let launch = Launch.pairingLink(install: Install(root: home.appendingPathComponent("Coconut\(version)")),
                                            python: python, environment: [:])
            let process = Process()
            process.executableURL = python
            process.arguments = ["python3"] + launch.arguments.dropFirst()
            let output = Pipe()
            process.standardOutput = output
            try process.run()
            let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            process.waitUntilExit()
            XCTAssertEqual(process.terminationStatus, 0)
            XCTAssertEqual(PairingLink(output: text)?.link, "coconut://pair/abc?setup=x")
            XCTAssertTrue(text.contains("expires after \(minutes) minutes"), text)
        }
    }

    func testTheLoginItemUsesThePathOfTheLoginShell() {
        XCTAssertEqual(LoginEnvironment.parse("motd\n\(LoginEnvironment.mark)/opt/homebrew/bin:/bin\(LoginEnvironment.mark)\n"),
                       "/opt/homebrew/bin:/bin")
        XCTAssertNil(LoginEnvironment.parse("no marks"))
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], loginPath: "/a:/b")["PATH"], "/a:/b")
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], loginPath: nil)["PATH"],
                       LoginEnvironment.fallbackPath)
        XCTAssertEqual(LoginEnvironment.make(base: ["PATH": "/mine"], loginPath: nil)["PATH"], "/mine")
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
        XCTAssertNil(DeployResult(output: "Traceback"))
    }

    // MARK: Update channel

    func testTheChannelIsStableUntilThePersonChoosesNightly() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "CoconutKitTests-\(UUID().uuidString)"))
        XCTAssertEqual(UpdateChannel.current(defaults), .stable)
        XCTAssertEqual(UpdateChannel.stable.allowedChannels, [], "Stable items have no channel, so every app sees them.")
        defaults.set("nightly", forKey: UpdateChannel.preferenceKey)
        XCTAssertEqual(UpdateChannel.current(defaults), .nightly)
        XCTAssertEqual(UpdateChannel.nightly.allowedChannels, ["nightly"])
        defaults.set("beta", forKey: UpdateChannel.preferenceKey)
        XCTAssertEqual(UpdateChannel.current(defaults), .stable)
    }

    // MARK: Pairing

    func testThePairingOutputGivesTheLinkAndTheImage() {
        let output = """
            Setup link: coconut://pair?setup=abc
            QR image: /Users/a/Coconut/.runtime/pairing/iphone-setup.png
            On the iPhone, tap the link, or scan the QR image in Coconut.
            """
        XCTAssertEqual(PairingLink(output: output),
                       PairingLink(link: "coconut://pair?setup=abc", image: "/Users/a/Coconut/.runtime/pairing/iphone-setup.png"))
        XCTAssertEqual(PairingLink(output: "Setup link: coconut://pair/mfrggzdf?setup=IHsi\n")?.link,
                       "coconut://pair/mfrggzdf?setup=IHsi")
        XCTAssertNil(PairingLink(output: "Coconut could not create the setup QR."))
        XCTAssertNil(PairingLink(output: "Setup link: https://example.com"))
    }
}
