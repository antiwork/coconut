import SwiftUI

// The routines of an agent, and its scheduled checks. The page has the title "Routines", so its first section needs no
// header. Next to a chat on the Mac (MacBotDetails) it lists the pages of the agent too, in the same list with no tabs
// between them, because the person found that nobody taps such tabs (2026-11-24); the routines then get a header.
struct BotSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    let botId: String
    /// Lists the pages of the agent under its routines. A row opens the page (DocumentRoute).
    var showsPages = false
    @State private var routineOpen = false

    /// The pages of the agent, the starred first, then the newest update first, as on the page of the agent.
    private var pages: [BotDocument] {
        (store.documents ?? []).filter { $0.botId == botId }
            .sorted { ($0.isPinned ? 1 : 0, $0.updatedAt, $1.id) > ($1.isPinned ? 1 : 0, $1.updatedAt, $0.id) }
    }

    var body: some View {
        Form {
            let groups = RoutineGroups(store.routines.filter { $0.botId == botId })
            Section {
                ForEach(groups.recurring) { routine in
                    routineLink(routine, detail: routine.error ?? routineLabel(routine))
                }
                Button("Add routine", systemImage: "plus") { routineOpen = true }.accessibilityIdentifier("add-routine")
            } header: {
                if showsPages { Text("Routines") }
            }
            if !groups.scheduledChecks.isEmpty {
                Section("Scheduled checks") {
                    ForEach(groups.scheduledChecks) { routine in
                        routineLink(routine, detail: routine.error ?? checkLabel(routine))
                    }
                }
            }
            if showsPages {
                Section("Pages") {
                    if pages.isEmpty { Text("No pages.").foregroundStyle(.secondary) }
                    ForEach(pages) { page in
                        NavigationLink(value: DocumentRoute(id: page.id)) {
                            Label(page.title, systemImage: page.isPinned ? "star.fill" : page.symbol).lineLimit(1)
                        }
                        .accessibilityIdentifier("bot-page-" + page.id)
                    }
                }
            }
            if let error = shownError { Section { InlineError(error) } }
        }
        .settingsBackground()
        .navigationTitle("Routines").inlineNavigationTitle()
        // The pages need the list of the pages, also before any page opens.
        .task(id: showsPages && store.connection == .connected) {
            if showsPages, store.documents == nil { await store.loadDocuments(quiet: true) }
        }
        // A sheet has no notice over it, so it shows the error itself.
        .sheet(isPresented: $routineOpen) {
            NavigationStack { RoutineEditorView(botId: botId) }.sheetShowsError()
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
}

struct RoutineEditorView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
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
                    Text("Runs on the Mac without model tokens. Ask this agent to change the script.")
                }
            } else {
                Section("Instructions") {
                    TextField("What should this agent do?", text: $prompt, axis: .vertical)
                        .fieldUnderHeader()
                        .lineLimit(4...12).accessibilityIdentifier("routine-prompt")
                }
                if let check = current?.scriptCheck {
                    Section {
                        LabeledContent("Script check", value: check).accessibilityIdentifier("routine-script-check")
                    } footer: {
                        Text("This script runs first, without model tokens. The agent runs only when the script finds work. An edit here keeps the script.")
                    }
                }
            }
            Section {
                TextField("Weekdays at 8am", text: $schedule)
                    .noAutocapitalization().autocorrectionDisabled()
                    .accessibilityIdentifier("routine-schedule")
                Toggle("Enabled", isOn: $enabled).tint(BoardStyle.green).accessibilityIdentifier("routine-enabled")
            } header: { Text("Schedule") } footer: {
                Text("Use a Hermes schedule, such as ‘weekdays at 8am’, ‘every 30m’, or a cron expression. Times follow this agent’s Hermes time zone.")
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
            if let error = shownError { Section { InlineError(error) } }
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
        content.confirmationDialog(bot?.isChat == true ? "Delete this agent?" : "Delete \(bot?.name ?? "agent")?",
                                   isPresented: Binding(get: { bot != nil }, set: { if !$0 { bot = nil } }),
                                   titleVisibility: .visible, presenting: bot) { bot in
            Button("Delete agent", role: .destructive) { Task { await store.deleteBot(bot) } }
                .accessibilityIdentifier("confirm-delete-bot")
        } message: { bot in
            Text(bot.isChat
                 ? "The Mac cancels the work of this agent and removes its conversation. \(bot.name) and its other agents stay."
                 : "The Mac cancels this agent's work and removes its conversation. The audit record remains on your Mac.")
        }
    }
}
