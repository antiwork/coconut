import SwiftUI

struct TrackedPullRequest: Identifiable {
    let artifact: Artifact
    let url: URL
    let repository: String
    let number: String
    let botIds: [String]
    var id: String { url.absoluteString.lowercased() }

    var isFinished: Bool { artifact.status == "merged" || ["closed", "merged"].contains(artifact.prState ?? "") }

    static func active(in artifacts: [Artifact]) -> [Self] {
        tracked(in: artifacts).filter { !$0.isFinished }
    }

    static func tracked(in artifacts: [Artifact], visibility: [String: Bool] = [:]) -> [Self] {
        let grouped = Dictionary(grouping: artifacts.filter { $0.kind == "pr" && location($0.url) != nil }) {
            location($0.url)!.url.absoluteString.lowercased()
        }
        return grouped.compactMap { url, copies in
            guard !(visibility[url] ?? copies.contains { $0.workDismissalId != nil }) else { return nil }
            guard let latest = copies.max(by: {
                let first = (($0.observedAt ?? $0.createdAt).apiDate ?? .distantPast, ($0.checkedAt ?? $0.createdAt).apiDate ?? .distantPast, $0.id)
                let second = (($1.observedAt ?? $1.createdAt).apiDate ?? .distantPast, ($1.checkedAt ?? $1.createdAt).apiDate ?? .distantPast, $1.id)
                return first < second
            }), let location = location(latest.url) else { return nil }
            return Self(artifact: latest, url: location.url, repository: location.repository,
                        number: location.number, botIds: Array(Set(copies.map(\.botId))).sorted())
        }.sorted {
            if $0.isFinished != $1.isFinished { return !$0.isFinished }
            return ($0.artifact.createdAt, $0.id) > ($1.artifact.createdAt, $1.id)
        }
    }

    private static func location(_ raw: String?) -> (url: URL, repository: String, number: String)? {
        guard let raw, let url = URL(string: raw), url.scheme == "https", url.host == "github.com",
              url.user == nil, url.password == nil, url.port == nil, url.query == nil, url.fragment == nil else { return nil }
        let parts = url.path.split(separator: "/")
        guard parts.count == 4, parts[2] == "pull", let number = Int(parts[3]), number > 0 else { return nil }
        guard let canonical = URL(string: "https://github.com/\(parts[0])/\(parts[1])/pull/\(number)") else { return nil }
        return (canonical, String(parts[1]), String(number))
    }

    func isStale(at date: Date) -> Bool {
        artifact.refreshError != nil || artifact.observedAt?.apiDate.map { date.timeIntervalSince($0) > 120 } != false
    }

    func activityLabel(at date: Date) -> String? {
        guard !isFinished, let label = artifact.activity else { return nil }
        guard let observed = artifact.activityObservedAt?.apiDate,
              date.timeIntervalSince(observed) < 60 else { return "Activity unconfirmed" }
        return label
    }

    func checksColor(at date: Date, scheme: ColorScheme) -> Color {
        if artifact.prState == "merged" || artifact.status == "merged" { return .purple }
        if artifact.prState == "closed" { return .secondary }
        guard !isStale(at: date) else { return .secondary }
        switch artifact.checksState {
        case "passed": return scheme == .dark ? .green : Color(red: 0.12, green: 0.44, blue: 0.22)
        case "running": return scheme == .dark ? .yellow : Color(red: 0.50, green: 0.36, blue: 0)
        case "failed": return scheme == .dark ? .red : Color(red: 0.75, green: 0.10, blue: 0.10)
        default: return .secondary
        }
    }

    func checksLabel(at date: Date) -> String {
        if artifact.prState == "merged" || artifact.status == "merged" { return "Merged" }
        if artifact.prState == "closed" { return "Closed" }
        guard !isStale(at: date) else { return "Status out of date" }
        switch artifact.checksState {
        case "failed": return "Checks failed"
        case "running": return "Checks running"
        case "passed": return "Checks passed"
        case "none": return "No checks"
        default: return "Checking status"
        }
    }
}

