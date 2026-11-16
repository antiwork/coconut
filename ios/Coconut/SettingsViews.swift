import SwiftUI

struct AppSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @Environment(\.dismiss) private var dismiss
    private var paired: Bool { store.hasPairing || store.isDemo }
    @AppStorage(MessageTimes.key) private var showTimes = true

    var body: some View {
        NavigationStack {
            Form {
                Section("Your Mac") {
                    if paired {
                        NavigationLink {
                            MacConnectionSettingsView()
                        } label: {
                            SettingsNavigationLabel(title: "Your Mac", subtitle: connectionSummary)
                        }
                        .accessibilityIdentifier("mac-settings")
                    } else {
                        PairMacButton()
                    }
                }
                Section("General") {
                    NavigationLink { ModelSettingsView() } label: {
                        SettingsNavigationLabel(title: "Model", subtitle: modelSummary)
                    }
                    NavigationLink { ApprovalSettingsView() } label: {
                        SettingsNavigationLabel(title: "Access", subtitle: store.approvals?.label(for: store.approvals?.defaults) ?? "Hermes default")
                    }
                    NotificationSettingsRow()
                    Toggle("Show message times", isOn: $showTimes)
                        .accessibilityIdentifier("show-message-times")
                    NavigationLink { MCPSettingsView() } label: {
                        SettingsNavigationLabel(title: "Connections", subtitle: "Google, GitHub and other tools")
                    }
                    .accessibilityIdentifier("mcp-settings")
                    TeamSettingsRow()
                }
                let bots = store.bots.filter { !$0.archived }
                if !bots.isEmpty {
                    Section("Agents") {
                        ForEach(bots) { bot in
                            NavigationLink { BotSettingsView(botId: bot.id) } label: {
                                HStack(spacing: 12) {
                                    BotAvatar(bot: bot, size: botAvatarSize, animated: false)
                                    Text(bot.name)
                                }
                            }
                            .accessibilityIdentifier("settings-bot-" + bot.id)
                        }
                    }
                }
                Section("Advanced") {
                    NavigationLink { ParallelWorkSettingsView() } label: {
                        SettingsNavigationLabel(title: "Parallel work", subtitle: parallelWorkSummary)
                    }
                    .accessibilityIdentifier("parallel-work-settings")
                }
                if let error = shownError {
                    Section { InlineError(error) }
                }
                Section {} footer: {
                    Text("Coconut \(appVersion) · Execution stays on your Mac.")
                }
            }
            #if os(iOS)
            .listSectionSpacing(24)
            #endif
            .settingsBackground()
            .navigationTitle("Settings").inlineNavigationTitle()
            // The Team row names the team, so Settings reads the teams of a connected Mac.
            .task(id: store.connection) { if store.connection == .connected { await store.loadTeam() } }
            .toolbar {
                ToolbarItem(placement: .leadingBar) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.body.weight(.semibold))
                            .frame(minWidth: 44, minHeight: 44)
                    }
                    .foregroundStyle(.primary)
                    .accessibilityLabel("Close settings")
                    .accessibilityIdentifier("close-settings")
                }
            }
        }
        .presentationCornerRadius(32)
    }

    @ScaledMetric(relativeTo: .body) private var botAvatarSize = 28.0

    private var connectionSummary: String {
        if store.isDemo { return "Preview data" }
        let status = store.connection.title
        guard let lastSync = store.lastSync else { return status }
        return status + " · Synced " + lastSync.formatted(date: .abbreviated, time: .shortened)
    }

    private var parallelWorkSummary: String {
        guard let capacity = store.workCapacity else { return "Agents working at once" }
        return capacity.limit == nil ? "Automatic · up to \(capacity.effectiveLimit) agents at once" : "Up to \(capacity.effectiveLimit) agents at once"
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

struct SettingsNavigationLabel: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(title).foregroundStyle(.primary)
            Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
        }.padding(.vertical, BoardStyle.Space.xs)
    }
}

private struct MacConnectionSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @State private var disconnectOpen = false
    @State private var restartingBots = false
    @State private var restartRequested = false
    @State private var hermes: HermesRuntimeStatus?

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
                    LabeledContent("Mac") {
                        Text(store.pairedMacDescription).font(.footnote).textSelection(.enabled)
                    }
                    Button("Reconnect") { Task { await store.reconnect() } }
                } else {
                    PairMacButton()
                }
            }
            if let warning = hermes?.untestedWarning {
                Section {
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .accessibilityIdentifier("hermes-untested")
                }
            }
            if store.hasPairing || store.isDemo {
                Section {
                    Button(restartRequested ? "Restart Requested" : "Restart Agents") {
                        restartingBots = true
                        Task {
                            restartRequested = await store.restartBots()
                            restartingBots = false
                        }
                    }
                    .disabled(restartingBots || restartRequested)
                    .accessibilityIdentifier("restart-bots")
                } footer: {
                    Text("All agents start fresh after current work finishes. Chats, memory, and routines stay.")
                }
            }
            Section("When the app closes") {
                Text("Your Mac keeps working when you close Coconut.")
                Text("Open the app to sync messages and decisions. Push alerts can arrive while the app is closed.")
                Text("When your phone cannot reach the Mac, it shows the last known state.")
            }.font(.subheadline).foregroundStyle(.secondary)
            if let error = store.syncIssue ?? shownError {
                Section { InlineError(error) }
            }
            if store.hasPairing && !store.isDemo {
                Section {
                    PairMacButton()
                }
                Section {
                    Button("Unpair this iPhone", role: .destructive) { disconnectOpen = true }
                }
            }
        }
        .settingsBackground()
        .navigationTitle("Your Mac").inlineNavigationTitle()
        // The check is quiet: a Mac that does not answer shows no warning and no error.
        .task(id: store.connection == .connected) {
            guard store.connection == .connected else { return }
            hermes = await store.loadHermesSetup(reportsErrors: false)?.hermes
        }
        .confirmationDialog("Unpair this iPhone?", isPresented: $disconnectOpen, titleVisibility: .visible) {
            Button("Unpair", role: .destructive) { store.disconnect() }
        } message: {
            Text("Your Mac keeps the agents and their history. This iPhone needs a new pairing link to reconnect.")
        }
    }
}

