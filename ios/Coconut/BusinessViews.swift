import SwiftUI
import MarkdownUI

struct BusinessHomeView: View {
    @Environment(AppStore.self) private var store
    @State private var report: BusinessReport?
    @State private var error: String?
    private var shownReport: BusinessReport? { store.businessReports["overview"] ?? report }

    init(initial: BusinessReport? = nil) { _report = State(initialValue: initial) }

    var body: some View {
        // One page with no scrolling (DashboardPage): the to-dos of Finance first, which the badge of the tab counts
        // (TodoLists.swift), then the tiles of the business. Each tile opens its detail.
        TimelineView(.periodic(from: .now, by: 60)) { context in
            DashboardPage {
                FinanceTiles(model: shownReport.map { BusinessHomePresentation(report: $0, now: context.date, refreshFailed: error != nil) },
                             error: error) { Task { await load() } }
            }
        }
        .navigationTitle("Finance")
        // No + here: the person found that a + made no sense on Finance (2026-11-24). An agent files a to-do only when
        // it gets blocked (github_todos.py), and the message box of the tab writes to the agent of the business.
        .task(id: "\(store.connection)/\(store.businessChanges)") { await load() }
        .onChange(of: store.businessGeneration) { _, _ in report = nil; error = nil }
        .refreshable {
            await store.loadTodoIssues(fresh: true)
            await store.loadRiskReviews(fresh: true)
            await load()
        }
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
        case .todos(let area): TodoListPage(area: area)
        case .conversations(let queue): SupportDetailView(queue: queue, part: .conversations)
        case .resolution(let queue): SupportDetailView(queue: queue, part: .resolution)
        case .csat: SupportCsatView()
        }
        }.id(store.businessGeneration)
    }
}

struct BusinessProjectsView: View {
    @Environment(AppStore.self) private var store
    @State private var adding = false
    @State private var error: String?

    var body: some View {
        BoardPage {
            // The repositories of the business that Coconut follows. Projects are Coconut, Gumroad and Personal.
            BoardSection {
                ForEach(store.businessCatalog?.projects ?? []) { project in
                    NavigationLink(value: BusinessRoute.project(project.key)) {
                        BoardRow(symbol: "shippingbox", title: project.displayName, detail: project.fullName)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("business-project-" + project.key)
                }
                if store.businessCatalog?.projects.isEmpty == true { BoardEmptyRow(text: "No repositories.") }
            }
            if let error { BusinessConnectionNotice(message: error) }
        }
        .navigationTitle("Repositories")
        .toolbar { ToolbarItem(placement: .primaryAction) { Button { adding = true } label: { Label("Add repository", systemImage: "plus") }.accessibilityIdentifier("business-add-project") } }
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
    /// The open work only. The person found that nobody taps a control for the closed work (2026-11-24), and a closed
    /// issue is done.
    private let state = "open"
    @State private var error: String?
    @State private var loading = false

    private var shownItems: [BusinessWork] { store.cachedBusinessWork(function: function, project: project, state: state) }
    private var title: String {
        if let function { return BusinessFunction.name(function) }
        return store.businessCatalog?.projects.first { $0.key == project }?.displayName ?? project ?? "Work"
    }

    var body: some View {
        BoardPage {
            if loading && shownItems.isEmpty { BoardLoading(title: "Loading work", height: 200) }
            BoardSection("Work", count: shownItems.count) {
                // A row opens its issue in Coconut: its comments, a box to answer, and Close or Reopen (GitHubItemView).
                ForEach(shownItems) { item in
                    NavigationLink(value: route(item)) {
                        BoardRow(symbol: "circle.dotted", title: item.title, detail: subtitle(item))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("business-work-" + item.id)
                }
                if !loading && shownItems.isEmpty && error == nil {
                    BoardEmptyRow(text: "No open work.")
                }
            }
            if let error { BusinessConnectionNotice(message: error) }
        }
        .navigationTitle(title)
        // No "New work": work of the business is a to-do, and the person removed to-do creation from Coconut
        // (2026-11-26). An agent files one only when it gets blocked (github_todos.py).
        .task(id: "\(function ?? "")/\(project ?? "")/\(state)/\(store.businessChanges)") { await load() }
        .refreshable { await load(fresh: true) }
    }

    /// The issue of a row of work, in the work repository of its organization.
    private func route(_ item: BusinessWork) -> GitHubItemRoute {
        URL(string: item.url).flatMap(InAppLink.route) as? GitHubItemRoute
            ?? GitHubItemRoute(repository: store.todoIssues?.repository ?? "antiwork/gumroad-private", number: item.number)
    }

    /// "Support · gumroad · #412": the function or the repository, whichever the page is not already about.
    private func subtitle(_ item: BusinessWork) -> String {
        [function == nil ? BusinessFunction.name(item.functionKey) : nil,
         project == nil ? item.projectKey : nil, "#\(item.number)"].compactMap { $0 }.joined(separator: " · ")
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

private struct BusinessConnectionNotice: View {
    @Environment(AppStore.self) private var store
    let message: String
    @State private var connecting = false
    var body: some View {
        BoardSection("Connection") {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                Text(message).font(.subheadline).foregroundStyle(.secondary)
                Button("GitHub connection") { connecting = true }.font(.subheadline)
            }
            .padding(BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
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
            .navigationTitle("Add repository").inlineNavigationTitle().settingsBackground()
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
