import SwiftUI

// The one onboarding screen, also "Pair a new Mac" in Settings. The person copies one message to their Hermes, for
// example in Telegram. Hermes installs Coconut on its Mac and runs `coconut pair <code>`, and this iPhone pairs by
// itself through the rendezvous box on the relay. Nobody touches the Mac.
struct OnboardingView: View {
    static let completedKey = "coconut.onboarding.completed"
    // The download link of Coconut.app for a manual install (updates/README.md).
    static let downloadURL = URL(string: "https://coconut-updates.gumroad.workers.dev/mac/download")!
    // A pairing screen on screen takes a tapped pairing link itself. PairingPages counts them.
    static var isOpen: Bool { PairingPages.shared.isOpen }

    private enum Phase: Equatable {
        // The iPhone makes the code and its box on the relay.
        case preparing
        // The message is ready. The iPhone waits for the Mac.
        case waiting
        case pairing(String)
        case failed(String)
    }

    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // The first run has no Close button, because Coconut shows nothing until it has a pairing.
    let firstRun: Bool
    @State private var phase = Phase.preparing
    @State private var pending: PendingPairing?
    // A new value starts the rendezvous again.
    @State private var attempt = 0
    @State private var copied = false
    @State private var helpOpen = false
    // A link from the help sheet waits until the sheet closes.
    @State private var helpPayload: String?
    // A setup from a QR code or a tapped link. The person confirms the Mac name before Coconut pairs.
    @State private var linkSetup: PairingSetup?
    @State private var confirmOpen = false
    // One pairing at a time: the rendezvous and a link could both arrive. Each success replaces the saved Mac.
    @State private var pairingNow = false
    // A closed screen starts no pairing, so a late setup cannot replace the Mac.
    @State private var closed = false
    // The pairing from a confirmed link. Closing the screen cancels it, as SwiftUI cancels the rendezvous task.
    @State private var linkPairing: Task<Void, Never>?

    init(firstRun: Bool = false) {
        self.firstRun = firstRun
    }

    static func showsOnLaunch(_ store: AppStore) -> Bool {
        #if DEBUG && targetEnvironment(simulator)
        // Test servers pair the simulator on launch.
        if ProcessInfo.processInfo.environment["COCONUT_SERVER_URL"] != nil { return false }
        #endif
        return !store.isDemo && !store.hasPairing && !UserDefaults.standard.bool(forKey: completedKey)
    }

    var body: some View {
        VStack(spacing: 0) {
            #if os(iOS)
            HStack {
                Spacer()
                Button { close() } label: {
                    Image(systemName: "xmark").font(.subheadline.weight(.semibold)).frame(minWidth: 44, minHeight: 44)
                }
                .foregroundStyle(.primary)
                .opacity(firstRun ? 0 : 1)
                .disabled(firstRun)
                .accessibilityLabel("Close")
            }
            .padding(.top, 4)
            #endif
            ScrollView {
                VStack(spacing: BoardStyle.Space.m) {
                    #if os(macOS)
                    // A Mac window centers a narrow column, as the setup pages of macOS do.
                    Image(nsImage: NSApplication.shared.applicationIconImage)
                        .resizable().frame(width: 72, height: 72)
                        .accessibilityHidden(true)
                    #endif
                    Text("Connect your Mac")
                        .font(.system(.title, design: .serif).weight(.medium))
                        .multilineTextAlignment(.center)
                        .accessibilityAddTraits(.isHeader)
                    Text("Send this message to your Hermes, for example in Telegram.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    if store.hasPairing && !firstRun {
                        Text("This replaces the pairing with \(store.pairedMacDescription).")
                            .font(.footnote).foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    message.padding(.top, BoardStyle.Space.m)
                    actions
                    status.padding(.top, BoardStyle.Space.m)
                }
                #if os(macOS)
                .frame(maxWidth: 440)
                .padding(.vertical, 32)
                .frame(maxWidth: .infinity)
                .containerRelativeFrame(.vertical, alignment: .center)
                #else
                .padding(.top, BoardStyle.Space.xl)
                .frame(maxWidth: .infinity)
                #endif
            }
            .scrollBounceBehavior(.basedOnSize)
            Button("Need help?") { helpOpen = true }
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(minHeight: 44)
                .accessibilityIdentifier("onboarding-help")
        }
        .padding(.horizontal, BoardStyle.Space.l)
        .padding(.bottom, BoardStyle.Space.s)
        .background(BoardStyle.paper.ignoresSafeArea())
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: phase)
        .task(id: attempt) { await rendezvous() }
        .onChange(of: store.hasPairing) { old, paired in
            // A first run ends with any pairing, for example from a test server.
            if paired && !old && firstRun { finish() }
        }
        .onChange(of: store.setupLink, initial: true) { _, _ in takeSetupLink() }
        // The confirmation belongs to this screen, so it opens only after the help sheet is gone.
        .sheet(isPresented: $helpOpen, onDismiss: {
            if let payload = helpPayload {
                helpPayload = nil
                useLink(payload)
            }
        }) {
            PairingHelpView { payload in
                helpPayload = payload
                helpOpen = false
            }
        }
        .confirmationDialog("Pair with \(linkSetup?.name ?? "this Mac")?", isPresented: $confirmOpen, titleVisibility: .visible) {
            Button("Pair") { if let linkSetup { linkPairing = Task { await pair(linkSetup) } } }
            Button("Cancel", role: .cancel) { linkSetup = nil }
        } message: {
            Text(store.hasPairing ? "This replaces the pairing with \(store.pairedMacDescription)." : "Check that this is your Mac.")
        }
        .onAppear {
            PairingPages.shared.opened()
            closed = false
        }
        .onDisappear {
            PairingPages.shared.closed()
            closed = true
            linkPairing?.cancel()
        }
        .accessibilityIdentifier("onboarding")
    }

