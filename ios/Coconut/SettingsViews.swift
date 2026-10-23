import SwiftUI

struct AppSettingsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var restartingBots = false
    @State private var restartRequested = false
    @State private var hermesSetupOpen = false

    var body: some View {
        NavigationStack {
            Form {
                if store.hasPairing || store.isDemo {
                    Section {
                        NavigationLink {
                            MacConnectionSettingsView()
                        } label: {
                            HStack(spacing: 14) {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("Your Mac").font(.body.weight(.medium))
                                    Text(connectionSummary).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 8)
                            }.padding(.vertical, 5)
                        }
                    }
                } else {
                    Section("Connect your Mac") { MacPairingFields() }
                }
                Section {
                    NavigationLink {
                        ModelSettingsView()
                    } label: {
                        SettingsNavigationLabel(title: "Model", subtitle: modelSummary)
                    }
                }
                if store.hasPairing || store.isDemo {
                    Section {
                        Button { hermesSetupOpen = true } label: {
                            SettingsNavigationLabel(title: "Hermes setup", subtitle: "Import profiles and model logins")
                        }
                        .foregroundStyle(.primary)
                        .accessibilityIdentifier("hermes-setup")
                    }
                }
                Section {
                    NavigationLink { ApprovalSettingsView() } label: {
                        SettingsNavigationLabel(title: "Access", subtitle: store.approvals?.label(for: store.approvals?.defaults) ?? "Hermes default")
                    }
                }
                Section {
                    NavigationLink { ParallelWorkSettingsView() } label: {
                        SettingsNavigationLabel(title: "Parallel work", subtitle: store.parallelWork.map { "Up to \($0.effectiveLimit) workers per session" } ?? "Hermes workers")
                    }
                    .accessibilityIdentifier("parallel-work-settings")
                }
                Section {
                    NavigationLink { MCPSettingsView() } label: {
                        SettingsNavigationLabel(title: "Connections", subtitle: "Google and MCP servers")
                    }
                    .accessibilityIdentifier("mcp-settings")
                }
                Section {
                    NotificationSettingsRow()
                }
                Section("Bots") {
                    ForEach(store.bots.filter { !$0.archived }) { bot in
                        NavigationLink(bot.name) {
                            List {
                                NavigationLink("Model") { ModelSettingsView(botId: bot.id) }
                                NavigationLink("Access") { ApprovalSettingsView(botId: bot.id) }
                                NavigationLink("Parallel work") { ParallelWorkSettingsView(botId: bot.id) }
                            }.navigationTitle(bot.name)
                        }
                    }
                }
                if store.hasPairing || store.isDemo {
                    Section {
                        Button(restartRequested ? "Restart Requested" : "Restart Bots") {
                            restartingBots = true
                            Task {
                                restartRequested = await store.restartBots()
                                restartingBots = false
                            }
                        }
                        .disabled(restartingBots || restartRequested)
                        .accessibilityIdentifier("restart-bots")
                    } footer: {
                        Text("All bots start fresh after current work finishes. Chats, memory, and routines stay.")
                    }
                }
                if let error = store.error {
                    Section { Text(error).font(.footnote).foregroundStyle(BoardStyle.accent).textSelection(.enabled) }
                }
                Section {} footer: {
                    Text("Coconut \(appVersion) · Execution stays on your Mac.")
                }
            }
            .fullScreenCover(isPresented: $hermesSetupOpen) { OnboardingView(start: .setup) }
            .listSectionSpacing(24)
            .scrollContentBackground(.hidden).background(BoardStyle.paper)
            .navigationTitle("Settings").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 17, weight: .medium))
                            .frame(width: 44, height: 44)
                    }
                    .foregroundStyle(.primary)
                    .accessibilityLabel("Close settings")
                    .accessibilityIdentifier("close-settings")
                }
            }
        }
        .presentationCornerRadius(32)
    }

    private var connectionSummary: String {
        if store.isDemo { return "Preview data" }
        let status: String = switch store.connection {
        case .connected: "Connected"
        case .connecting: "Connecting…"
        case .disconnected: "Disconnected"
        case .macOffline: "Mac offline"
        case .relayUnreachable: "Relay unreachable"
        case .needsRepair: "Needs a new pairing link"
        case .unconfigured: "Not paired"
        case .preview: "Preview data"
        }
        guard let lastSync = store.lastSync else { return status }
        return status + " · Synced " + lastSync.formatted(date: .abbreviated, time: .shortened)
    }

    private var appVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Unknown"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "Unknown"
        return "\(version) (\(build))"
    }

    private var modelSummary: String {
        guard let catalog = store.modelCatalog else { return store.model.isEmpty ? "Configure on your Mac" : store.model }
        return catalog.name(for: catalog.defaults.selection) + " · " + ModelPresentation.effortName(catalog.defaults.reasoningEffort)
    }
}

