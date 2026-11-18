import AppKit
import CoconutKit
import CoreImage.CIFilterBuiltins
import ServiceManagement
import SwiftUI

@main
struct CoconutApp: App {
    @StateObject private var model: AppModel
    @StateObject private var updater = Updater()
    // The chat client: the same state as the iPhone app. A Mac app keeps running without a window, so it keeps its
    // connection to the paired Mac and shows each new card at once.
    @State private var store: AppStore
    @State private var banners: MacBanners

    init() {
        let model = AppModel()
        let store = AppStore()
        let banners = MacBanners(model: model, store: store)
        // The chat client starts with the app, not with a view: the app can start without its main window, and the
        // banners need the lists of the teams also then. A team-only Mac has no pairing, so its chat client has
        // nothing to connect to.
        if model.mode == .full && !TestIsolation.active {
            banners.start()
            store.start()
        }
        _model = StateObject(wrappedValue: model)
        _store = State(initialValue: store)
        _banners = State(initialValue: banners)
    }

    var body: some Scene {
        Window("Coconut", id: "main") {
            MacMainWindow(model: model)
                // The share extension lists the chats that the app wrote for it, and the app sends each share that
                // the extension left (MacShareInbox).
                .modifier(ShareDirectoryPublisher())
                .modifier(MacShareDelivery(model: model))
                .environment(store)
                // Controls take the coconut brown of the app, never black (BoardStyle.control).
                .tint(BoardStyle.control)
                .modifier(BannerWindowOpener(banners: banners))
                .onOpenURL { url in
                    // A team-only Mac pairs no iPhone, and it joins a team with coconut team join.
                    guard model.mode == .full else { return }
                    // A Hermes agent sends this link. Coconut shows the Mac name before it pairs.
                    if PairingSetup.isLink(url) { store.setupLink = url.absoluteString }
                    // An invite link of a team opens the join sheet. The link stays in memory only.
                    if TeamLinkCheck.isLink(url) { store.teamJoin = TeamJoinRequest(link: url.absoluteString) }
                    // The share extension wrote a share and brought the app to the front.
                    if ShareInbox.isWake(url) { Task { await MacShareInbox.deliver(store: store, model: model) } }
                }
        }
        .defaultSize(width: 1100, height: 760)
        // Nobody sits at a team-only Mac, so its app is a menu bar item only: the window does not open at launch.
        // .automatic is the behavior of a scene without this modifier, so a full install opens it as before.
        .defaultLaunchBehavior(model.mode == .team ? .suppressed : .automatic)
        // Coconut has no settings window. Coconut > Settings shows Profile in the main window: the things of this
        // device and the defaults of the agents. The Coconut chat changes the rest.
        .commands {
            CommandGroup(replacing: .appSettings) { ProfileCommand(model: model) }
            // File > New chat (Command-N): the message box with a project chosen.
            CommandGroup(replacing: .newItem) { NewChatCommands(model: model) }
            // Show all chats, with Command-Shift-\ in the View menu.
            ChatDeckCommands()
            // View > Mark all as read, with Shift-Esc as in Slack.
            CommandGroup(after: .sidebar) { MarkAllReadCommand(model: model, store: store) }
        }

        MenuBarExtra {
            MenuContent(model: model, updater: updater)
        } label: {
            // The label shows while the app runs, also without a window, so it gives the banners the action that opens
            // the main window.
            Image(nsImage: model.status.service == .running ? MenuBarIcon.running : MenuBarIcon.stopped)
                .modifier(BannerWindowOpener(banners: banners))
                .modifier(DockBadge(store: store, enabled: model.mode == .full && !TestIsolation.active))
        }
        .menuBarExtraStyle(.menu)

        Window("Pair iPhone", id: "pair") {
            PairingView(model: model)
        }
        .windowResizability(.contentSize)
    }
}

/// File > New chat. It brings the main window to the front and asks it for the start (the window
/// knows which chat it shows).
struct NewChatCommands: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if model.mode == .full {
            Button("New chat") {
                model.newChatRequested = true
                show()
            }
            .keyboardShortcut("n", modifiers: .command)
        }
    }

    private func show() {
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// View > Mark all as read, with the shortcut of Slack (Shift-Esc): every chat is read through its newest message
/// (AppStore.markAllRead). A Mac for a team only has no chats of its own.
struct MarkAllReadCommand: View {
    @ObservedObject var model: AppModel
    let store: AppStore

    var body: some View {
        if model.mode == .full {
            Button("Mark all as read") { Task { await store.markAllRead() } }
                .keyboardShortcut(.escape, modifiers: .shift)
        }
    }
}

/// Coconut > Settings, with Command-comma. It brings the main window to the front on You. A Mac that runs Coconut
/// for a team only has no such page: its window shows the note of that mode.
struct ProfileCommand: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button("Settings…") {
            model.shownProfile = true
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut(",", modifiers: .command)
    }
}

