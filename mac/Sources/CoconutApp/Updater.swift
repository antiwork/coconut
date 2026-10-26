import AppKit
import CoconutKit
import Foundation
import Sparkle

/// Sparkle with the feed of Info.plist (SUFeedURL) and the channel of the menu.
///
/// Sparkle replaces only the app. The service runs from the install folder, so it keeps running. After the relaunch,
/// AppModel.start runs `coconut update --if-needed`, which lets the running turns finish before it changes the service.
@MainActor
final class Updater: NSObject, ObservableObject, SPUUpdaterDelegate {
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var channel = UpdateChannel.current()

    private var controller: SPUStandardUpdaterController!
    private var observation: NSKeyValueObservation?

    override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            let value = updater.canCheckForUpdates
            Task { @MainActor in self?.canCheckForUpdates = value }
        }
    }

    func checkForUpdates() {
        NSApp.activate(ignoringOtherApps: true)
        controller.checkForUpdates(nil)
    }

    func setChannel(_ value: UpdateChannel) {
        UserDefaults.standard.set(value.rawValue, forKey: UpdateChannel.preferenceKey)
        channel = value
        // A new channel can offer another build at once.
        controller.updater.checkForUpdatesInBackground()
    }

    nonisolated func allowedChannels(for updater: SPUUpdater) -> Set<String> {
        UpdateChannel.current().allowedChannels
    }

    /// A menu bar app seldom quits, so an update that waits for the quit would wait for days. Install it now.
    nonisolated func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem,
                             immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        immediateInstallHandler()
        return true
    }
}