private struct SettingsNavigationLabel: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).foregroundStyle(.primary)
            Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
        }.padding(.vertical, 5)
    }
}

struct MacPairingFields: View {
    @Environment(AppStore.self) private var store
    @State private var pairing = false
    @State private var setup: PairingSetup?
    @State private var scannerOpen = false
    @State private var pasteOpen = false
    @State private var setupError: String?

    var body: some View {
        Button { scannerOpen = true } label: {
            Label("Scan setup code", systemImage: "qrcode.viewfinder")
        }
        .disabled(pairing)
        .accessibilityIdentifier("scan-pairing-setup")
        .sheet(isPresented: $scannerOpen) {
            PairingScannerView { payload in
                do { try importSetup(payload) }
                catch { setupError = error.localizedDescription }
            }
        }
        Button { pasteOpen = true } label: {
            Label("Paste setup link", systemImage: "doc.on.clipboard")
        }
        .disabled(pairing)
        .accessibilityIdentifier("paste-pairing-setup")
        .sheet(isPresented: $pasteOpen) { PastePairingSetupView(importPayload: importSetup) }
        if let setup {
            LabeledContent("Mac") { Text(setup.name).textSelection(.enabled) }
        }
        if let setupError { Text(setupError).font(.footnote).foregroundStyle(BoardStyle.accent) }
        Button {
            guard let setup else { return }
            pairing = true
            setupError = nil
            Task {
                if await store.pair(setup: setup) {
                    self.setup = nil
                } else {
                    setupError = store.error ?? "Coconut could not pair with this Mac."
                }
                pairing = false
            }
        } label: {
            HStack {
                Text(pairing ? "Connecting…" : "Pair iPhone")
                Spacer()
                if pairing { ProgressView() }
            }
        }.disabled(pairing || setup == nil)
        Text(store.hasPairing ? "Check the Mac name before pairing. Your current pairing stays unchanged until you tap Pair iPhone."
             : "Ask your Hermes for a pairing link. Check the Mac name, then pair this iPhone.")
            .font(.caption).foregroundStyle(.secondary)
    }

    private func importSetup(_ payload: String) throws {
        setup = try PairingSetup.parse(payload)
        setupError = nil
    }
}

