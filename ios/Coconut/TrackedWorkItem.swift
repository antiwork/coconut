import SwiftUI

struct TrackedWorkItem: Identifiable {
    let artifact: Artifact
    let pullRequests: [TrackedPullRequest]
    let recoveryWork: Artifact?
    let originalTaskStatus: String?
    init(artifact: Artifact, pullRequests: [TrackedPullRequest], recoveryWork: Artifact? = nil,
         originalTaskStatus: String? = nil) {
        self.artifact = artifact
        self.pullRequests = pullRequests
        self.recoveryWork = recoveryWork ?? (artifact.kind == "work" ? artifact : nil)
        self.originalTaskStatus = originalTaskStatus
    }
    var pullRequest: TrackedPullRequest? { pullRequests.count == 1 ? pullRequests.first : nil }
    var id: String { artifact.workIdentity }
    func groupOrder(at date: Date) -> Int {
        if isFinished { return 5 }
        switch artifact.workState ?? artifact.activityState ?? artifact.status {
        case "working": return isWorking(at: date) ? 0 : 4
        case "queued": return 1
        case "waiting": return 2
        case "needs_decision": return 3
        default: return 4
        }
    }
    var isFinished: Bool {
        if let state = artifact.workState { return ["completed", "cancelled"].contains(state) }
        return pullRequests.isEmpty ? ["completed", "cancelled"].contains(artifact.status) : pullRequests.allSatisfy(\.isFinished)
    }
    var checkoutRecoveryWork: Artifact? {
        guard originalTaskStatus == "cancelled", let recoveryWork, recoveryWork.kind == "work",
              recoveryWork.workOutcomeId == nil || recoveryWork.workOutcomeId == recoveryWork.id,
              recoveryWork.nativeTaskId != nil, recoveryWork.taskId != nil,
              recoveryWork.workState == "cancelled", recoveryWork.checkoutRecoveryStopped == true else { return nil }
        return recoveryWork
    }
    var canOfferCheckoutRecovery: Bool { checkoutRecoveryWork != nil }

    /// Hermes states that wait for someone to act. A task there without activity for a week counts as idle.
    static let idleStatuses: Set<String> = ["triage", "todo", "blocked", "review", "scheduled"]
    static let idleAge: TimeInterval = 7 * 86_400
    /// Finished work stays on the task list for a week. Older finished work stays behind Show older.
    static let finishedWindow: TimeInterval = 7 * 86_400

    /// A pull request row shows the newest copy of the PR, which can belong to another bot. Its Work item owns the Hermes task.
    private var ownerWork: Artifact { recoveryWork ?? artifact }
    /// Hermes keeps each task under the bot of its Work item. Without that Work item, the owner is unknown.
    var nativeTaskId: String? { recoveryWork?.nativeTaskId }
    var nativeOwnerBotId: String? { recoveryWork?.botId }

    var lastActivity: Date? {
        (ownerWork.nativeLastActivityAt ?? ownerWork.observedAt ?? ownerWork.checkedAt ?? ownerWork.createdAt).apiDate
    }

    func isIdle(at date: Date) -> Bool {
        guard !isFinished, let work = recoveryWork, work.nativeTaskId != nil, let status = work.nativeStatus,
              Self.idleStatuses.contains(status), let last = work.nativeLastActivityAt?.apiDate else { return false }
        return date.timeIntervalSince(last) >= Self.idleAge
    }

    func isRecent(at date: Date) -> Bool {
        guard let last = lastActivity else { return true }
        return date.timeIntervalSince(last) < Self.finishedWindow
    }

    func idleLabel(at date: Date) -> String {
        let days = max(1, Int(date.timeIntervalSince(lastActivity ?? date) / 86_400))
        let idle = days == 1 ? "Idle 1 day" : "Idle \(days) days"
        guard let diagnostic = ownerWork.nativeDiagnostics?.first else { return idle }
        return idle + " · " + diagnostic.title
    }

    /// Task details shows active Work first, then Work idle for a week, then finished Work.
    struct Groups {
        var active: [TrackedWorkItem] = [], idle: [TrackedWorkItem] = [], recent: [TrackedWorkItem] = [], older: [TrackedWorkItem] = []

        /// Work that the bot delegated counts as idle too. Each task's own bot archives it.
        init(_ items: [TrackedWorkItem], at date: Date) {
            for item in items {
                // A stopped checkout stays on top for a week. After that its recovery stays with finished Work.
                let recoverable = item.canOfferCheckoutRecovery && item.isRecent(at: date)
                if recoverable || (!item.isFinished && !item.isIdle(at: date)) { active.append(item) }
                else if !item.isFinished { idle.append(item) }
                else if item.isRecent(at: date) { recent.append(item) }
                else { older.append(item) }
            }
            recent.sort { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }
            older.sort { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }
        }
    }

