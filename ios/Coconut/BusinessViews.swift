import SwiftUI
import MarkdownUI

struct BusinessHomeView: View {
    @Environment(AppStore.self) private var store
    @State private var report: BusinessReport?
    @State private var error: String?
    private var shownReport: BusinessReport? { store.businessReports["overview"] ?? report }

    init(initial: BusinessReport? = nil) { _report = State(initialValue: initial) }

    var body: some View {
        BoardPage(width: .wide, spacing: BoardStyle.Space.l) {
            if let report = shownReport {
                TimelineView(.periodic(from: .now, by: 60)) { context in
                    BusinessHomeContent(model: BusinessHomePresentation(report: report, now: context.date, refreshFailed: error != nil))
                }
            } else if error == nil {
                BoardLoading(title: "Loading business")
            }
            if let error {
                BoardLoadNotice(message: error) { Task { await load() } }
                    .accessibilityIdentifier("business-home-refresh-error")
            }
        }
        .navigationTitle("Business")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    ForEach(BusinessFunction.builtIns) { function in
                        NavigationLink(value: BusinessRoute.function(function.key)) {
                            Label(function.name, systemImage: function.symbol)
                        }.accessibilityIdentifier("business-function-" + function.key)
                    }
                    NavigationLink(value: BusinessRoute.reports) { Label("Reports", systemImage: "chart.bar.doc.horizontal") }
                        .accessibilityIdentifier("business-reports-link")
                    NavigationLink(value: BusinessRoute.projects) { Label("Projects", systemImage: "square.stack.3d.up") }
                } label: { Label("Functions", systemImage: "square.grid.2x2") }
                .accessibilityIdentifier("business-home-functions")
            }
        }
        .task(id: "\(store.connection)/\(store.businessChanges)") { await load() }
        .onChange(of: store.businessGeneration) { _, _ in report = nil; error = nil }
        .refreshable { await load() }
    }

    private func load() async {
        do {
            let loaded = try await store.businessReport("overview")
            guard !Task.isCancelled else { return }
            report = loaded; error = nil
            try? await store.loadBusinessCatalog()
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}

struct BusinessDestinationView: View {
    @Environment(AppStore.self) private var store
    let route: BusinessRoute
    var body: some View {
        Group {
        switch route {
        case .home: BusinessHomeView()
        case .projects: BusinessProjectsView()
        case .reports: BusinessReportsView()
        case .function(let key): BusinessWorkspaceView(function: key)
        case .project(let key): BusinessWorkListView(project: key)
        case .report(let key): BusinessReportView(key: key)
        case .ledger: BusinessLedgerView()
        }
        }.id(store.businessGeneration)
    }
}

struct BusinessProjectsView: View {
    @Environment(AppStore.self) private var store
    @State private var adding = false
    @State private var error: String?

    var body: some View {
        List {
            ForEach(store.businessCatalog?.projects ?? []) { project in
                NavigationLink(value: BusinessRoute.project(project.key)) {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                        Text(project.displayName)
                        Text(project.fullName).font(.caption).foregroundStyle(.secondary)
                    }
                }.accessibilityIdentifier("business-project-" + project.key)
            }
            if store.businessCatalog?.projects.isEmpty == true {
                Section { Text("No projects yet.").foregroundStyle(.secondary) }
            }
            if let error { BusinessConnectionNotice(message: error) }
        }
        .navigationTitle("Projects").settingsBackground()
        .toolbar { ToolbarItem(placement: .primaryAction) { Button { adding = true } label: { Label("Add project", systemImage: "plus") }.accessibilityIdentifier("business-add-project") } }
        .task(id: store.businessChanges) { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $adding) { BusinessProjectEditor() }
    }

    private func load() async {
        do { try await store.loadBusinessCatalog(fresh: true); error = nil }
        catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}

struct BusinessWorkListView: View {
    @Environment(AppStore.self) private var store
    var function: String? = nil
    var project: String? = nil
    @State private var state = "open"
    @State private var error: String?
    @State private var loading = false
    @State private var adding = false

    private var shownItems: [BusinessWork] { store.cachedBusinessWork(function: function, project: project, state: state) }
    private var title: String {
        if let function { return BusinessFunction.name(function) }
        return store.businessCatalog?.projects.first { $0.key == project }?.displayName ?? project ?? "Work"
    }
    private var repository: BusinessRepository? {
        if let function { return store.businessCatalog?.functions.first { $0.key == function }?.repository }
        return store.businessCatalog?.projects.first { $0.key == project }
    }

    var body: some View {
        List {
            Section {
                Picker("Work", selection: $state) {
                    Text("Open").tag("open")
                    Text("Completed").tag("closed")
                }.pickerStyle(.segmented)
                if let repository, let url = URL(string: repository.url) {
                    Link(destination: url) { Label(repository.fullName, systemImage: "folder") }.font(.caption)
                }
            }
            if loading && shownItems.isEmpty { ProgressView("Loading work") }
            ForEach(shownItems) { item in
                NavigationLink { BusinessWorkDetailView(work: item) } label: {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                        Text(item.title).foregroundStyle(.primary)
                        Text([function == nil ? BusinessFunction.name(item.functionKey) : nil,
                              project == nil ? item.projectKey : nil, "#\(item.number)"].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, BoardStyle.Space.xs)
                }.accessibilityIdentifier("business-work-" + item.id)
            }
            if !loading && shownItems.isEmpty && error == nil {
                Section { Text(state == "open" ? "No open work yet." : "No completed work yet.").foregroundStyle(.secondary) }
            }
            if let error { BusinessConnectionNotice(message: error) }
        }
        .navigationTitle(title).settingsBackground()
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { adding = true } label: { Label("New work", systemImage: "plus") }
                    .disabled(store.businessCatalog == nil).accessibilityIdentifier("business-new-work")
            }
        }
        .task(id: "\(function ?? "")/\(project ?? "")/\(state)/\(store.businessChanges)") { await load() }
        .refreshable { await load(fresh: true) }
        .sheet(isPresented: $adding) { BusinessWorkEditor(function: function, project: project) }
    }

    private func load(fresh: Bool = false) async {
        loading = true
        defer { loading = false }
        do {
            try await store.loadBusinessCatalog(fresh: fresh)
            _ = try await store.businessWork(function: function, project: project, state: state)
            guard !Task.isCancelled else { return }
            error = nil
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}

struct BusinessWorkDetailView: View {
    @Environment(AppStore.self) private var store
    @State var work: BusinessWork
    private var shownWork: BusinessWork { store.businessItems[work.id] ?? work }
    @State private var busy = false
    @State private var error: String?
    @State private var asking = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xl) {
                Text(shownWork.title).font(.title2.bold())
                Text([BusinessFunction.name(shownWork.functionKey), shownWork.projectKey, shownWork.isOpen ? "Open" : "Completed"].compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(.secondary)
                if !shownWork.body.isEmpty { Markdown(shownWork.body).textSelection(.enabled) }
                if let error { Text(error).foregroundStyle(BoardStyle.destructive) }
                Button(busy ? "Saving…" : shownWork.isOpen ? "Complete work" : "Reopen work") {
                    busy = true
                    Task {
                        defer { busy = false }
                        do { work = try await store.setBusinessWork(shownWork, state: shownWork.isOpen ? "closed" : "open"); error = nil }
                        catch { self.error = error.localizedDescription }
                    }
                }.disabled(busy).accessibilityIdentifier("business-work-state")
                Button("Ask an agent", systemImage: "bubble.left.and.bubble.right") { asking = true }
                    .disabled(store.bots.allSatisfy(\.archived)).accessibilityIdentifier("business-ask-agent")
                if let url = URL(string: shownWork.url) { Link("Open on GitHub", destination: url).font(.subheadline) }
            }.frame(maxWidth: 720, alignment: .leading).padding(BoardStyle.Space.xl).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(BoardStyle.paper).navigationTitle("Work").inlineNavigationTitle()
        .sheet(isPresented: $asking) { BusinessAgentSheet(work: work) }
    }
}

