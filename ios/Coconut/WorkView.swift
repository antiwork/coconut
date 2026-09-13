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
    let openConversation: (String) -> Void
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
                    .listRowInsets(EdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24))
                    .listRowBackground(BoardStyle.paper)
                    .listRowSeparator(.hidden, edges: .top)
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

    @ViewBuilder
    private func workRow(_ item: TrackedWorkItem) -> some View {
        if let pr = item.pullRequest {
            row(pr)
        } else {
            Button { openConversation(item.artifact.botId) } label: {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(item.artifact.title).font(.body.weight(.medium))
                            .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                            .foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                        TimelineView(.periodic(from: .now, by: 15)) { context in
                            Text(item.activity(at: context.date)).font(.caption)
                                .foregroundStyle(item.color(at: context.date, scheme: colorScheme))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    if !item.isFinished, let bot = store.bots.first(where: { $0.id == item.artifact.botId }) {
                        BotAvatar(bot: bot, size: 28, animated: false).frame(width: 44, height: 44)
                            .accessibilityHidden(true)
                    }
                }.padding(.vertical, 14).contentShape(Rectangle())
            }.buttonStyle(HomePressStyle())
                .accessibilityHint("Opens the bot conversation")
                .accessibilityIdentifier("work-item-\(item.artifact.id)")
        }
    }

    private func row(_ pr: TrackedPullRequest) -> some View {
        HStack(spacing: 8) {
            Link(destination: pr.url) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(pr.artifact.title).font(.body.weight(.medium))
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                        .foregroundStyle(.primary).fixedSize(horizontal: false, vertical: true)
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 6) {
                            reference(pr)
                            Text("·").foregroundStyle(.tertiary).accessibilityHidden(true)
                            checks(pr)
                        }.fixedSize(horizontal: true, vertical: false)
                        VStack(alignment: .leading, spacing: 3) { reference(pr); checks(pr) }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    .padding(.vertical, 14)
            }
            .buttonStyle(HomePressStyle())
            .accessibilityIdentifier("work-pr-\(pr.number)")
            if !pr.isFinished, let bot = store.bots.first(where: { $0.id == (pr.artifact.activityBotId ?? pr.artifact.botId) }) {
                Button { openConversation(bot.id) } label: {
                    BotAvatar(bot: bot, size: 28, animated: false)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(HomePressStyle())
                .frame(width: 44)
                .accessibilityLabel("Open \(bot.name) conversation")
                .accessibilityIdentifier("work-bot-\(bot.id)")
            }
        }
    }

    private func reference(_ pr: TrackedPullRequest) -> some View {
        Text("\(pr.repository) #\(pr.number)" + (!pr.isFinished && pr.artifact.draft == true ? " · Draft" : ""))
            .font(.caption).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func checks(_ pr: TrackedPullRequest) -> some View {
        TimelineView(.periodic(from: .now, by: 15)) { context in
            HStack(spacing: 6) {
                if let activity = pr.activityLabel(at: context.date) {
                    Text(activity).font(.caption).foregroundStyle(.secondary)
                    Circle().fill(pr.checksColor(at: context.date, scheme: colorScheme))
                        .frame(width: 6, height: 6)
                        .accessibilityLabel(pr.checksLabel(at: context.date))
                } else {
                    Text(pr.checksLabel(at: context.date)).font(.caption)
                        .foregroundStyle(pr.checksColor(at: context.date, scheme: colorScheme))
                }
            }.fixedSize(horizontal: false, vertical: true)
        }
    }
}
