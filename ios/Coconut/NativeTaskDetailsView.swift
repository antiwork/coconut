import SwiftUI

struct CheckoutRecoveryRequest: Codable, Sendable {
    let taskId: String
    let nativeTaskId: String
    let expectedOwner: String
    let repository: String
    let path: String
    let savedBranch: String
    let attachedBranch: String
    let savedHead: String
    let attachedHead: String
    let recoveryBranch: String
    let message: String
    var resumeConsent: String?
    var resumeConfirmed: Bool?
}

struct CheckoutRecoveryPreview: Decodable, Sendable {
    let key: String
    let request: CheckoutRecoveryRequest
    let resumeConsent: String
    let dirtyCount: Int
}

struct CheckoutRecoveryReceipt: Decodable, Sendable {
    let state: String
}

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
            let identity = copies.first(where: { $0.workOutcomeId != nil })?.workIdentity ?? url
            guard !(visibility[identity] ?? copies.contains { $0.workDismissalId != nil }) else { return nil }
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
        artifact.isPRStatusStale(at: date)
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

struct NativeTaskDetailsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let botId: String
    @State private var cancelling: Set<String> = []
    @State private var error: String?
    @State private var taskToStop: TrackedWorkItem?
    @State private var recovery: CheckoutRecoveryPreview?
    @State private var recovering: Set<String> = []
    @State private var recoveryRequested: Set<String> = []

    private var items: [TrackedWorkItem] {
        TrackedWorkItem.nativeTasks(in: store.artifacts, botId: botId, tasks: store.tasks)
    }

    var body: some View {
        List {
            if items.isEmpty {
                Text("No tasks").foregroundStyle(.secondary)
            }
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: 8) {
                    Text(item.artifact.title).font(.headline)
                    TimelineView(.periodic(from: .now, by: 10)) { context in
                        Text(item.activity(at: context.date)).foregroundStyle(.secondary)
                    }
                    ForEach(item.pullRequests) { request in
                        Link("\(request.repository) #\(request.number)", destination: request.url)
                    }
                    if let evidence = item.artifact.completionEvidence, item.isFinished {
                        Text(evidence).font(.footnote).foregroundStyle(.secondary)
                    }
                    if !item.isFinished {
                        Button(cancelling.contains(item.id) ? "Stopping…" : "Stop task", role: .destructive) {
                            taskToStop = item
                        }
                        .disabled(cancelling.contains(item.id))
                        .accessibilityIdentifier("task-stop-" + item.artifact.id)
                    }
                    if item.canOfferCheckoutRecovery {
                        Button(recoveryRequested.contains(item.artifact.taskId ?? "") ? "Recovery requested" :
                               recovering.contains(item.id) ? "Checking…" : "Recover checkout") {
                            recovering.insert(item.id)
                            Task {
                                do { recovery = try await store.checkoutRecoveryPreview(item) }
                                catch { error = error.localizedDescription }
                                recovering.remove(item.id)
                            }
                        }
                        .disabled(recovering.contains(item.id) || recoveryRequested.contains(item.artifact.taskId ?? ""))
                        .accessibilityIdentifier("task-recover-" + item.artifact.id)
                    }
                }
                .padding(.vertical, 4)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("task-detail-" + item.artifact.id)
            }
        }
        .navigationTitle("Task details")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        .alert("Stop this task?", isPresented: Binding(
            get: { taskToStop != nil }, set: { if !$0 { taskToStop = nil } }),
            presenting: taskToStop) { item in
            Button("Cancel", role: .cancel) { taskToStop = nil }
            Button("Stop task", role: .destructive) {
                cancelling.insert(item.id)
                Task {
                    error = await store.cancelWork(item)
                    cancelling.remove(item.id)
                }
            }.accessibilityIdentifier("confirm-stop-task")
        } message: { _ in
            Text("The Mac records the stop request. An action already executing can finish before it stops.")
        }
        .alert("Stop not confirmed", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
            Button("OK") { error = nil }
        } message: { Text(error ?? "") }
        .alert("Recover this checkout?", isPresented: Binding(
            get: { recovery != nil }, set: { if !$0 { recovery = nil } })) {
            Button("Not now", role: .cancel) { recovery = nil }
            Button("Recover checkout") {
                guard let preview = recovery else { return }
                recovery = nil
                Task {
                    error = await store.confirmCheckoutRecovery(preview)
                    if error == nil { recoveryRequested.insert(preview.request.taskId) }
                }
            }.accessibilityIdentifier("confirm-checkout-recovery")
        } message: {
            Text("This resumes the stopped Work in its original workspace. The Mac will check the saved branches and local edits before continuing. Nothing changes until you confirm.")
        }
    }
}