struct WorkView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let isVisible: Bool
    let openConversation: (ConversationRoute) -> Void
    @State private var requestingUpdates: Set<String> = []
    @State private var showCompleted = false
    @State private var cancellingItems: Set<String> = []
    @State private var orderingDate = Date()
    private var items: [TrackedWorkItem] { TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility, at: orderingDate) }

    @State private var displayedOrder: [String] = []
    @State private var isInteracting = false

    private var proposedOrder: [String] {
        var result: [String] = []
        let active = items.filter { !$0.isFinished }
        result += active.map(\.id)
        if active.isEmpty { result.append("empty") }
        if items.contains(where: \.isFinished) {
            result.append("completed-toggle")
            if showCompleted { result += items.filter(\.isFinished).map(\.id) }
        }
        return result
    }

    private func updateOrder(animated: Bool = true) {
        guard !isInteracting else { return }
        withAnimation(animated && !reduceMotion ? .spring(response: 0.35, dampingFraction: 1) : nil) {
            displayedOrder = proposedOrder
        }
    }

    var body: some View {
        List {
            ForEach(displayedOrder, id: \.self) { identity in
                if identity == "empty" {
                    Text("All caught up")
                        .font(.body).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 220)
                        .listRowBackground(BoardStyle.paper).listRowSeparator(.hidden)
                        .accessibilityIdentifier("work-empty")
                } else if identity == "completed-toggle" {
                    Button { showCompleted.toggle() } label: {
                        HStack {
                            Text("Completed")
                            Spacer()
                            Image(systemName: "chevron.right").rotationEffect(.degrees(showCompleted ? 90 : 0))
                        }.frame(minHeight: 44)
                    }
                    .font(.subheadline).foregroundStyle(.secondary)
                    .listRowBackground(BoardStyle.paper).listRowSeparator(.hidden)
                    .listRowInsets(EdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24))
                    .accessibilityIdentifier("work-completed")
                    .accessibilityValue(showCompleted ? "Expanded" : "Collapsed")
                } else if let item = items.first(where: { $0.id == identity }) {
                    row(item)
                }
            }
        }
        .onAppear { updateOrder(animated: false) }
        .onChange(of: proposedOrder) { updateOrder() }
        .onScrollPhaseChange { _, phase in
            isInteracting = phase != .idle
            if !isInteracting { updateOrder() }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .accessibilityIdentifier("work-list")
        .toolbar {
            if #available(iOS 26, *) {
                ToolbarItem(placement: .topBarTrailing) { undoButton }.sharedBackgroundVisibility(.hidden)
            } else {
                ToolbarItem(placement: .topBarTrailing) { undoButton }
            }
        }
        .onDisappear { store.workUndo = []; isInteracting = false }
        .background(BoardStyle.paper)
        .refreshable { await store.refreshWork() }
        .task(id: isVisible && scenePhase == .active) {
            guard isVisible && scenePhase == .active else { return }
            while !Task.isCancelled {
                await store.refreshWork()
                orderingDate = Date()
                do { try await Task.sleep(for: .seconds(15)) }
                catch { return }
            }
        }
    }

    private var undoButton: some View {
        Button("Undo") {
            withAnimation(reduceMotion ? nil : .smooth(duration: 0.22)) { store.undoWorkDismissal() }
        }
        .font(.subheadline.weight(.medium)).frame(minWidth: 44, minHeight: 44)
        .opacity(store.workUndo.isEmpty ? 0 : 1)
        .disabled(store.workUndo.isEmpty)
        .accessibilityHidden(store.workUndo.isEmpty)
        .accessibilityIdentifier("work-undo")
    }

    private func openWork(_ artifact: Artifact, attach: Bool = true) {
        let botId = artifact.activityBotId ?? artifact.botId
        openConversation(ConversationRoute(botId: botId,
            workContext: attach ? WorkReference(id: artifact.id, title: artifact.title) : nil))
    }

    private func getUpdate(_ artifact: Artifact) {
        let botId = artifact.activityBotId ?? artifact.botId
        guard !requestingUpdates.contains(artifact.id), let bot = store.bots.first(where: { $0.id == botId }) else { return }
        requestingUpdates.insert(artifact.id)
        openWork(artifact, attach: false)
        Task {
            defer { requestingUpdates.remove(artifact.id) }
            await store.send(bot: bot, text: "How is this going?",
                workContext: WorkReference(id: artifact.id, title: artifact.title), workStatusOnly: true)
        }
    }

    private func row(_ item: TrackedWorkItem) -> some View {
        workRow(item)
            .contextMenu { workActions(item) }
            .listRowInsets(EdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24))
            .listRowBackground(BoardStyle.paper)
            .listRowSeparator(.hidden)
            .navigationLinkIndicatorVisibility(.hidden)
            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                if item.isFinished {
                    Button(role: .destructive) { store.dismissWork(item) } label: {
                        Label("Dismiss", systemImage: "xmark")
                    }.tint(.gray)
                }
            }
    }

    @ViewBuilder
    private func workActions(_ item: TrackedWorkItem) -> some View {
        Button("Ask for progress", systemImage: "arrow.clockwise") { getUpdate(item.artifact) }
            .disabled(requestingUpdates.contains(item.artifact.id))
        if let pr = item.pullRequest {
            Link(destination: pr.url) { Label("Open pull request", systemImage: "arrow.up.right") }
        }
        if item.artifact.workState == "cancelled" {
            Button("Resume work", systemImage: "play") { Task { await store.resumeWork(item) } }
        } else if !item.isFinished {
            Button("Cancel work", systemImage: "stop.circle", role: .destructive) {
                guard cancellingItems.insert(item.id).inserted else { return }
                Task {
                    defer { cancellingItems.remove(item.id) }
                    await store.cancelWork(item)
                }
            }
            .disabled(cancellingItems.contains(item.id))
            .accessibilityIdentifier("work-cancel-\(item.artifact.id)")
        }
    }

    private func decision(for item: TrackedWorkItem) -> Decision? {
        guard item.needsDecision, let identity = item.artifact.workDecisionId else { return nil }
        return store.decisions.first { $0.id == identity && $0.isPending }
    }

    private func statusLabel(_ item: TrackedWorkItem, at date: Date) -> String {
        if let decision = decision(for: item) { return decision.title }
        let botName = store.bots.first { $0.id == (item.artifact.activityBotId ?? item.artifact.botId) }?.name ?? "this bot"
        let label = item.activity(at: date)
        guard label != "Status unavailable" else { return label }
        if let queue = item.queueLabel(botName: botName) { return queue }
        if label == "In progress" { return "Working on the next step" }
        if label == "Needs recovery" { return "Stopped before finishing" }
        return label
    }

    private func workRow(_ item: TrackedWorkItem) -> some View {
        HStack(alignment: .top, spacing: 4) {
        Button {
            if let decision = decision(for: item) {
                openConversation(store.decisionChatRoute(for: decision))
            } else {
                openWork(item.artifact)
            }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(item.artifact.title).font(.body.weight(.medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    TimelineView(.periodic(from: .now, by: 15)) { context in
                        HStack(alignment: .center, spacing: 6) {
                            if item.isWorking(at: context.date) {
                                ProgressView().controlSize(.mini).frame(width: 14, height: 14)
                                    .accessibilityIdentifier("work-progress-\(item.artifact.id)")
                            } else if item.isFinished {
                                Image(systemName: item.artifact.workState == "cancelled" ? "stop.circle" : "checkmark")
                            }
                            Text(statusLabel(item, at: context.date))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .font(.caption)
                        .foregroundStyle(decision(for: item) != nil ? Color.accentColor : .secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if decision(for: item) != nil {
                    Image(systemName: "chevron.right").font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }
            .padding(.vertical, 16)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint(decision(for: item) != nil ? "Opens the decision card" : "Discusses this work with its context attached")
        .accessibilityIdentifier(item.pullRequest.map { "work-pr-\($0.number)" } ?? "work-item-\(item.artifact.id)")
        .accessibilityAction(named: "Ask for progress") { getUpdate(item.artifact) }
        Menu { workActions(item) } label: {
            Image(systemName: "ellipsis")
                .font(.body).foregroundStyle(.secondary)
                .frame(width: 44, height: 44).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.top, 4)
        .accessibilityLabel("Work options")
        .accessibilityIdentifier("work-menu-\(item.artifact.id)")
        }
        .overlay(alignment: .bottom) {
            Rectangle().fill(.primary.opacity(0.10)).frame(height: 0.5).accessibilityHidden(true)
        }
    }
}