private struct MacConnectionSettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var disconnectOpen = false

    var body: some View {
        Form {
            Section("Connection") {
                ConnectionRow()
                if let lastSync = store.lastSync {
                    LabeledContent("Last sync", value: lastSync.formatted(date: .abbreviated, time: .shortened))
                        .font(.subheadline).monospacedDigit()
                }
                if store.isDemo {
                    Text("Preview uses sample conversations.").foregroundStyle(.secondary)
                } else if store.hasPairing {
                    LabeledContent(store.usesLegacyPairing ? "Address" : "Mac") {
                        Text(store.pairedMacDescription).font(.caption).textSelection(.enabled)
                    }
                    Button("Reconnect") { Task { await store.reconnect() } }
                    if store.usesLegacyPairing {
                        // A version 1 pairing works until R5. The next pairing link moves this iPhone to the relay.
                        Text("Coconut no longer needs Tailscale. Ask your Hermes for a new pairing link.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                } else {
                    MacPairingFields()
                }
            }
            Section("When the app closes") {
                Text("Your Mac keeps working when you close Coconut.")
                Text("Open the app to sync messages and decisions. Push alerts can arrive while the app is closed.")
                Text("When your phone cannot reach the Mac, it shows the last known state.")
            }.font(.subheadline).foregroundStyle(.secondary)
            if let error = store.syncIssue ?? store.error {
                Section { Text(error).font(.footnote).foregroundStyle(.secondary).textSelection(.enabled) }
            }
            if store.hasPairing && !store.isDemo {
                Section {
                    NavigationLink("Pair with a Mac") {
                        Form { Section("Connect your Mac") { MacPairingFields() } }
                            .navigationTitle("Pair with a Mac").navigationBarTitleDisplayMode(.inline)
                    }
                }
                Section {
                    Button("Unpair this iPhone", role: .destructive) { disconnectOpen = true }
                }
            }
        }
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle("Your Mac").navigationBarTitleDisplayMode(.inline)
        .confirmationDialog(store.checkoutConfirmation == nil ? "Unpair this iPhone?" : "Unpair with recovery unresolved?",
                            isPresented: $disconnectOpen, titleVisibility: .visible) {
            Button(store.checkoutConfirmation == nil ? "Unpair" : "Unpair anyway", role: .destructive) { store.disconnect() }
            if store.checkoutConfirmation != nil {
                Button("Keep pairing to check or stop recovery", role: .cancel) {}
            }
        } message: {
            Text(store.checkoutConfirmation == nil
                 ? "Your Mac keeps the bots and their history. This iPhone needs a new pairing code to reconnect."
                 : "Unpairing does not cancel recovery on your Mac. You will lose paired access to check its status or stop it. Keep this pairing to check or stop recovery, or unpair anyway if you cannot reach your Mac.")
        }
    }
}



struct BotEditorView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    var bot: Bot? = nil
    var identityOnly = false
    @State private var name = ""
    @State private var instructions = ""
    @State private var avatar = "fruit." + FruitArtwork.all.randomElement()!.id
    @State private var color = "A86640"
    @State private var saving = false
    @State private var deleteOpen = false
    @State private var originalBot: Bot?
    @State private var appearanceOpen = false
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Button { appearanceOpen = true } label: { BotAvatar(symbol: avatar, color: color, size: 86, identity: bot?.id ?? "").frame(maxWidth: .infinity) }.buttonStyle(.plain).accessibilityLabel("Change character")
                        .padding(.vertical, 14).listRowBackground(Color.clear)
                    TextField("Name", text: $name).textContentType(.name)
                        .accessibilityIdentifier("bot-name")
                }

                Section("Description") {
                    TextField("What should this bot do?", text: $instructions, axis: .vertical)
                        .lineLimit(5...18).accessibilityIdentifier("bot-instructions")
                }
                if let error = store.error {
                    Section { Text(error).font(.caption).foregroundStyle(BoardStyle.accent) }
                }
            }
            .disabled(saving)
            .scrollContentBackground(.hidden).background(BoardStyle.paper)
            .navigationTitle(identityOnly ? "Character" : bot == nil ? "New bot" : "Bot details").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : "Save", action: save)
                        .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityIdentifier("save-bot")
                }
            }
            .sheet(isPresented: $appearanceOpen) {
                NavigationStack {
                    Form { BotAppearancePicker(avatar: $avatar, color: $color, identity: bot?.id ?? "") }
                        .navigationTitle("Character").toolbar { Button("Done") { appearanceOpen = false } }
                }.presentationDetents([.large])
            }
            .onAppear {
                guard !loaded else { return }
                loaded = true
                if let bot {
                    originalBot = bot
                    name = bot.name
                    instructions = bot.instructions
                    avatar = bot.avatar
                    color = bot.color
                }
            }
            .confirmationDialog("Delete \(bot?.name ?? "bot")?", isPresented: $deleteOpen, titleVisibility: .visible) {
                Button("Delete bot", role: .destructive) {
                    if let bot {
                        Task {
                            store.error = nil
                            await store.deleteBot(bot)
                            if store.error == nil { dismiss() }
                        }
                    }
                }
            } message: {
                Text("The Mac cancels this bot's work and removes its conversation. The audit record remains on your Mac.")
            }
        }
    }

    private func save() {
        saving = true
        store.error = nil
        Task {
            if let originalBot {
                var updated = originalBot
                updated.name = name
                if !identityOnly {
                    updated.instructions = instructions
                }
                updated.avatar = avatar
                updated.color = color
                await store.updateBot(updated, comparedTo: originalBot)
            } else {
                await store.createBot(name: name, role: "", avatar: avatar, color: color, instructions: instructions, memory: "")
            }
            saving = false
            if store.error == nil { dismiss() }
        }
    }
}

