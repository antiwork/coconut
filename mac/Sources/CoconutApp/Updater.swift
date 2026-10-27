import AppKit
import CoconutKit
import Foundation
import Sparkle

/// Sparkle with the feed of Info.plist (SUFeedURL) and the automatic updates of the preferences.
///
/// The feed has one channel: its items have no `sparkle:channel`, so Sparkle offers each item to every Mac. The updater
/// allows no channel.
///
/// Sparkle replaces only the app. The service runs from the install folder, so it keeps running. After the relaunch,
/// AppModel.start runs `coconut install-source --if-needed`, which lets the running turns finish before it changes the
/// service. The `coconut` tool changes the settings and asks for checks through the preferences and distributed
/// notifications, so a Hermes agent can manage the updates of a Mac that nobody watches.
@MainActor
final class Updater: NSObject, ObservableObject, SPUUpdaterDelegate {
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var automaticUpdates: Bool
    @Published private(set) var lastResult: UpdateResult?

    private let preferences = UpdatePreferences()
    private var controller: SPUStandardUpdaterController!
    private var observation: NSKeyValueObservation?
    /// True while a requested check uses automatic installs that the settings have off.
    private var installsForRequest = false

    override init() {
        // An earlier version kept an update channel. Sparkle does not need it any more.
        preferences.removeLegacyChannel()
        automaticUpdates = preferences.automaticUpdates
        lastResult = preferences.lastResult
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            let value = updater.canCheckForUpdates
            Task { @MainActor in self?.canCheckForUpdates = value }
        }
        let center = DistributedNotificationCenter.default()
        center.addObserver(forName: .init(UpdatePreferences.settingsChanged), object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.applySettings() }
        }
        center.addObserver(forName: .init(UpdatePreferences.checkRequested), object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.checkForRequest() }
        }
        // `coconut update` can ask before the app runs. Then `open` starts the app, and the request waits here.
        checkForRequest()
    }

    func checkForUpdates() {
        NSApp.activate(ignoringOtherApps: true)
        controller.checkForUpdates(nil)
    }

    func setAutomaticUpdates(_ value: Bool) {
        preferences.setAutomaticUpdates(value)
        applySettings()
    }

    /// Read the settings that the menu or the `coconut` tool changed.
    func applySettings() {
        let updater = controller.updater
        let automatic = preferences.automaticUpdates
        updater.automaticallyChecksForUpdates = automatic
        updater.automaticallyDownloadsUpdates = automatic
        automaticUpdates = automatic
        updater.resetUpdateCycle()
    }

    /// Answer a request of `coconut update`: check now, and install a newer build without a question.
    func checkForRequest() {
        guard preferences.pendingRequest != nil else { return }
        let updater = controller.updater
        if !updater.automaticallyDownloadsUpdates {
            installsForRequest = true
            updater.automaticallyDownloadsUpdates = true
        }
        updater.checkForUpdatesInBackground()
    }

    private func record(_ outcome: UpdateResult.Outcome, version: String? = nil, message: String? = nil) {
        let result = UpdateResult(time: Date(), outcome: outcome, version: version, message: message)
        preferences.record(result)
        lastResult = result
    }

    private func endRequest() {
        if installsForRequest {
            installsForRequest = false
            controller.updater.automaticallyDownloadsUpdates = preferences.automaticUpdates
        }
    }

    nonisolated func updaterDidNotFindUpdate(_ updater: SPUUpdater, error: any Error) {
        Task { @MainActor in self.record(.upToDate) }
    }

    nonisolated func updater(_ updater: SPUUpdater, didAbortWithError error: any Error) {
        let nsError = error as NSError
        // Sparkle reports "no update" as an error too. updaterDidNotFindUpdate records it.
        guard !(nsError.domain == SUSparkleErrorDomain && nsError.code == Int(SUError.noUpdateError.rawValue)) else { return }
        let message = error.localizedDescription
        Task { @MainActor in self.record(.failed, message: message) }
    }

    nonisolated func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: (any Error)?) {
        Task { @MainActor in self.endRequest() }
    }

    /// A menu bar app seldom quits, so an update that waits for the quit would wait for days. Install it now.
    nonisolated func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem,
                             immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        let version = "\(item.displayVersionString) (\(item.versionString))"
        Task { @MainActor in
            self.record(.installing, version: version)
            self.endRequest()
            immediateInstallHandler()
        }
        return true
    }
}