// The character, name, and description of a bot. New bots and bot settings use the same fields.
struct BotIdentityFields: View {
    @Binding var name: String
    @Binding var instructions: String
    @Binding var avatar: String
    @Binding var color: String
    let identity: String
    // Called when a text field loses focus or the person submits it.
    var commit: () -> Void = {}
    @State private var appearanceOpen = false
    @FocusState private var focused: Field?
    @ScaledMetric(relativeTo: .largeTitle) private var avatarSize = 86.0

    private enum Field { case name, instructions }

    var body: some View {
        Section {
            Button { appearanceOpen = true } label: {
                BotAvatar(symbol: avatar, color: color, size: avatarSize, identity: identity)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.l)
            }
            .buttonStyle(.plain).listRowBackground(Color.clear)
            .accessibilityLabel("Change character")
            .sheet(isPresented: $appearanceOpen) {
                NavigationStack {
                    Form { BotAppearancePicker(avatar: $avatar, color: $color, identity: identity) }
                        .settingsBackground()
                        // In the bot settings a pick saves at once, and the save can fail. The sheet shows the error at
                        // its top, and the page behind it shows none. It stays in view at each scroll position.
                        .pageErrorBanner()
                        #if os(macOS)
                        // A Mac sheet takes the height of its content. The fruits made it 835 points high, and the
                        // notice of an error made it higher than a small window, so Done went off the screen. With a
                        // fixed height the fruits scroll, and the notice takes its place inside the sheet. A plain Mac
                        // form did not scroll inside the height, and it pushed the notice above the sheet. A grouped
                        // form scrolls, as in the New agent sheet.
                        .formStyle(.grouped)
                        .frame(width: 470, height: 600)
                        #endif
                        .navigationTitle("Character").inlineNavigationTitle()
                        .toolbar { Button("Done") { appearanceOpen = false } }
                }
                .sheetShowsError()
                .presentationDetents([.large])
            }
        }
        Section("Name") {
            TextField("Name", text: $name).textContentType(.name)
                .fieldUnderHeader()
                .focused($focused, equals: .name)
                .submitLabel(.done)
                .onSubmit(commit)
                .accessibilityIdentifier("bot-name")
        }
        Section("Description") {
            TextField("What should this agent do?", text: $instructions, axis: .vertical)
                .lineLimit(5...18)
                .fieldUnderHeader()
                .focused($focused, equals: .instructions)
                .onSubmit(commit)
                .accessibilityIdentifier("bot-instructions")
                .onChange(of: focused) { previous, current in
                    if previous != nil && previous != current { commit() }
                }
        }
    }
}

// Creates a bot. Bot settings edit an existing bot and save each change.
struct BotEditorView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var instructions = ""
    @State private var avatar = "fruit." + FruitArtwork.all.randomElement()!.id
    @State private var color = "A86640"
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                BotIdentityFields(name: $name, instructions: $instructions, avatar: $avatar, color: $color, identity: "")
                if let error = shownError {
                    Section { InlineError(error) }
                }
            }
            .disabled(saving)
            .settingsBackground()
            #if os(macOS)
            // A Mac sheet sizes itself to a plain form, which then runs its fields to the edges. Grouped sections give
            // the fields their boxes and margins, as in the bot details.
            .formStyle(.grouped)
            .frame(width: 460, height: 460)
            #endif
            .navigationTitle("New agent").inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Adding…" : "Add", action: create)
                        .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityIdentifier("save-bot")
                }
            }
        }
    }

    private func create() {
        saving = true
        store.error = nil
        Task {
            await store.createBot(name: name, role: "", avatar: avatar, color: color, instructions: instructions, memory: "")
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
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 76))], spacing: BoardStyle.Space.l) {
                ForEach(FruitArtwork.all, id: \.id) { fruit in
                    Button { avatar = "fruit." + fruit.id; color = fruit.color } label: {
                        VStack(spacing: BoardStyle.Space.xs) {
                            BotAvatar(symbol: "fruit." + fruit.id, color: fruit.color, size: 56, animated: false)
                            Text(fruit.name).font(.footnote).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.s)
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