struct BotAppearancePicker: View {
    @Binding var avatar: String
    @Binding var color: String
    var identity = ""
    private var selectedFruit: FruitArtwork { FruitArtwork.all[FruitArtwork.index(symbol: avatar, identity: identity)] }

    var body: some View {
        Section {
            BotAvatar(symbol: avatar, color: color, size: 120, identity: identity)
                .frame(maxWidth: .infinity).padding(.vertical, 12)
        }.listRowBackground(Color.clear)
        Section("Fruit") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 76))], spacing: 18) {
                ForEach(FruitArtwork.all, id: \.id) { fruit in
                    Button { avatar = "fruit." + fruit.id; color = fruit.color } label: {
                        VStack(spacing: 3) {
                            BotAvatar(symbol: "fruit." + fruit.id, color: fruit.color, size: 56, animated: false)
                            Text(fruit.name).font(.caption2).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, 7)
                        .background(selectedFruit.id == fruit.id ? BoardStyle.ink.opacity(0.07) : .clear,
                                    in: RoundedRectangle(cornerRadius: 16))
                    }
                    .buttonStyle(HomePressStyle())
                    .accessibilityLabel(fruit.name)
                    .accessibilityIdentifier("bot-avatar-fruit." + fruit.id)
                    .accessibilityAddTraits(selectedFruit.id == fruit.id ? .isSelected : [])
                }
            }
        }
    }
}

struct LearningView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        List {
            Section {
                Text("Before you decide, the bot records its predicted choice. Coconut compares that prediction with your response and corrections.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if store.learning.isEmpty {
                QuietEmptyState(symbol: "checkmark.bubble", title: "Learning from your decisions", message: "Agreement appears here after you respond to decisions. Permissions stay under your control.")
                    .listRowBackground(Color.clear)
            }
            ForEach(store.learning) { group in
                Section(store.bots.first { $0.id == group.botId }?.name ?? "Bot") {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(group.actionType.replacingOccurrences(of: "_", with: " ").capitalized)
                            .font(.headline)
                        HStack(alignment: .firstTextBaseline) {
                            Text(group.agreement, format: .percent.precision(.fractionLength(0)))
                                .font(.title2.weight(.semibold)).monospacedDigit()
                            Text("agreement").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Text("\(group.agreed) of \(group.total)").font(.caption).foregroundStyle(.secondary)
                        }
                        if let suggestion = group.suggestion {
                            Text(suggestion).font(.subheadline).foregroundStyle(.secondary)
                        }
                        Text("No permission changes applied.").font(.caption2).foregroundStyle(.tertiary)
                    }.padding(.vertical, 7)
                }
            }
        }
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle("Decision learning").navigationBarTitleDisplayMode(.inline)
        .task { await store.loadLearning() }
    }
}
