import SwiftUI

struct TrackedWorkItem: Identifiable {
    let artifact: Artifact
    let pullRequest: TrackedPullRequest?
    var id: String { pullRequest?.id ?? "work:" + artifact.id }
    var needsDecision: Bool { artifact.workState == "needs_decision" && artifact.workDecisionId != nil }
    var groupOrder: Int { isFinished ? 2 : needsDecision ? 0 : 1 }
    var isFinished: Bool {
        if let state = artifact.workState { return ["completed", "cancelled"].contains(state) }
        return pullRequest?.isFinished ?? ["completed", "cancelled"].contains(artifact.status)
    }

    static func tracked(in artifacts: [Artifact], visibility: [String: Bool] = [:]) -> [Self] {
        let requests = TrackedPullRequest.tracked(in: artifacts, visibility: visibility)
            .map { Self(artifact: $0.artifact, pullRequest: $0) }
        let tasks = artifacts.filter { $0.kind == "work" && !(visibility["work:" + $0.id] ?? ($0.workDismissalId != nil)) }
            .map { Self(artifact: $0, pullRequest: nil) }
        return (requests + tasks).sorted {
            if $0.groupOrder != $1.groupOrder { return $0.groupOrder < $1.groupOrder }
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
        if artifact.status == "completed" { return "Completed" }
        if artifact.status == "cancelled" { return "Stopped" }
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
