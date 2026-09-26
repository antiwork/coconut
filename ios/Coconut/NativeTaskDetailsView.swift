import SwiftUI

struct CheckoutRecoveryRequest: Codable, Equatable, Sendable {
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

struct CheckoutRecoveryPairing: Codable, Equatable, Sendable {
    let deviceId: String
    let serverURL: String
    let fingerprint: String
}

struct CheckoutRecoveryCheck: Sendable {
    let preview: CheckoutRecoveryPreview
    let pairing: CheckoutRecoveryPairing
    let workItemId: String
}

struct CheckoutRecoveryConfirmation: Codable, Sendable {
    let key: String
    let request: CheckoutRecoveryRequest
    let pairing: CheckoutRecoveryPairing
    let workItemId: String?
    private(set) var isUncertain = false

    init(preview: CheckoutRecoveryPreview, pairing: CheckoutRecoveryPairing, workItemId: String? = nil) {
        key = preview.key
        self.pairing = pairing
        self.workItemId = workItemId
        var request = preview.request
        request.resumeConsent = preview.resumeConsent
        request.resumeConfirmed = true
        self.request = request
    }

    private enum CodingKeys: String, CodingKey { case key, request, pairing, workItemId, isUncertain }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        key = try container.decode(String.self, forKey: .key)
        request = try container.decode(CheckoutRecoveryRequest.self, forKey: .request)
        pairing = try container.decode(CheckoutRecoveryPairing.self, forKey: .pairing)
        workItemId = try container.decodeIfPresent(String.self, forKey: .workItemId)
        isUncertain = try container.decodeIfPresent(Bool.self, forKey: .isUncertain) ?? false
    }

    mutating func markUncertain() { isUncertain = true }

    func matches(pairing: CheckoutRecoveryPairing) -> Bool { self.pairing == pairing }

    func matches(_ other: Self) -> Bool {
        key == other.key && request == other.request && pairing == other.pairing && workItemId == other.workItemId
    }
}

struct CheckoutRecoveryStop {
    let workItemId: String
    let originalTaskId: String
    let path: String
    let key: String

    init?(confirmation: CheckoutRecoveryConfirmation) {
        guard let workItemId = confirmation.workItemId, !workItemId.isEmpty else { return nil }
        self.workItemId = workItemId
        originalTaskId = confirmation.request.taskId
        path = "work/items/\(workItemId)/cancel"
        key = UUID().uuidString
    }

    func admittedSuccessor(in artifacts: [Artifact], tasks: [AgentTask]) -> AgentTask? {
        guard let work = artifacts.first(where: { $0.id == workItemId && $0.kind == "work" &&
            ($0.workOutcomeId == nil || $0.workOutcomeId == $0.id) }),
              let successorId = work.taskId, successorId != originalTaskId,
              tasks.contains(where: { $0.id == originalTaskId && $0.status == "cancelled" }) else { return nil }
        return tasks.first(where: { $0.id == successorId && $0.botId == work.botId && $0.isActive })
    }
}

struct CheckoutRecoveryReceipt: Decodable, Sendable {
    let state: String
    let error: String?
}

enum CheckoutRecoveryAcceptedState: String, Equatable, Sendable {
    case accepted, draining, applied
}

enum CheckoutRecoveryConfirmationResult: Equatable, Sendable {
    case accepted(CheckoutRecoveryAcceptedState)
    case rejected(String)
    case uncertain(String)

    var failureReason: String? {
        switch self {
        case .accepted: return nil
        case .rejected(let reason), .uncertain(let reason): return reason
        }
    }

    static func classify(receipt: CheckoutRecoveryReceipt) -> Self {
        if let state = CheckoutRecoveryAcceptedState(rawValue: receipt.state) { return .accepted(state) }
        switch receipt.state {
        case "held": return .rejected(receipt.error.flatMap { $0.isEmpty ? nil : $0 }
                                      ?? "The Mac held this recovery request. Check the task again.")
        case "superseded": return .rejected(receipt.error ?? "This recovery request was superseded. Check the task again.")
        default: return .uncertain("The Mac returned an unrecognized recovery state. Retry the same confirmation to check it.")
        }
    }

