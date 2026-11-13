import SwiftUI

// Each change saves by itself: toggles and the character when they change, text when the field loses focus,
// when the person submits it, and when the page closes. Back never drops an edit.
struct BotSettingsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.windowShowsError) private var windowShowsError
    let botId: String
    @State private var edits: BotDraft?
    @State private var routineOpen = false

    private var draft: Bot? { edits?.bot }

    var body: some View {
        Form {
            if let draft {
                BotIdentityFields(name: field(\.name), instructions: field(\.instructions),
                                  avatar: field(\.avatar), color: field(\.color), identity: botId, commit: save)
                Section {
                    NavigationLink { ModelSettingsView(botId: botId) } label: {
                        SettingsNavigationLabel(title: "Model", subtitle: modelSummary(draft))
                    }
                    .accessibilityIdentifier("bot-model-settings")
                    NavigationLink { ApprovalSettingsView(botId: botId) } label: {
                        SettingsNavigationLabel(title: "Access", subtitle: accessSummary(draft))
                    }
                    .accessibilityIdentifier("bot-access-settings")
                }
                Section {
                    Toggle("Notifications", isOn: Binding(get: { edits?.bot.notifications ?? true }, set: { edits?.bot.notifications = $0 }))
                        .tint(BoardStyle.green)
                        .accessibilityIdentifier("bot-notifications")
                } footer: { Text("Get notified when this bot needs a decision, finishes work, or fails.") }
                let groups = RoutineGroups(store.routines.filter { $0.botId == botId })
                Section("Routines") {
                    ForEach(groups.recurring) { routine in
                        routineLink(routine, detail: routine.error ?? routineLabel(routine))
                    }
                    Button("Add routine", systemImage: "plus") { routineOpen = true }.accessibilityIdentifier("add-routine")
                }
                if !groups.scheduledChecks.isEmpty {
                    Section("Scheduled checks") {
                        ForEach(groups.scheduledChecks) { routine in
                            routineLink(routine, detail: routine.error ?? checkLabel(routine))
                        }
                    }
                }
            }
            if !windowShowsError, let error = store.error { Section { InlineError(error) } }
        }
        .settingsBackground()
        .navigationTitle(draft?.name ?? "Bot").inlineNavigationTitle()
        .onAppear {
            if edits == nil, let bot = store.bots.first(where: { $0.id == botId }) { edits = BotDraft(bot) }
        }
        .onChange(of: store.bots.first { $0.id == botId }) { _, current in
            if let current { edits?.receive(current) }
        }
        .onChange(of: draft?.notifications) { _, _ in save() }
        .onChange(of: draft?.avatar) { _, _ in save() }
        .onChange(of: draft?.color) { _, _ in save() }
        .onDisappear(perform: save)
        // A sheet has no notice over it, so it shows the error itself.
        .sheet(isPresented: $routineOpen) {
            NavigationStack { RoutineEditorView(botId: botId) }.environment(\.windowShowsError, false)
        }
    }

    private func routineLink(_ routine: Routine, detail: String) -> some View {
        NavigationLink { RoutineEditorView(botId: botId, routine: routine) } label: {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(routine.name)
                Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
            }.padding(.vertical, BoardStyle.Space.xs)
        }
    }

    // A routine with a script check names its script after the schedule.
    private func routineLabel(_ routine: Routine) -> String {
        guard let script = routine.scriptCheck else { return routine.scheduleLabel }
        return routine.scheduleLabel + "\nScript check: " + script
    }

    // A one-time check shows its run time. A paused check that still has a future time says that it is paused.
    private func checkLabel(_ routine: Routine) -> String {
        guard let date = (routine.nextRunAt ?? routine.runAt)?.apiDate else { return routine.scheduleLabel }
        let time = date.formatted(date: .abbreviated, time: .shortened)
        return routine.enabled && routine.state != "paused" ? "Next run " + time : "Paused · " + time
    }

    private func modelSummary(_ bot: Bot) -> String {
        guard let selection = bot.modelOverride else { return "Default model" }
        return store.modelCatalog?.name(for: selection) ?? selection.model
    }

    private func accessSummary(_ bot: Bot) -> String {
        guard let value = bot.approvalOverride else { return "Default access" }
        return store.approvals?.label(for: value) ?? value
    }

    // One save at a time. A change during a save starts one more save after it.
    private func save() {
        guard let request = edits?.beginSave() else { return }
        Task {
            await store.updateBot(request.pending, comparedTo: request.original)
            let current = store.error == nil ? store.bots.first(where: { $0.id == botId }) : nil
            if edits?.finishSave(current) == true { save() }
        }
    }

    private func field(_ path: WritableKeyPath<Bot, String>) -> Binding<String> {
        Binding(get: { edits?.bot[keyPath: path] ?? "" }, set: { edits?.bot[keyPath: path] = $0 })
    }
}

// The bot on the bot settings page, the newest bot from the Mac, and the save that runs.
struct BotDraft: Equatable {
    var bot: Bot
    private(set) var saved: Bot
    private(set) var submitted: Bot?
    private var saveAgain = false

    init(_ bot: Bot) {
        self.bot = bot
        saved = bot
    }

