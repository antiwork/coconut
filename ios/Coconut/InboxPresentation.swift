import Foundation

struct PendingDecisionThread: Identifiable {
    let id: String
    let decision: Decision
    let decisions: [Decision]
}

enum InboxPresentation {
    static func threads(decisions: [Decision], tasks: [AgentTask]) -> [PendingDecisionThread] {
        let tasksByID = Dictionary(tasks.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let grouped = Dictionary(grouping: decisions.filter(\.isPending)) { decision in
            tasksByID[decision.taskId]?.threadId ?? decision.taskId
        }
        return grouped.compactMap { threadID, pending in
            let ordered = pending.sorted(by: precedes)
            guard let first = ordered.first else { return nil }
            return PendingDecisionThread(id: threadID, decision: first, decisions: ordered)
        }.sorted { precedes($0.decision, $1.decision) }
    }

    private static func precedes(_ first: Decision, _ second: Decision) -> Bool {
        let urgency = ["critical": 3, "high": 2, "normal": 1, "low": 0]
        let firstRank = urgency[first.urgency] ?? 1
        let secondRank = urgency[second.urgency] ?? 1
        if firstRank != secondRank { return firstRank > secondRank }
        let firstDate = first.createdAt.apiDate ?? .distantPast
        let secondDate = second.createdAt.apiDate ?? .distantPast
        if firstDate != secondDate { return firstDate < secondDate }
        return first.id < second.id
    }
}
