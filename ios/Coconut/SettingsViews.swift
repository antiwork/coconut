import SwiftUI

// Coconut has no Settings screen. Profile (ProfileView) has the things of this device and the defaults of the agents,
// and the Coconut chat changes the rest.

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

struct MacConnectionSettingsView: View {
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