    // The bot to send and the bot it changes, or nil. A request during a save asks for one more save after it.
    mutating func beginSave() -> (pending: Bot, original: Bot)? {
        guard submitted == nil else {
            saveAgain = true
            return nil
        }
        guard let pending = BotAutosave.changes(bot, comparedTo: saved) else { return nil }
        submitted = pending
        return (pending, saved)
    }

    // An update from the Mac, also during a save. It keeps every edit that the Mac has not confirmed.
    mutating func receive(_ current: Bot) {
        bot = BotAutosave.merge(draft: bot, baseline: saved, into: current)
        saved = current
    }

    // The save ended. `current` is the bot after a successful save, nil after a failure.
    // Returns true when a change during the save needs one more save.
    mutating func finishSave(_ current: Bot?) -> Bool {
        if let current, let submitted {
            // The confirmed result replaces only the fields that the person did not edit after the save started.
            bot = BotAutosave.merge(draft: bot, baseline: submitted, into: current)
            saved = current
        }
        submitted = nil
        defer { saveAgain = false }
        return saveAgain
    }
}

enum BotAutosave {
    // The bot to save, or nil when nothing changed. An empty name keeps the saved name, so the other edits still save.
    static func changes(_ draft: Bot, comparedTo original: Bot) -> Bot? {
        var pending = draft
        if pending.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { pending.name = original.name }
        return BotPatch(updated: pending, original: original).isEmpty ? nil : pending
    }

    // The bot from the Mac, with each draft field that differs from the baseline kept as the person left it.
    static func merge(draft: Bot, baseline: Bot, into current: Bot) -> Bot {
        var updated = current
        let fields: [WritableKeyPath<Bot, String>] = [\.name, \.instructions, \.avatar, \.color]
        for field in fields where draft[keyPath: field] != baseline[keyPath: field] {
            updated[keyPath: field] = draft[keyPath: field]
        }
        if draft.notifications != baseline.notifications { updated.notifications = draft.notifications }
        return updated
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
    @Environment(\.windowShowsError) private var windowShowsError
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
            && (current?.script != nil || !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            && !schedule.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        Form {
            Section("Name") { TextField("Morning brief", text: $name).accessibilityIdentifier("routine-name") }
            if let script = current?.script {
                Section {
                    Text(script).font(.footnote.monospaced()).textSelection(.enabled)
                } header: { Text("Script") } footer: {
                    Text("Runs on the Mac without model tokens. Ask this bot to change the script.")
                }
            } else {
                Section("Instructions") {
                    TextField("What should this bot do?", text: $prompt, axis: .vertical)
                        .fieldUnderHeader()
                        .lineLimit(4...12).accessibilityIdentifier("routine-prompt")
                }
                if let check = current?.scriptCheck {
                    Section {
                        LabeledContent("Script check", value: check).accessibilityIdentifier("routine-script-check")
                    } footer: {
                        Text("This script runs first, without model tokens. The bot runs only when the script finds work. An edit here keeps the script.")
                    }
                }
            }
            Section {
                TextField("Weekdays at 8am", text: $schedule)
                    .noAutocapitalization().autocorrectionDisabled()
                    .accessibilityIdentifier("routine-schedule")
                Toggle("Enabled", isOn: $enabled).tint(BoardStyle.green).accessibilityIdentifier("routine-enabled")
            } header: { Text("Schedule") } footer: {
                Text("Use a Hermes schedule, such as ‘weekdays at 8am’, ‘every 30m’, or a cron expression. Times follow this bot’s Hermes time zone.")
            }
            if let current {
                if let error = current.error { Section { InlineError(error) } }
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
            if !windowShowsError, let error = store.error { Section { InlineError(error) } }
        }
        .settingsBackground()
        .navigationTitle(routine == nil ? "New routine" : "Routine").inlineNavigationTitle()
        .toolbar {
            if routine == nil { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            ToolbarItem(placement: .confirmationAction) { Button(saving ? "Saving…" : "Save", action: save).disabled(!canSave).accessibilityIdentifier("save-routine") }
        }
        .onAppear {
            guard !loaded else { return }
            loaded = true
            if let current { name = current.name; prompt = current.prompt; schedule = current.cron; enabled = current.enabled }
        }
        .onChange(of: current) { previous, current in
            guard loaded, let previous, let current else { return }
            if name == previous.name { name = current.name }
            if prompt == previous.prompt { prompt = current.prompt }
            if schedule == previous.cron { schedule = current.cron }
            if enabled == previous.enabled { enabled = current.enabled }
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

/// Asks before a bot is deleted. The bot list opens it from the secondary click on a Mac and the long press on an iPhone.
struct DeleteBotDialog: ViewModifier {
    @Environment(AppStore.self) private var store
    @Binding var bot: Bot?

    func body(content: Content) -> some View {
        content.confirmationDialog("Delete \(bot?.name ?? "bot")?",
                                   isPresented: Binding(get: { bot != nil }, set: { if !$0 { bot = nil } }),
                                   titleVisibility: .visible, presenting: bot) { bot in
            Button("Delete bot", role: .destructive) { Task { await store.deleteBot(bot) } }
                .accessibilityIdentifier("confirm-delete-bot")
        } message: { _ in
            Text("The Mac cancels this bot's work and removes its conversation. The audit record remains on your Mac.")
        }
    }
}
