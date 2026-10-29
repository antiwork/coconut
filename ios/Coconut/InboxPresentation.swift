import Foundation

struct PendingDecisionThread: Identifiable {
    let id: String
    let decision: Decision
    let decisions: [Decision]
}

enum InboxPresentation {
    static func entries(decisions: [Decision], sort: InboxSort = .recommended) -> [Decision] {
        decisions.filter(\.isPending).sorted { first, second in
            if sort == .recommended && first.isProposal != second.isProposal { return !first.isProposal }
            let firstDate = first.createdAt.apiDate ?? .distantPast
            let secondDate = second.createdAt.apiDate ?? .distantPast
            if firstDate != secondDate {
                return sort == .newest || (sort == .recommended && first.isProposal)
                    ? firstDate > secondDate : firstDate < secondDate
            }
            return first.id < second.id
        }
    }

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

/// Keep the current decision stable while new items arrive or another device answers.
struct DecisionQueue {
    private(set) var ids: [String] = []
    private var finished: Set<String> = []
    private var versions: [String: Int] = [:]
    private var deferred: Set<String> = []
    var remainingCount: Int { ids.count }

    mutating func reconcile(_ decisions: [Decision], sort: InboxSort, reset: Bool = false) {
        let entries = InboxPresentation.entries(decisions: decisions, sort: sort)
        let pending = Set(entries.map(\.id))
        for decision in entries {
            if let revision = versions[decision.id],
               revision != decision.revision || (deferred.contains(decision.id) && decision.inboxDeferredAt == nil) {
                finished.remove(decision.id)
            }
        }
        versions = Dictionary(entries.map { ($0.id, $0.revision) }, uniquingKeysWith: { _, latest in latest })
        deferred = Set(entries.filter { $0.inboxDeferredAt != nil }.map(\.id))
        finished.formIntersection(pending)
        let sorted = entries.filter { $0.inboxDeferredAt == nil } + entries.filter { $0.inboxDeferredAt != nil }
        if reset { ids = [] }
        ids = ids.filter { pending.contains($0) && !finished.contains($0) }
        ids += sorted.map(\.id).filter { !ids.contains($0) && !finished.contains($0) }
    }

    mutating func remove(_ id: String) {
        ids.removeAll { $0 == id }
        finished.insert(id)
    }

    mutating func restoreFirst(_ id: String) {
        finished.remove(id)
        ids.removeAll { $0 == id }
        ids.insert(id, at: 0)
    }

    mutating func reviewAgain(_ decisions: [Decision], sort: InboxSort) {
        finished = []
        reconcile(decisions, sort: sort, reset: true)
    }
}

/// Later preserves a decision. Dismiss removes it without an answer after the Undo window. It is not a rejection.
enum InboxSwipeAction: String {
    case later, dismiss

    static func resolve(translation: CGFloat, projected: CGFloat, width: CGFloat, isProposal: Bool) -> Self? {
        guard abs(translation) > 24 else { return nil }
        let destination = abs(projected) > abs(translation) && projected * translation > 0 ? projected : translation
        guard abs(destination) > width * 0.32 else { return nil }
        return destination > 0 ? .dismiss : .later
    }
}

struct InboxMutationResult: Sendable {
    let decision: Decision?
    let isUncertain: Bool
}