private struct BusinessConnectionNotice: View {
    @Environment(AppStore.self) private var store
    let message: String
    @State private var connecting = false
    var body: some View {
        Section {
            Text(message).font(.subheadline).foregroundStyle(.secondary)
            Button("GitHub connection") { connecting = true }
        }
        .sheet(isPresented: $connecting, onDismiss: { store.businessChanges += 1 }) {
            NavigationStack {
                GitHubSettingsView().toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { connecting = false } } }
            }
        }
    }
}

private struct BusinessProjectEditor: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var selected = ""
    @State private var busy = false
    @State private var error: String?
    private var candidates: [BusinessRepository] {
        let existing = Set(store.businessCatalog?.projects.map(\.key) ?? [])
        return (store.businessCatalog?.repositories ?? []).filter { !existing.contains($0.key) }
    }
    var body: some View {
        NavigationStack {
            Form {
                Picker("Repository", selection: $selected) {
                    Text("Choose a repository").tag("")
                    ForEach(candidates) { Text($0.fullName).tag($0.key) }
                }.accessibilityIdentifier("business-project-repository")
                if let error { Text(error).foregroundStyle(BoardStyle.destructive) }
            }
            .navigationTitle("Add project").inlineNavigationTitle().settingsBackground()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(busy ? "Adding…" : "Add") {
                    busy = true
                    Task {
                        defer { busy = false }
                        do { try await store.addBusinessProject(selected); dismiss() }
                        catch { self.error = error.localizedDescription }
                    }
                }.disabled(selected.isEmpty || busy) }
            }
            .task { do { try await store.loadBusinessCatalog(fresh: true) } catch { self.error = error.localizedDescription } }
        }.frame(minWidth: 340, minHeight: 260)
    }
}

