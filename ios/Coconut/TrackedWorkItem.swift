import SwiftUI

struct TrackedWorkItem: Identifiable {
    let artifact: Artifact
    let pullRequests: [TrackedPullRequest]
    let recoveryWork: Artifact?
    init(artifact: Artifact, pullRequests: [TrackedPullRequest], recoveryWork: Artifact? = nil) {
        self.artifact = artifact
        self.pullRequests = pullRequests
        self.recoveryWork = recoveryWork ?? (artifact.kind == "work" ? artifact : nil)
    }
    var pullRequest: TrackedPullRequest? { pullRequests.count == 1 ? pullRequests.first : nil }
    var id: String { artifact.workIdentity }
    var needsDecision: Bool { artifact.workState == "needs_decision" && artifact.workDecisionId != nil }
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
        guard let recoveryWork, recoveryWork.kind == "work", recoveryWork.nativeTaskId != nil,
              recoveryWork.taskId != nil, recoveryWork.workState == "cancelled" else { return nil }
        return recoveryWork
    }
    var canOfferCheckoutRecovery: Bool { checkoutRecoveryWork != nil }

    static func nativeTasks(in artifacts: [Artifact], botId: String, tasks: [AgentTask] = []) -> [Self] {
        let conversationTasks = Set(tasks.filter { $0.conversationBotId == botId }.map(\.id))
        let identities = Set(artifacts.filter {
            $0.nativeTaskId != nil && ($0.botId == botId || $0.taskId.map(conversationTasks.contains) == true)
        }.map(\.workIdentity))
        return tracked(in: artifacts.filter { identities.contains($0.workIdentity) })
    }

    static func tracked(in artifacts: [Artifact], visibility: [String: Bool] = [:], at date: Date = Date()) -> [Self] {
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
                  !(visibility[identity] ?? copies.contains { $0.workDismissalId != nil }) else { return nil }
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

    func activity(at date: Date) -> String {
        if isFinished { return (artifact.workState ?? artifact.status) == "cancelled" ? "Stopped" : "Completed" }
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

    func color(at date: Date, scheme: ColorScheme) -> Color {
        let green = scheme == .dark ? Color.green : Color(red: 0.12, green: 0.44, blue: 0.22)
        if artifact.status == "completed" { return green }
        guard !isFinished, let observed = artifact.activityObservedAt?.apiDate,
              date.timeIntervalSince(observed) < 60 else { return .secondary }
        switch artifact.activityState {
        case "ready": return green
        case "blocked": return scheme == .dark ? .orange : Color(red: 0.60, green: 0.27, blue: 0)
        default: return .secondary
        }
    }
}
