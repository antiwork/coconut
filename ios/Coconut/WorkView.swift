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
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let isVisible: Bool
    let openConversation: (ConversationRoute) -> Void
    @State private var requestingUpdates: Set<String> = []
    private var items: [TrackedWorkItem] { TrackedWorkItem.tracked(in: store.artifacts, visibility: store.workVisibility) }

    var body: some View {
        List {
            if items.isEmpty {
                Text("No tracked work")
                    .font(.body).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 220)
                    .listRowBackground(BoardStyle.paper)
                    .listRowSeparator(.hidden)
                    .accessibilityIdentifier("work-empty")
            }
            ForEach(items) { item in
                workRow(item)
                    .contextMenu {
                        Button("Get update", systemImage: "arrow.clockwise") { getUpdate(item.artifact) }
                            .disabled(requestingUpdates.contains(item.artifact.id))
                        if let pr = item.pullRequest {
                            Link(destination: pr.url) { Label("Open pull request", systemImage: "arrow.up.right") }
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24))
                    .listRowBackground(BoardStyle.paper)
                    .listRowSeparator(.hidden)
                    .navigationLinkIndicatorVisibility(.hidden)
                    .alignmentGuide(.listRowSeparatorLeading) { _ in 0 }
                    .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                        if item.isFinished {
                            Button {
                                withAnimation(reduceMotion ? nil : .smooth(duration: 0.22)) { store.dismissWork(item) }
                            } label: { Label("Dismiss", systemImage: "xmark") }
                            .tint(.gray)
                        }
                    }
            }
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
        .onDisappear { store.workUndo = [] }
        .background(BoardStyle.paper)
        .refreshable { await store.refreshWork() }
        .task(id: isVisible && scenePhase == .active) {
            guard isVisible && scenePhase == .active else { return }
            while !Task.isCancelled {
                await store.refreshWork()
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

    @ViewBuilder
    private func workRow(_ item: TrackedWorkItem) -> some View {
        Button { openWork(item.artifact) } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(item.artifact.title).font(.body.weight(.semibold))
                    .modifier(WorkTextLines())
                    .foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                TimelineView(.periodic(from: .now, by: 15)) { context in
                    if let pr = item.pullRequest {
                        HStack(spacing: 6) {
                            Text("\(pr.repository) #\(pr.number)" + (!pr.isFinished && pr.artifact.draft == true ? " · Draft" : ""))
                                .foregroundStyle(.secondary)
                            Text("·").foregroundStyle(.tertiary).accessibilityHidden(true)
                            Text(pr.checksLabel(at: context.date))
                                .foregroundStyle(pr.checksColor(at: context.date, scheme: colorScheme))
                                .layoutPriority(1)
                        }.font(.caption).lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if !pr.isFinished {
                            activity(pr.activityLabel(at: context.date) ?? "No activity reported", item: item, at: context.date)
                        }
                    } else {
                        activity(item.activity(at: context.date), item: item, at: context.date)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 12)
            .overlay(alignment: .bottom) {
                Rectangle().fill(.primary.opacity(0.10)).frame(height: 0.5)
                    .accessibilityHidden(true)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityHint("Opens the bot conversation")
        .accessibilityIdentifier(item.pullRequest.map { "work-pr-\($0.number)" } ?? "work-item-\(item.artifact.id)")
    }

    private func activity(_ label: String, item: TrackedWorkItem, at date: Date) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(label).font(.caption)
                .foregroundStyle(item.color(at: date, scheme: colorScheme))
                .modifier(WorkTextLines())
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            // Reserve the indicator's space while activity changes.
            ZStack {
                if item.isWorking(at: date) {
                    ProgressView().controlSize(.mini)
                        .accessibilityLabel("Working")
                        .accessibilityIdentifier("work-progress-\(item.artifact.id)")
                }
            }.frame(width: 16, height: 16)
        }
    }
}

private struct WorkTextLines: ViewModifier {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    @ViewBuilder
    func body(content: Content) -> some View {
        if dynamicTypeSize.isAccessibilitySize {
            content.lineLimit(nil)
        } else {
            content.lineLimit(2, reservesSpace: true)
        }
    }
}