    private var messageText: String { pending?.pairingCode.hermesMessage ?? PairingCode(bytes: Data(count: 16)).hermesMessage }

    private var message: some View {
        Text(messageText)
            .font(.body)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(BoardStyle.Space.l)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous))
            .redacted(reason: pending == nil ? .placeholder : [])
            .privacySensitive()
            .accessibilityIdentifier("hermes-message")
    }

    @ViewBuilder private var actions: some View {
        #if os(macOS)
        // Standard Mac buttons at their own size. Return copies the message.
        HStack(spacing: 10) {
            ShareLink(item: messageText) { Label("Share…", systemImage: "square.and.arrow.up") }
                .accessibilityIdentifier("share-hermes-message")
            Button {
                Pasteboard.copy(messageText)
                copied = true
            } label: {
                Label(copied ? "Copied" : "Copy Message", systemImage: copied ? "checkmark" : "doc.on.doc")
            }
            .buttonStyle(.borderedProminent)
            // The window tints its controls black and white. The one action of the page keeps the system blue.
            .tint(.accentColor)
            .keyboardShortcut(.defaultAction)
            .accessibilityIdentifier("copy-hermes-message")
        }
        .controlSize(.large)
        .frame(maxWidth: .infinity, alignment: .trailing)
        .disabled(pending == nil)
        #else
        HStack(spacing: 12) {
            Button {
                Pasteboard.copy(messageText)
                withAnimation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.8)) { copied = true }
            } label: {
                Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                    .font(.body.weight(.semibold))
                    .contentTransition(.symbolEffect(.replace))
                    .frame(maxWidth: .infinity, minHeight: 50)
                    .foregroundStyle(BoardStyle.controlLabel)
                    .background(BoardStyle.control, in: Capsule())
            }
            .buttonStyle(HomePressStyle())
            .accessibilityIdentifier("copy-hermes-message")
            ShareLink(item: messageText) {
                Label("Share", systemImage: "square.and.arrow.up")
                    .font(.body.weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 50)
                    .foregroundStyle(.primary)
                    .background(Color.primary.opacity(0.08), in: Capsule())
            }
            .accessibilityIdentifier("share-hermes-message")
        }
        .disabled(pending == nil)
        .opacity(pending == nil ? 0.4 : 1)
        #endif
    }

    @ViewBuilder private var status: some View {
        switch phase {
        case .preparing, .waiting:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(phase == .preparing ? "Making a pairing code…" : "Waiting for your Mac…")
            }
            .font(.subheadline).foregroundStyle(.secondary)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("onboarding-status")
        case .pairing(let name):
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Pairing with \(name)…")
            }
            .font(.subheadline.weight(.medium))
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("onboarding-status")
        case .failed(let text):
            VStack(spacing: BoardStyle.Space.s) {
                InlineError(text).multilineTextAlignment(.center)
                Button("Try again") { attempt += 1 }
                    .font(.subheadline.weight(.semibold))
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("onboarding-retry")
            }
            .accessibilityIdentifier("onboarding-error")
        }
    }

    // Make or resume the code, wait for the Mac's setup, then pair. An expired code gives way to a new one.
    private func rendezvous() async {
        guard let relay = Rendezvous.appRelay else {
            phase = .failed("This Coconut build has no relay address.")
            return
        }
        let box = Rendezvous(relay: relay)
        while !Task.isCancelled {
            let current = ProtectedCache.read(PendingPairing.cacheName).flatMap { (saved: PendingPairing) in saved.isLive() ? saved : nil }
                ?? PendingPairing.make()
            try? ProtectedCache.write(current, name: PendingPairing.cacheName)
            // The message shows only when its box exists, so a copied message always works.
            pending = nil
            phase = .preparing
            do {
                try await box.open(current)
            } catch RendezvousError.expired {
                ProtectedCache.remove(PendingPairing.cacheName)
                continue
            } catch RendezvousError.relay(let status) {
                phase = .failed("The relay refused the pairing code with status \(status). Try again, or tap Need help?.")
                return
            } catch {
                guard !Task.isCancelled else { return }
                phase = .failed("Coconut could not reach the relay. Check the internet connection, then try again.")
                return
            }
            pending = current
            phase = .waiting
            guard let sealed = await waitForSetup(box, current) else {
                // The code expired. The next pass makes a new one, so the message on screen always works.
                ProtectedCache.remove(PendingPairing.cacheName)
                copied = false
                continue
            }
            await box.close(current)
            ProtectedCache.remove(PendingPairing.cacheName)
            // The box is used. Copy and Share must not offer its code again, and Try again makes a new box.
            pending = nil
            copied = false
            // A link that the person confirmed pairs already.
            guard !pairingNow else { return }
            guard let plaintext = try? current.pairingCode.open(sealed),
                  let setup = try? PairingSetup.parse(String(decoding: plaintext, as: UTF8.self)) else {
                phase = .failed("The setup from the Mac is damaged. Send a new message to Hermes.")
                return
            }
            await pair(setup)
            return
        }
    }

    // Read the box until the setup comes or the code expires. A lost connection, for example while iOS suspends
    // Coconut, retries.
    private func waitForSetup(_ box: Rendezvous, _ current: PendingPairing) async -> Data? {
        while !Task.isCancelled && current.isLive() {
            let wait = max(1, min(25, Int(current.expires.timeIntervalSinceNow)))
            do {
                if let sealed = try await box.read(current, wait: wait) { return sealed }
            } catch RendezvousError.expired {
                return nil
            } catch {
                try? await Task.sleep(for: .seconds(2))
            }
        }
        return nil
    }

    private func pair(_ setup: PairingSetup) async {
        guard !pairingNow, !closed else { return }
        pairingNow = true
        defer { pairingNow = false }
        phase = .pairing(setup.name)
        linkSetup = nil
        if await store.pair(setup: setup) {
            finish()
        } else if Task.isCancelled {
            return
        } else {
            phase = .failed(store.error ?? "Coconut could not pair with \(setup.name). Send a new message to Hermes.")
        }
    }

    private func takeSetupLink() {
        guard let link = store.setupLink else { return }
        store.setupLink = nil
        useLink(link)
    }

    private func useLink(_ payload: String) {
        do {
            linkSetup = try PairingSetup.parse(payload)
            confirmOpen = true
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    // The code stops working when the screen closes without a pairing.
    private func close() {
        if let pending, let relay = Rendezvous.appRelay {
            Task { await Rendezvous(relay: relay).close(pending) }
        }
        ProtectedCache.remove(PendingPairing.cacheName)
        dismiss()
    }

    private func finish() {
        UserDefaults.standard.set(true, forKey: Self.completedKey)
        dismiss()
    }
}

// For a Hermes that cannot run commands: install Coconut.app by hand, then scan its QR code.
private struct PairingHelpView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var scannerOpen = false
    @State private var pasteOpen = false
    let onPayload: (String) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Link(destination: OnboardingView.downloadURL) {
                        Label("Download Coconut for Mac", systemImage: "arrow.down.circle")
                    }
                    ShareLink(item: OnboardingView.downloadURL) {
                        Label("Send the link to your Mac", systemImage: "square.and.arrow.up")
                    }
                } header: {
                    Text("Install Coconut by hand")
                } footer: {
                    Text("On your Mac, open the download, move Coconut to Applications, open it, and choose Set Up Coconut in its menu.")
                }
                Section {
                    #if os(iOS)
                    Button { scannerOpen = true } label: { Label("Scan the QR code", systemImage: "qrcode.viewfinder") }
                        .accessibilityIdentifier("onboarding-scan")
                    #endif
                    Button { pasteOpen = true } label: { Label("Paste a pairing link", systemImage: "doc.on.clipboard") }
                        .accessibilityIdentifier("onboarding-paste")
                } header: {
                    Text("Pair with a QR code")
                } footer: {
                    Text("On your Mac, choose Pair iPhone in the Coconut menu. Then scan the QR code that it shows.")
                }
            }
            .settingsBackground()
            .navigationTitle("Need help?").inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            #if os(iOS)
            .sheet(isPresented: $scannerOpen) {
                PairingScannerView { payload in
                    scannerOpen = false
                    onPayload(payload)
                }
            }
            #endif
            .sheet(isPresented: $pasteOpen) {
                PastePairingSetupView { payload in
                    _ = try PairingSetup.parse(payload)
                    onPayload(payload)
                }
            }
        }
    }
}
