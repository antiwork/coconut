import AppKit
import CoconutKit
import Foundation
import ServiceManagement

/// The state of the menu bar: the status of this Mac, the login items and the pairing link.
@MainActor
final class AppModel: ObservableObject {
    static let startAtLoginKey = "startAtLogin"

    @Published private(set) var status: Status
    @Published private(set) var loginItems: [LoginItem: SMAppService.Status] = [:]
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
    private var loginItemReads = LatestRead()

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
            // The install script (updates/install.sh) opens the app with --set-up, so the person needs no click.
            if CommandLine.arguments.contains("--set-up") && legacyAgents.isEmpty && status.service != .running {
                await setUp()
            } else if legacyAgents.isEmpty && install.isInstalled() && !install.isCheckout() {
                // After Sparkle installs a new app, the new app updates the source that it carries.
                await update(ifNeeded: true)
            } else if !install.isInstalled() && UpdatePreferences().pendingInstall != nil {
                // No service to update: the new app is the whole update. While an earlier LaunchAgent runs the old
                // source, the install stays pending.
                UpdatePreferences().record(UpdateResult(time: Date(), outcome: .installed, version: version))
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
        // Each status is a request to launchd. On the main thread the three of them held the app for about 95 ms every
        // five seconds, also while the person clicked in a chat.
        let read = loginItemReads.start()
        let loginItems = await Task.detached(priority: .utility) { LoginItems().statuses() }.value
        // A timer refresh can overlap a change of "Start at Login". Its older read must not replace the newer status.
        guard loginItemReads.isLatest(read) else { return }
        if loginItems != self.loginItems { self.loginItems = loginItems }
    }

    /// Register the login items (the app and its two agents) when the person wants them, Coconut is installed and no
    /// old LaunchAgent runs it. Each launch registers them again when one is missing.
    /// A first install registers them before the source exists: the tool of the agents waits, and the install starts it.
    func registerIfNeeded(beforeInstall: Bool = false) {
        legacyAgents = LegacyAgents.present()
        guard startAtLogin, legacyAgents.isEmpty, beforeInstall || install.isInstalled() else { return }
        loginError = LoginItems().register()
    }

    func setStartAtLogin(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: Self.startAtLoginKey)
        if value {
            registerIfNeeded()
        } else {
            // The app and its agents go together. A login item that waits for approval is registered too.
            loginError = LoginItems().unregister()
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
        guard loginItems[.agent(.service)] == .enabled else {
            updateError = needsApproval
                ? "Allow Coconut in Login Items, then choose Set Up Coconut again."
                : loginError ?? "Coconut could not add its login items."
            return
        }
        await update(ifNeeded: false, setUp: true)
    }

    /// Install the source of this app when it is newer. deploy.py lets the running turns finish first.
    /// Return whether the install or the update succeeded.
    @discardableResult
    func update(ifNeeded: Bool, setUp: Bool = false) async -> Bool {
        guard activity == nil else { return false }
        activity = install.isInstalled() ? "Updating Coconut. Running work finishes first." : "Installing Coconut…"
        updateError = nil
        // Only a setup that the person asked for changes the Hermes config (scripts/hermes_skills.py).
        let result = await runTool(ifNeeded ? ["install-source", "--if-needed"] : ["install-source"] + (setUp ? ["--set-up"] : []))
        activity = nil
        let preferences = UpdatePreferences()
        if result.0 != 0 {
            updateError = DeployResult(output: result.1)?.error ?? Self.lastLine(result.1) ?? "The update failed."
            // `coconut update --wait` and `coconut status` report this step after an app update.
            preferences.record(UpdateResult(time: Date(), outcome: .failed,
                                            message: "The service update failed: \(updateError ?? "")"))
        } else {
            if let relayError = DeployResult(output: result.1)?.relayError {
                // The service runs, but without the relay the iPhone cannot reach it.
                updateError = "The relay did not start: \(relayError)"
            }
            if preferences.pendingInstall != nil {
                // The new app runs, and the service has its source. The app update is complete.
                preferences.record(UpdateResult(time: Date(), outcome: .installed, version: version))
            }
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