    static func nativeTasks(in artifacts: [Artifact], botId: String, tasks: [AgentTask] = [],
                            includeDismissed: Bool = false) -> [Self] {
        let conversationTasks = Set(tasks.filter { $0.conversationBotId == botId }.map(\.id))
        let identities = Set(artifacts.filter {
            $0.nativeTaskId != nil && ($0.botId == botId || $0.taskId.map(conversationTasks.contains) == true)
        }.map(\.workIdentity))
        let taskStatuses = Dictionary(tasks.map { ($0.id, $0.status) }, uniquingKeysWith: { first, _ in first })
        let visibleArtifacts = artifacts.filter { identities.contains($0.workIdentity) }
        let dismissedIdentities = includeDismissed ? Set(visibleArtifacts.filter {
            Self(artifact: $0, pullRequests: [],
                 originalTaskStatus: $0.taskId.flatMap { taskStatuses[$0] }).canOfferCheckoutRecovery
        }.map(\.workIdentity)) : []
        return tracked(in: visibleArtifacts, includeDismissedWorkIdentities: dismissedIdentities).map { item in
            let taskStatus = item.recoveryWork?.taskId.flatMap { taskStatuses[$0] }
            return Self(artifact: item.artifact, pullRequests: item.pullRequests,
                        recoveryWork: item.recoveryWork, originalTaskStatus: taskStatus)
        }
    }

    static func tracked(in artifacts: [Artifact], visibility: [String: Bool] = [:], at date: Date = Date(),
                        includeDismissedWorkIdentities: Set<String> = []) -> [Self] {
        let allRequests = TrackedPullRequest.tracked(in: artifacts, visibility: visibility)
        let represented = Set(allRequests.map { $0.artifact.workIdentity })
        let requests = Dictionary(grouping: allRequests, by: { $0.artifact.workIdentity }).values.map { copies in
            let copies = copies.sorted { $0.id < $1.id }
            let outcome = artifacts.first(where: {
                $0.kind == "work" && $0.workIdentity == copies[0].artifact.workIdentity
                    && ($0.workOutcomeId == nil || $0.workOutcomeId == $0.id)
            })
            return Self(artifact: copies.count > 1 ? outcome ?? copies[0].artifact : copies[0].artifact,
                        pullRequests: copies, recoveryWork: outcome)
        }
        let groupedTasks = Dictionary(grouping: artifacts.filter { $0.kind == "work" }, by: \.workIdentity)
        let tasks = groupedTasks.compactMap { identity, copies -> Self? in
            guard !represented.contains(identity),
                  !(visibility[identity] ?? copies.contains { $0.workDismissalId != nil })
                    || includeDismissedWorkIdentities.contains(identity) else { return nil }
            let ordered = copies.sorted { $0.id < $1.id }
            guard let item = ordered.first(where: { $0.workOutcomeId == nil || $0.workOutcomeId == $0.id })
                    ?? ordered.first else { return nil }
            return Self(artifact: item, pullRequests: [])
        }
        return (requests + tasks).sorted {
            let firstGroup = $0.groupOrder(at: date)
            let secondGroup = $1.groupOrder(at: date)
            if firstGroup != secondGroup { return firstGroup < secondGroup }
            if firstGroup == 1 {
                let first = $0.artifact.workQueuePosition ?? Int.max
                let second = $1.artifact.workQueuePosition ?? Int.max
                if first != second { return first < second }
            }
            return ($0.artifact.createdAt, $0.id) < ($1.artifact.createdAt, $1.id)
        }
    }

    func queueLabel(botName: String) -> String? {
        guard artifact.workState == "queued", let position = artifact.workQueuePosition, position > 0 else { return nil }
        if position == 1 { return "Next for \(botName)" }
        let formatter = NumberFormatter()
        formatter.numberStyle = .ordinal
        formatter.locale = Locale(identifier: "en_US")
        let ordinal = formatter.string(from: NSNumber(value: position)) ?? String(position)
        return "\(ordinal) in \(botName)’s queue"
    }

    private var wasStopped: Bool { (artifact.workState ?? artifact.status) == "cancelled" }

    func activity(at date: Date) -> String {
        if isFinished { return wasStopped ? "Stopped" : "Completed" }
        guard let observed = artifact.activityObservedAt?.apiDate, date.timeIntervalSince(observed) < 60 else {
            return "Status unavailable"
        }
        return artifact.activity ?? "Status unavailable"
    }

    func isWorking(at date: Date) -> Bool {
        guard !isFinished, (artifact.workState ?? artifact.activityState) == "working",
              let observed = artifact.activityObservedAt?.apiDate else { return false }
        return date.timeIntervalSince(observed) < 60
    }

    /// The StatusLabel key for a task row. Waiting work pauses on its own, so it does not claim that it needs you.
    func status(at date: Date) -> String {
        if isFinished {
            if wasStopped { return "cancelled" }
            // Without a Work state, pull requests end the task. A closed pull request did not complete it.
            if artifact.workState == nil, !pullRequests.isEmpty, !pullRequests.contains(where: { $0.artifact.status == "merged" || $0.artifact.prState == "merged" }) {
                return "closed"
            }
            return "completed"
        }
        switch artifact.workState ?? artifact.activityState ?? artifact.status {
        case "working": return isWorking(at: date) ? "working" : "status_out_of_date"
        case "queued": return "queued"
        case "needs_decision": return "needs_you"
        case "waiting": return "paused"
        case let state: return state
        }
    }
}
