import SwiftUI

struct AppSettingsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var restartingBots = false
    @State private var restartRequested = false

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
                Section {
                    NavigationLink { ApprovalSettingsView() } label: {
                        SettingsNavigationLabel(title: "Access", subtitle: store.approvals?.label(for: store.approvals?.defaults) ?? "Hermes default")
                    }
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
        let status = store.connection == .connected ? "Connected" : "Connecting…"
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

private struct MacPairingFields: View {
    @Environment(AppStore.self) private var store
    @State private var pairing = false
    @State private var draft = PairingSetupDraft()
    @State private var initialized = false
    @State private var detailsExpanded = false
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
        .onAppear {
            guard !initialized else { return }
            initialized = true
            draft = PairingSetupDraft(serverURL: store.serverURL, fingerprint: store.fingerprint, pairCode: store.pairCode)
            detailsExpanded = !store.pairCode.isEmpty
        }
        Button { pasteOpen = true } label: {
            Label("Paste setup details", systemImage: "doc.on.clipboard")
        }
        .disabled(pairing)
        .accessibilityIdentifier("paste-pairing-setup")
        .sheet(isPresented: $pasteOpen) { PastePairingSetupView(importPayload: importSetup) }
        DisclosureGroup("Connection details", isExpanded: $detailsExpanded) {
            TextField("Mac address", text: $draft.serverURL)
                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                .accessibilityLabel("Mac address")
            TextField("Certificate fingerprint", text: $draft.fingerprint, axis: .vertical)
                .font(.caption.monospaced()).lineLimit(2...4)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .accessibilityLabel("SHA-256 certificate fingerprint")
            TextField("Pairing code", text: $draft.pairCode)
                .keyboardType(.numberPad).textContentType(.oneTimeCode).privacySensitive()
                .autocorrectionDisabled().accessibilityIdentifier("pairing-code")
        }.disabled(pairing)
        if let setupError { Text(setupError).font(.footnote).foregroundStyle(BoardStyle.accent) }
        Button {
            pairing = true
            setupError = nil
            Task {
                let previousAddress = store.serverURL
                let previousFingerprint = store.fingerprint
                store.serverURL = draft.serverURL
                store.fingerprint = draft.fingerprint
                store.pairCode = draft.pairCode
                await store.pair()
                // Pair clears the code after saving, before the first state sync.
                let pairingSaved = store.pairCode.isEmpty
                if pairingSaved { draft.pairCode = "" }
                if let error = store.error {
                    setupError = error
                    if store.hasPairing && !pairingSaved {
                        store.serverURL = previousAddress
                        store.fingerprint = previousFingerprint
                    }
                }
                pairing = false
            }
        } label: {
            HStack {
                Text(pairing ? "Connecting…" : "Pair iPhone")
                Spacer()
                if pairing { ProgressView() }
            }
        }.disabled(pairing || draft.pairCode.isEmpty || draft.fingerprint.isEmpty || draft.serverURL.isEmpty)
        Text(store.hasPairing ? "Review the address before pairing. Your current pairing stays unchanged until you tap Pair iPhone."
             : "Get a setup code from your Mac. Review the address, then pair this iPhone.")
            .font(.caption).foregroundStyle(.secondary)
    }

    private func importSetup(_ payload: String) throws {
        try draft.importPayload(payload)
        detailsExpanded = true
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
                    LabeledContent("Address") {
                        Text(store.serverURL).font(.caption).textSelection(.enabled)
                    }
                    Button("Reconnect") { Task { await store.reconnect() } }
                        .disabled(store.connection == .connecting)
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
        .confirmationDialog("Unpair this iPhone?", isPresented: $disconnectOpen, titleVisibility: .visible) {
            Button("Unpair", role: .destructive) { store.disconnect() }
        } message: {
            Text("Your Mac keeps the bots and their history. This iPhone needs a new pairing code to reconnect.")
        }
    }
}



struct BotEditorView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    var bot: Bot? = nil
    var identityOnly = false
    @State private var name = ""
    @State private var role = ""
    @State private var instructions = ""
    @State private var avatar = "sparkle"
    @State private var color = "687D69"
    @State private var saving = false
    @State private var deleteOpen = false
    @State private var originalBot: Bot?
    @State private var appearanceOpen = false
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Button { appearanceOpen = true } label: { BotAvatar(symbol: avatar, color: color, size: 86).frame(maxWidth: .infinity) }.buttonStyle(.plain)
                        .padding(.vertical, 14).listRowBackground(Color.clear)
                    TextField("Name", text: $name).textContentType(.name)
                        .accessibilityIdentifier("bot-name")
                    TextField("Label (optional)", text: $role)
                        .accessibilityIdentifier("bot-role")
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
                    Form { BotAppearancePicker(avatar: $avatar, color: $color) }
                        .navigationTitle("Character").toolbar { Button("Done") { appearanceOpen = false } }
                }.presentationDetents([.medium])
            }
            .onAppear {
                guard !loaded else { return }
                loaded = true
                if let bot {
                    originalBot = bot
                    name = bot.name
                    role = bot.role
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
                updated.role = role
                if !identityOnly {
                    updated.instructions = instructions
                }
                updated.avatar = avatar
                updated.color = color
                await store.updateBot(updated, comparedTo: originalBot)
            } else {
                await store.createBot(name: name, role: role, avatar: avatar, color: color, instructions: instructions, memory: "")
            }
            saving = false
            if store.error == nil { dismiss() }
        }
    }
}

struct BotAppearancePicker: View {
    @Binding var avatar: String
    @Binding var color: String
    private let symbols = ["sparkle", "hammer.fill", "binoculars.fill", "pencil.and.outline", "curlybraces", "leaf.fill", "paperplane.fill", "tray.full.fill"]
    private let colors = ["AA6044", "687D69", "647A97", "8C7594", "9D864C", "777771"]
    private let colorNames = ["Copper", "Green", "Blue", "Purple", "Gold", "Gray"]

    var body: some View {
        VStack(spacing: 14) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 48))], spacing: 10) {
                ForEach(symbols, id: \.self) { symbol in
                    Button { avatar = symbol } label: {
                        Image(systemName: symbol).font(.system(size: 24))
                            .frame(width: 48, height: 48)
                            .background(avatar == symbol ? Color(hex: color).opacity(0.15) : .clear, in: RoundedRectangle(cornerRadius: 14))
                            .overlay(RoundedRectangle(cornerRadius: 14).stroke(avatar == symbol ? Color(hex: color) : .clear))
                    }
                    .buttonStyle(HomePressStyle()).foregroundStyle(Color(hex: color))
                    .accessibilityLabel(symbol.replacingOccurrences(of: ".", with: " "))
                    .accessibilityIdentifier("bot-avatar-\(symbol)")
                    .accessibilityAddTraits(avatar == symbol ? .isSelected : [])
                }
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 44))], spacing: 4) {
                ForEach(Array(colors.enumerated()), id: \.element) { index, choice in
                    Button { color = choice } label: {
                        Circle().fill(Color(hex: choice)).frame(width: 25, height: 25)
                            .padding(5).overlay(Circle().stroke(color == choice ? Color(hex: choice) : .clear, lineWidth: 1.5))
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(HomePressStyle())
                    .accessibilityLabel("\(colorNames[index]) avatar")
                    .accessibilityIdentifier("bot-color-\(choice)")
                    .accessibilityAddTraits(color == choice ? .isSelected : [])
                }
            }
        }.padding(.vertical, 10)
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