struct MenuContent: View {
    @ObservedObject var model: AppModel
    @ObservedObject var updater: Updater
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if model.mode == .team {
            // A team-only Mac has no relay, no phone and no Hermes of Coconut.
            Text("Mode: team only")
        }
        Text("Mac: \(Self.describe(model.status.service))")
        if model.mode == .full {
            Text("Relay: \(model.status.relay.replacingOccurrences(of: "_", with: " "))")
            Text("Hermes: \(Self.describe(hermes: model.status))")
        }
        if let activity = model.activity {
            Text(activity)
        }
        if let error = model.updateError {
            Text(error)
        }
        // A Mac for a team is set up with Coconut.app --set-up --team-only, never with a click.
        if model.mode == .full {
            if !model.legacyAgents.isEmpty {
                Text("Runs from the earlier LaunchAgent")
                Button("Move Coconut to This App") { Task { await model.moveToApp() } }
                    .disabled(model.activity != nil || model.status.service == .notInstalled)
            } else if model.status.service == .notInstalled {
                Button("Set Up Coconut") { Task { await model.setUp() } }
                    .disabled(model.activity != nil)
            }
        }
        if let error = model.loginError {
            Text(error)
        }
        if model.startAtLogin && model.legacyAgents.isEmpty && model.status.service != .notInstalled {
            // The status of each login item that does not run at login yet: the app and its two agents.
            ForEach(model.mode.loginItems.filter { model.loginItems[$0].map { $0 != .enabled } ?? false }, id: \.self) { item in
                Text("Login item \(item.name): \(Self.describe(model.loginItems[item] ?? .notFound))")
            }
        }
        if model.needsApproval {
            Button("Allow Coconut in Login Items…") { model.openLoginItemsSettings() }
        }
        Divider()
        if model.mode == .full {
            Button("Open Coconut") {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        if model.mode == .full {
            Button("Pair iPhone…") {
                openWindow(id: "pair")
                NSApp.activate(ignoringOtherApps: true)
            }
            .disabled(model.status.service == .notInstalled)
        }
        Toggle("Start at Login", isOn: Binding(get: { model.startAtLogin }, set: { model.setStartAtLogin($0) }))
            .disabled(!model.legacyAgents.isEmpty)
        Button("Open Logs") { model.openLogs() }
        Divider()
        Text("Coconut \(model.version)")
        if let result = updater.lastResult, result.outcome == .failed {
            Text("Last update error: \(result.message ?? "unknown")")
        }
        Button("Check for Updates…") { updater.checkForUpdates() }
            .disabled(!updater.canCheckForUpdates)
        Toggle("Automatic Updates", isOn: Binding(get: { updater.automaticUpdates }, set: { updater.setAutomaticUpdates($0) }))
        Button("Quit Coconut") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    static func describe(_ service: Status.Service) -> String {
        switch service {
        case .notInstalled: return "not installed"
        case .stopped: return "stopped"
        case .starting: return "starting"
        case .running: return "running"
        }
    }

    static func describe(_ status: SMAppService.Status) -> String {
        switch status {
        case .enabled: return "on"
        case .requiresApproval: return "waits for approval"
        case .notRegistered: return "off"
        case .notFound: return "not found"
        @unknown default: return "unknown"
        }
    }

    static func describe(hermes status: Status) -> String {
        if status.hermes == "ok" {
            return status.hermesVersion ?? "ready"
        }
        return status.hermesReason ?? status.hermes
    }
}

struct PairingView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 16) {
            if let pairing = model.pairing {
                if let image = Self.qrImage(pairing.link) {
                    Image(nsImage: image)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: 240, height: 240)
                        .accessibilityLabel("QR code of the setup link")
                }
                Text("Scan the code in Coconut on the iPhone, or open the link on the iPhone.")
                    .multilineTextAlignment(.center)
                Text(pairing.link)
                    .font(.footnote.monospaced())
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                HStack {
                    Button("Copy link") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(pairing.link, forType: .string)
                    }
                    Button("New code") { Task { await model.pair() } }
                }
                Text("The code works once and expires after ten minutes. Send it only to the person who pairs.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            } else if model.isPairing {
                ProgressView("Creating a setup code…")
            } else {
                Text(model.pairingError ?? "Create a one-time setup code for an iPhone.")
                    .multilineTextAlignment(.center)
                Button("Create code") { Task { await model.pair() } }
            }
        }
        .padding(24)
        .frame(width: 360)
        .task { if model.pairing == nil { await model.pair() } }
    }

    static func qrImage(_ text: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else { return nil }
        let representation = NSCIImageRep(ciImage: output)
        let image = NSImage(size: representation.size)
        image.addRepresentation(representation)
        return image
    }
}
