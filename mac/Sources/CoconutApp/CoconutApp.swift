import AppKit
import CoconutKit
import CoreImage.CIFilterBuiltins
import SwiftUI

@main
struct CoconutApp: App {
    @StateObject private var model = AppModel()
    @StateObject private var updater = Updater()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model, updater: updater)
        } label: {
            Image(systemName: model.status.service == .running ? "network" : "network.slash")
        }
        .menuBarExtraStyle(.menu)

        Window("Pair iPhone", id: "pair") {
            PairingView(model: model)
        }
        .windowResizability(.contentSize)
    }
}

struct MenuContent: View {
    @ObservedObject var model: AppModel
    @ObservedObject var updater: Updater
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text("Mac: \(Self.describe(model.status.service))")
        Text("Relay: \(model.status.relay.replacingOccurrences(of: "_", with: " "))")
        Text("Hermes: \(Self.describe(hermes: model.status))")
        if let activity = model.activity {
            Text(activity)
        }
        if let error = model.updateError {
            Text(error)
        }
        if !model.legacyAgents.isEmpty {
            Text("Runs from the earlier LaunchAgent")
            Button("Move Coconut to This App") { Task { await model.moveToApp() } }
                .disabled(model.activity != nil || model.status.service == .notInstalled)
        } else if model.status.service == .notInstalled {
            Button("Set Up Coconut") { Task { await model.setUp() } }
                .disabled(model.activity != nil)
        }
        if let error = model.loginError {
            Text(error)
        }
        if model.needsApproval {
            Button("Allow Coconut in Login Items…") { model.openLoginItemsSettings() }
        }
        Divider()
        Button("Pair iPhone…") {
            openWindow(id: "pair")
            NSApp.activate(ignoringOtherApps: true)
        }
        .disabled(model.status.service == .notInstalled)
        Toggle("Start at Login", isOn: Binding(get: { model.startAtLogin }, set: { model.setStartAtLogin($0) }))
            .disabled(!model.legacyAgents.isEmpty)
        Button("Open Logs") { model.openLogs() }
        Divider()
        Text("Coconut \(model.version)")
        Button("Check for Updates…") { updater.checkForUpdates() }
            .disabled(!updater.canCheckForUpdates)
        Picker("Update Channel", selection: Binding(get: { updater.channel }, set: { updater.setChannel($0) })) {
            ForEach(UpdateChannel.allCases, id: \.self) { Text($0.title).tag($0) }
        }
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
                    .font(.caption.monospaced())
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                HStack {
                    Button("Copy Link") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(pairing.link, forType: .string)
                    }
                    Button("New Code") { Task { await model.pair() } }
                }
                Text("The code works once and expires after ten minutes. Send it only to the person who pairs.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            } else if model.isPairing {
                ProgressView("Creating a setup code…")
            } else {
                Text(model.pairingError ?? "Create a one-time setup code for an iPhone.")
                    .multilineTextAlignment(.center)
                Button("Create Code") { Task { await model.pair() } }
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
