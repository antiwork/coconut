import SwiftUI

struct BotSettingsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let botId: String
    @State private var draft: Bot?
    @State private var original: Bot?
    @State private var appearanceOpen = false
    @State private var routineOpen = false
    @State private var deleteOpen = false
    @State private var saving = false

    var body: some View {
        Form {
            if let draft {
                Section {
                    Button { appearanceOpen = true } label: {
                        BotAvatar(symbol: draft.avatar, color: draft.color, size: 86)
                            .frame(maxWidth: .infinity).padding(.vertical, 20)
                    }
                    .buttonStyle(.plain).listRowBackground(Color.clear)
                    .accessibilityLabel("Change character")
                }
                Section("Name") {
                    TextField("Name", text: field(\.name)).accessibilityIdentifier("bot-name")
                }
                Section("Label (optional)") {
                    TextField("Research, marketing, admin", text: field(\.role)).accessibilityIdentifier("bot-role")
                }
                Section("Description") {
                    TextField("What should this bot do?", text: field(\.instructions), axis: .vertical)
                        .lineLimit(5...18).accessibilityIdentifier("bot-instructions")
                }
                Section {
                    Toggle("Notifications", isOn: Binding(get: { self.draft?.notifications ?? true }, set: { self.draft?.notifications = $0 }))
                } footer: { Text("Get notified when this bot needs a decision, finishes work, or fails.") }
                Section("Routines") {
                    ForEach(store.routines.filter { $0.botId == botId }) { routine in
                        NavigationLink { RoutineEditorView(botId: botId, routine: routine) } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(routine.name)
                                Text(routine.error ?? routine.scheduleLabel).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                            }.padding(.vertical, 3)
                        }
                    }
                    Button("Add routine", systemImage: "plus") { routineOpen = true }.accessibilityIdentifier("add-routine")
                }
            }
            if let error = store.error { Section { Text(error).foregroundStyle(BoardStyle.accent) } }
        }
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle(draft?.name ?? "Bot").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if let draft, let original, draft != original {
                    Button(saving ? "Saving…" : "Save") {
                        saving = true
                        Task {
                            await store.updateBot(draft, comparedTo: original)
                            saving = false
                            if store.error == nil { self.original = draft }
                        }
                    }.disabled(saving || draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("save-bot")
                } else {
                    Menu {
                        if let draft {
                            Button(draft.archived ? "Restore bot" : "Archive bot") {
                                Task { await store.archiveBot(draft); if store.error == nil { dismiss() } }
                            }
                            Button("Delete bot", role: .destructive) { deleteOpen = true }
                        }
                    } label: { Image(systemName: "ellipsis") }.accessibilityLabel("Bot options")
                }
            }
        }
        .onAppear {
            if draft == nil { draft = store.bots.first { $0.id == botId }; original = draft }
        }
        .sheet(isPresented: $appearanceOpen) {
            NavigationStack {
                Form { BotAppearancePicker(avatar: field(\.avatar), color: field(\.color)) }
                    .navigationTitle("Character").navigationBarTitleDisplayMode(.inline)
                    .toolbar { Button("Done") { appearanceOpen = false } }
            }.presentationDetents([.medium])
        }
        .sheet(isPresented: $routineOpen) { NavigationStack { RoutineEditorView(botId: botId) } }
        .confirmationDialog("Delete \(draft?.name ?? "bot")?", isPresented: $deleteOpen, titleVisibility: .visible) {
            Button("Delete bot", role: .destructive) {
                if let draft { Task { await store.deleteBot(draft); if store.error == nil { dismiss() } } }
            }
        }
    }

    private func field(_ path: WritableKeyPath<Bot, String>) -> Binding<String> {
        Binding(get: { draft?[keyPath: path] ?? "" }, set: { draft?[keyPath: path] = $0 })
    }
}

struct BotTextDraft {
    var text = ""
    private(set) var baseline = ""
    private(set) var isLoaded = false

    mutating func loadCached(_ value: String) {
        guard !isLoaded else { return }
        text = value
        baseline = value
        isLoaded = true
    }

    mutating func applyFresh(_ value: String, requestedBaseline: String) {
        guard text == baseline, baseline == requestedBaseline else { return }
        text = value
        baseline = value
        isLoaded = true
    }

    mutating func markSaved(_ submitted: String) { baseline = submitted }
}

private struct RoutineEditorView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let botId: String
    var routine: Routine? = nil
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = ""
    @State private var enabled = true
    @State private var loaded = false
    @State private var saving = false
    @State private var deleteOpen = false

    private var current: Routine? { store.routines.first { $0.id == routine?.id } ?? routine }
    private var canSave: Bool {
        !saving && !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !schedule.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        Form {
            Section("Name") { TextField("Morning brief", text: $name).accessibilityIdentifier("routine-name") }
            Section("Instructions") {
                TextField("What should this bot do?", text: $prompt, axis: .vertical)
                    .lineLimit(4...12).accessibilityIdentifier("routine-prompt")
            }
            Section {
                TextField("Weekdays at 8am", text: $schedule)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .accessibilityIdentifier("routine-schedule")
                Toggle("Enabled", isOn: $enabled).accessibilityIdentifier("routine-enabled")
            } header: { Text("Schedule") } footer: {
                Text("Use a Hermes schedule, such as ‘weekdays at 8am’, ‘every 30m’, or a cron expression. Times follow this bot’s Hermes time zone.")
            }
            if let current {
                if let error = current.error { Section { Text(error).foregroundStyle(BoardStyle.accent) } }
                Section {
                    if let date = current.nextRunAt?.apiDate, current.enabled {
                        LabeledContent("Next run", value: date.formatted(date: .abbreviated, time: .shortened))
                    }
                    Button("Run now", systemImage: "play") {
                        Task { _ = await store.runRoutine(current) }
                    }.disabled(saving || current.error != nil).accessibilityIdentifier("run-routine")
                    if current.lastTaskId != nil {
                        NavigationLink("Open conversation", value: ConversationRoute(botId: botId))
                    }
                    Button("Delete routine", role: .destructive) { deleteOpen = true }
                }
            }
            if let error = store.error { Section { Text(error).foregroundStyle(BoardStyle.accent) } }
        }
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle(routine == nil ? "New routine" : "Routine").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if routine == nil { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            ToolbarItem(placement: .confirmationAction) { Button(saving ? "Saving…" : "Save", action: save).disabled(!canSave).accessibilityIdentifier("save-routine") }
        }
        .onAppear {
            guard !loaded else { return }
            loaded = true
            if let current { name = current.name; prompt = current.prompt; schedule = current.cron; enabled = current.enabled }
        }
        .confirmationDialog("Delete this routine?", isPresented: $deleteOpen, titleVisibility: .visible) {
            Button("Delete routine", role: .destructive) {
                if let current { Task { await store.deleteRoutine(current); if store.error == nil { dismiss() } } }
            }
        }
    }

    private func save() {
        guard let bot = store.bots.first(where: { $0.id == botId }) else { return }
        saving = true
        Task {
            if var current {
                current.name = name; current.prompt = prompt; current.cron = schedule; current.enabled = enabled
                await store.updateRoutine(current)
            } else {
                await store.createRoutine(bot: bot, name: name, prompt: prompt, cron: schedule, timezone: "", enabled: enabled)
            }
            saving = false
            if store.error == nil { dismiss() }
        }
    }
}
