import AppKit
import CoconutKit
import Foundation
import ServiceManagement

/// The state of the menu bar: the status of this Mac, the login items and the pairing link.
@MainActor
final class AppModel: ObservableObject {
    static let startAtLoginKey = "startAtLogin"

    @Published private(set) var status: Status
    @Published private(set) var loginItems: [Agent: SMAppService.Status] = [:]
    @Published private(set) var legacyAgents: [String] = []
    @Published private(set) var loginError: String?
    @Published private(set) var pairing: PairingLink?
    @Published private(set) var pairingError: String?
    @Published private(set) var isPairing = false
    /// The install, update or move that runs now, with the text that the menu shows for it.
    @Published private(set) var activity: String?
    @Published private(set) var updateError: String?

    // The login items start without the environment of the app, so the app ignores COCONUT_INSTALL_DIR too. Both
    // read the same preference.
    let install = Install.standard(environment: [:])
    private var timer: Timer?
    private var couldRegister = false

    init() {
        UserDefaults.standard.register(defaults: [Self.startAtLoginKey: true])
        status = StatusReader(install: install).read(healthy: false)
        // SwiftUI gives the label of a menu bar extra no dependable task, so the model starts itself.
        start()
    }

    var tool: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/coconut") }

    var version: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "\(info["CFBundleShortVersionString"] as? String ?? "development") (\(info["CFBundleVersion"] as? String ?? "0"))"
    }

    var startAtLogin: Bool { UserDefaults.standard.bool(forKey: Self.startAtLoginKey) }

    var needsApproval: Bool { loginItems.values.contains(.requiresApproval) }

    func start() {
        guard timer == nil else { return }
        registerIfNeeded()
        Task {
            await refresh()
            // After Sparkle installs a new app, the new app updates the source that it carries.
            if legacyAgents.isEmpty && install.isInstalled() {
                await update(ifNeeded: true)
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { await self?.refresh() }
        }
    }

    func refresh() async {
        let healthy = await StatusReader.checkHealth()
        // The timer runs every five seconds. Each assignment redraws the menu, so only a change is assigned.
        let status = StatusReader(install: install).read(healthy: healthy)
        if status != self.status { self.status = status }
        let legacyAgents = LegacyAgents.present()
        if legacyAgents != self.legacyAgents { self.legacyAgents = legacyAgents }
        // Coconut can be installed, or the earlier LaunchAgents removed, while the app runs. Register then.
        let canRegister = legacyAgents.isEmpty && status.service != .notInstalled
        if canRegister && !couldRegister { registerIfNeeded() }
        couldRegister = canRegister
        let loginItems = Dictionary(uniqueKeysWithValues: Agent.allCases.map { ($0, SMAppService.agent(plistName: $0.plistName).status) })
        if loginItems != self.loginItems { self.loginItems = loginItems }
    }

    /// Register both login items when the person wants them, Coconut is installed and no old LaunchAgent runs it.
    /// A first install registers them before the source exists: their tool waits, and the install starts it.
    func registerIfNeeded(beforeInstall: Bool = false) {
        legacyAgents = LegacyAgents.present()
        guard startAtLogin, legacyAgents.isEmpty, beforeInstall || install.isInstalled() else { return }
        loginError = nil
        for agent in Agent.allCases {
            let service = SMAppService.agent(plistName: agent.plistName)
            guard service.status != .enabled else { continue }
            do {
                try service.register()
            } catch {
                loginError = "Coconut could not add its login item \(agent.rawValue): \(error.localizedDescription)"
            }
        }
    }

    func setStartAtLogin(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: Self.startAtLoginKey)
        if value {
            registerIfNeeded()
        } else {
            loginError = nil
            // A login item that waits for approval is registered too. Approval would start it later.
            for agent in Agent.allCases where [.enabled, .requiresApproval].contains(SMAppService.agent(plistName: agent.plistName).status) {
                do {
                    try SMAppService.agent(plistName: agent.plistName).unregister()
                } catch {
                    loginError = "Coconut could not remove its login item \(agent.rawValue): \(error.localizedDescription)"
                }
            }
        }
        Task { await refresh() }
    }

    func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }

    func openLogs() {
        NSWorkspace.shared.open(install.logs)
    }

    /// Install Coconut in the install folder, with the source of this app.
    /// The service of the app runs only as its login item, so a first install turns on Start at Login. The install
    /// starts the login item, which must be registered and allowed first.
    func setUp() async {
        guard activity == nil else { return }
        UserDefaults.standard.set(true, forKey: Self.startAtLoginKey)
        registerIfNeeded(beforeInstall: true)
        await refresh()
        guard loginItems[.service] == .enabled else {
            updateError = needsApproval
                ? "Allow Coconut in Login Items, then choose Set Up Coconut again."
                : loginError ?? "Coconut could not add its login items."
            return
        }
        await update(ifNeeded: false)
    }

    /// Install the source of this app when it is newer. deploy.py lets the running turns finish first.
    /// Return whether the install or the update succeeded.
    @discardableResult
    func update(ifNeeded: Bool) async -> Bool {
        guard activity == nil else { return false }
        activity = install.isInstalled() ? "Updating Coconut. Running work finishes first." : "Installing Coconut…"
        updateError = nil
        let result = await runTool(ifNeeded ? ["update", "--if-needed"] : ["update"])
        activity = nil
        if result.0 != 0 {
            updateError = DeployResult(output: result.1)?.error ?? Self.lastLine(result.1) ?? "The update failed."
        } else if let relayError = DeployResult(output: result.1)?.relayError {
            // The service runs, but without the relay the iPhone cannot reach it.
            updateError = "The relay did not start: \(relayError)"
        }
        await refresh()
        return result.0 == 0
    }

    /// Hand the service from the earlier LaunchAgents to the login items of this app.
    ///
    /// The earlier source has no migration command, so the source of this app goes in first, under the earlier
    /// LaunchAgent: deploy.py drains, stops it, replaces the source and starts it again. Then the new host.py moves it.
    func moveToApp() async {
        guard activity == nil, await update(ifNeeded: true) else { return }
        activity = "Moving Coconut to this app. Running work finishes first."
        let result = await runTool(["migrate"])
        activity = nil
        guard result.0 == 0 else {
            updateError = Self.lastLine(result.1) ?? "Coconut could not move to this app."
            return
        }
        // The update ran under the earlier LaunchAgent, so the relay login item did not exist yet. It starts now.
        updateError = nil
        UserDefaults.standard.set(true, forKey: Self.startAtLoginKey)
        registerIfNeeded()
        await refresh()
    }

    static func lastLine(_ text: String) -> String? {
        text.split(whereSeparator: \.isNewline).last.map(String.init)
    }

    /// Ask the service for a new one-time setup link, with `coconut pair`.
    func pair() async {
        guard !isPairing else { return }
        isPairing = true
        pairingError = nil
        defer { isPairing = false }
        let result = await runTool(["pair"])
        if result.0 == 0, let link = PairingLink(output: result.1) {
            pairing = link
        } else {
            pairing = nil
            pairingError = result.1.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "Coconut could not create a setup link." : result.1.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    /// Run the `coconut` tool of this bundle. Return its exit code and its output.
    func runTool(_ arguments: [String]) async -> (Int32, String) {
        let tool = tool
        // The tool must use the folder that the menu shows, also when the app inherited another COCONUT_INSTALL_DIR.
        var environment = ProcessInfo.processInfo.environment
        environment["COCONUT_INSTALL_DIR"] = install.root.path
        return await Task.detached { () -> (Int32, String) in
            let process = Process()
            process.executableURL = tool
            process.arguments = arguments
            process.environment = environment
            let output = Pipe()
            process.standardOutput = output
            process.standardError = output
            do {
                try process.run()
            } catch {
                return (-1, error.localizedDescription)
            }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (process.terminationStatus, String(decoding: data, as: UTF8.self))
        }.value
    }
}