private struct BusinessWorkEditor: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let function: String?
    let project: String?
    @State private var selectedFunction = "accounting"
    @State private var selectedProject = ""
    @State private var title = ""
    @State private var bodyText = ""
    @State private var requestId = UUID().uuidString
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            Form {
                if function == nil {
                    Picker("Function", selection: $selectedFunction) {
                        ForEach(BusinessFunction.builtIns) { Text($0.name).tag($0.key) }
                    }
                }
                if project == nil {
                    Picker("Project", selection: $selectedProject) {
                        Text("Across projects").tag("")
                        ForEach(store.businessCatalog?.projects ?? []) { Text($0.displayName).tag($0.key) }
                    }
                }
                TextField("Title", text: $title).accessibilityIdentifier("business-work-title")
                TextField("Description", text: $bodyText, axis: .vertical).lineLimit(5...12)
                    .accessibilityIdentifier("business-work-description")
                Section {} footer: { Text("Saved to \(store.businessCatalog?.owner ?? "")/\(function ?? selectedFunction) on GitHub.") }
                if let error { Text(error).foregroundStyle(BoardStyle.destructive) }
            }
            .navigationTitle("New work").inlineNavigationTitle().settingsBackground()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(busy ? "Saving…" : "Create") {
                    busy = true
                    Task {
                        defer { busy = false }
                        do {
                            let _ = try await store.createBusinessWork(function: function ?? selectedFunction, project: project ?? (selectedProject.isEmpty ? nil : selectedProject), title: title, body: bodyText, requestId: requestId)
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy).accessibilityIdentifier("business-create-work") }
            }
        }.frame(minWidth: 360, minHeight: 380)
    }
}

private struct BusinessAgentSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    let work: BusinessWork
    @State private var selected = ""
    @State private var instruction = ""
    @State private var busy = false
    var body: some View {
        NavigationStack {
            Form {
                Picker("Agent", selection: $selected) {
                    Text("Choose an agent").tag("")
                    ForEach(store.bots.withoutChats.filter { !$0.archived }) { Text($0.name).tag($0.id) }
                }
                TextField("Instructions (optional)", text: $instruction, axis: .vertical).lineLimit(3...6)
            }
            .navigationTitle("Ask an agent").inlineNavigationTitle().settingsBackground()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(busy ? "Sending…" : "Send") {
                    guard let bot = store.bots.first(where: { $0.id == selected }) else { return }
                    busy = true
                    Task {
                        defer { busy = false }
                        let scope = [BusinessFunction.name(work.functionKey), work.projectKey].compactMap { $0 }.joined(separator: " · ")
                        let text = "Work on this \(scope) issue: \(work.url)\n\n\(instruction)\n\nKeep the work and results in this GitHub issue and its function repository. Use its project label to retain the project context."
                        if await store.send(bot: bot, text: text, mode: "queue") {
                            dismiss()
                            if let openChat { openChat(bot.id) } else { pushRoute?(ConversationRoute(botId: bot.id)) }
                        }
                    }
                }.disabled(selected.isEmpty || busy) }
            }
        }.frame(minWidth: 360, minHeight: 300)
    }
}
