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
        Task { await refresh() }
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
    func registerIfNeeded() {
        legacyAgents = LegacyAgents.present()
        guard startAtLogin, legacyAgents.isEmpty, install.isInstalled() else { return }
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

    /// Ask the service for a new one-time setup link, with `coconut pair`.
    func pair() async {
        guard !isPairing else { return }
        isPairing = true
        pairingError = nil
        defer { isPairing = false }
        let tool = tool
        let result = await Task.detached { () -> (Int32, String) in
            let process = Process()
            process.executableURL = tool
            process.arguments = ["pair"]
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
        if result.0 == 0, let link = PairingLink(output: result.1) {
            pairing = link
        } else {
            pairing = nil
            pairingError = result.1.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "Coconut could not create a setup link." : result.1.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }
}