    static func classify(error: Error) -> Self {
        if let serviceError = error as? ServiceError {
            switch serviceError {
            case .response(let status, let message) where (400..<500).contains(status) && status != 408:
                return .rejected(message)
            case .configuration(let message): return .rejected(message)
            case .keychain: return .rejected(error.localizedDescription)
            default: break
            }
        }
        return .uncertain(error.localizedDescription)
    }
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
    @State private var recovery: CheckoutRecoveryCheck?
    @State private var confirming = false
    @State private var recovering: Set<String> = []
    @State private var stoppingRecovery = false
    @State private var stopPendingConfirmation: CheckoutRecoveryConfirmation?
    @State private var stopConfirmationMessage: String?

    private var items: [TrackedWorkItem] {
        TrackedWorkItem.nativeTasks(in: store.artifacts, botId: botId, tasks: store.tasks, includeDismissed: true)
    }

    private var confirmation: CheckoutRecoveryConfirmation? { store.checkoutConfirmation }

    private func retryConfirmation() {
        guard let confirmation, !confirming else { return }
        confirming = true
        Task {
            let result = await store.confirmCheckoutRecovery(confirmation)
            store.recordCheckoutRecoveryResult(result, for: confirmation)
            error = result.failureReason
            confirming = false
        }
    }

    var body: some View {
        List {
            if confirmation != nil {
                Button("Check recovery status") { retryConfirmation() }
                    .disabled(confirming)
                    .accessibilityIdentifier("retry-checkout-recovery")
                if let confirmation, confirmation.workItemId != nil {
                    Button(stoppingRecovery ? "Stopping…" : "Stop recovery", role: .destructive) {
                        stopPendingConfirmation = confirmation
                    }
                    .disabled(stoppingRecovery)
                    .accessibilityIdentifier("stop-pending-checkout-recovery")
                }
                if let stopConfirmationMessage {
                    Text(stopConfirmationMessage).font(.footnote).foregroundStyle(.secondary)
                }
            }
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
                        Button(recovering.contains(item.id) ? "Checking…" : "Recover checkout") {
                            recovering.insert(item.id)
                            Task {
                                do { recovery = try await store.checkoutRecoveryPreview(item) }
                                catch { error = error.localizedDescription }
                                recovering.remove(item.id)
                            }
                        }
                        .disabled(recovering.contains(item.id) || confirming || confirmation != nil)
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
        .alert("Stop this recovery?", isPresented: Binding(
            get: { stopPendingConfirmation != nil }, set: { if !$0 { stopPendingConfirmation = nil } }),
            presenting: stopPendingConfirmation) { pending in
            Button("Not now", role: .cancel) { stopPendingConfirmation = nil }
            Button("Stop recovery", role: .destructive) {
                stopPendingConfirmation = nil
                stoppingRecovery = true
                Task {
                    let failure = await store.stopPendingCheckoutRecovery(pending)
                    error = failure
                    stopConfirmationMessage = failure == nil ? "Stop sent. Check recovery status to reconcile the original request." : nil
                    stoppingRecovery = false
                }
            }.accessibilityIdentifier("confirm-stop-pending-checkout-recovery")
        } message: { _ in
            Text("This sends a fresh Stop for the original Work. If recovery already resumed, it stops that Work. Check recovery status afterward; Stop never confirms a new recovery.")
        }
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
        .alert(confirmation?.isUncertain == true ? "Recovery confirmation uncertain" : "Request not confirmed",
               isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
            if confirmation?.isUncertain == true {
                Button("Retry same confirmation") { error = nil; retryConfirmation() }
                Button("Later", role: .cancel) { error = nil }
            } else {
                Button("OK") { error = nil }
            }
        } message: { Text(confirmation?.isUncertain != true ? (error ?? "") :
            "The Mac may have accepted this request. Retry uses the same confirmation and key. \(error ?? "")") }
        .alert("Recover this checkout?", isPresented: Binding(
            get: { recovery != nil }, set: { if !$0 { recovery = nil } })) {
            Button("Not now", role: .cancel) { recovery = nil }
            Button("Recover checkout") {
                guard let check = recovery, confirmation == nil else { return }
                recovery = nil
                do {
                    try store.stageCheckoutRecovery(CheckoutRecoveryConfirmation(preview: check.preview,
                        pairing: check.pairing, workItemId: check.workItemId))
                    retryConfirmation()
                } catch { self.error = error.localizedDescription }
            }.disabled(confirmation != nil).accessibilityIdentifier("confirm-checkout-recovery")
        } message: {
            Text("This resumes the stopped Work in its original workspace. The Mac will check the saved branches and local edits before continuing. Nothing changes until you confirm.")
        }
    }
}
