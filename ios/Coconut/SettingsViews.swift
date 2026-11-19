import SwiftUI

// Coconut has no Settings screen. Profile (ProfileView) has the things of this device and the settings that every chat
// shares, and the Coconut chat changes the rest.

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
                    Text("The demo uses sample projects and chats.").foregroundStyle(.secondary)
                    PairMacButton()
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
                    Button(restartRequested ? "Restart requested" : "Restart agents") {
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
