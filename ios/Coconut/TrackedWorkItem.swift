import SwiftUI

struct TrackedWorkItem: Identifiable {
    let artifact: Artifact
    let pullRequest: TrackedPullRequest?
    var id: String { pullRequest?.id ?? "work:" + artifact.id }
    var isFinished: Bool { pullRequest?.isFinished ?? ["completed", "cancelled"].contains(artifact.status) }

    static func tracked(in artifacts: [Artifact], visibility: [String: Bool] = [:]) -> [Self] {
        let requests = TrackedPullRequest.tracked(in: artifacts, visibility: visibility)
            .map { Self(artifact: $0.artifact, pullRequest: $0) }
        let tasks = artifacts.filter { $0.kind == "work" && !(visibility["work:" + $0.id] ?? ($0.workDismissalId != nil)) }
            .map { Self(artifact: $0, pullRequest: nil) }
        return (requests + tasks).sorted {
            if $0.isFinished != $1.isFinished { return !$0.isFinished }
            return ($0.artifact.createdAt, $0.id) > ($1.artifact.createdAt, $1.id)
        }
    }

    func activity(at date: Date) -> String {
        if artifact.status == "completed" { return "Completed" }
        if artifact.status == "cancelled" { return "Stopped" }
        guard let observed = artifact.activityObservedAt?.apiDate, date.timeIntervalSince(observed) < 60 else {
            return "Status unavailable"
        }
        return artifact.activity ?? "Status unavailable"
    }

    func isWorking(at date: Date) -> Bool {
        guard !isFinished, artifact.activityState == "working",
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
