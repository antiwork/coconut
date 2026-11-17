import Foundation

struct StateUpdates: Decodable, Sendable {
    let after: Int
    let cursor: Int
    let serverTime: String
    var reset: StateSnapshot?
    var bots: [Bot]?
    var tasks: [AgentTask]?
    var messages: [Message]?
    var removedMessages: [String]?
    var decisions: [Decision]?
    var artifacts: [Artifact]?
    var collectionDeltas: Bool?
    var removedDecisions: [String]?
    var removedArtifacts: [String]?
    var workCapacity: ParallelWorkSettings?
    var settingsChanged: Bool?
    /// A document or a team page changed. The list of the Library is not part of the state.
    var documentsChanged: Bool?
    /// A team, a member or a bot of a team changed. The list of the Team screen is not part of the state.
    var teamChanged: Bool?
    /// The teams whose chat changed: a message, an edit, a removal, or the read position.
    var teamMessagesChanged: [String]?
    /// The direct chats that changed: a message, an edit, a removal, a name, or the read position.
    var teamDirectsChanged: [TeamDirectChange]?
    /// The direct chats with a bot that changed: a message, an edit, a removal, a name, or the read position.
    var teamBotDirectsChanged: [TeamBotDirectChange]?
    /// The teams where who types changed. The list of the teams says who types now.
    var teamTypingChanged: [String]?
    /// The tasks whose live activity changed. The feed is not part of the state: an open live panel reads it again.
    var activityChanged: [String]?
    var confirmedRequestIds: [String]?

    /// The chats that changed: each Team chat, each direct chat and each direct chat with a bot of this page.
    var changedTeamChats: [TeamChatKey] {
        (teamMessagesChanged ?? []).map { TeamChatKey(room: $0) }
            + (teamDirectsChanged ?? []).map { TeamChatKey(room: $0.team, member: $0.member) }
            + (teamBotDirectsChanged ?? []).map { TeamChatKey(room: $0.team, bot: $0.bot) }
    }

    func applying(to snapshot: StateSnapshot) -> StateSnapshot? {
        guard after == snapshot.cursor else { return nil }
        if let reset { return reset.cursor == cursor ? reset : nil }
        guard cursor >= after else { return nil }
        var result = snapshot
        if let bots { result.bots = bots }
        if let tasks { result.tasks = Self.merge(tasks, into: result.tasks) }
        if let messages { result.messages = Self.merge(messages, into: result.messages) }
        if let removedMessages, !removedMessages.isEmpty {
            let removed = Set(removedMessages)
            result.messages.removeAll { removed.contains($0.id) }
        }
        result.messages.sort { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
        if collectionDeltas == true {
            if let decisions { result.decisions = Self.merge(decisions, into: result.decisions) }
            if let artifacts { result.artifacts = Self.merge(artifacts, into: result.artifacts) }
            let removedCards = Set(removedDecisions ?? [])
            let removedFiles = Set(removedArtifacts ?? [])
            result.decisions.removeAll { removedCards.contains($0.id) }
            result.artifacts.removeAll { removedFiles.contains($0.id) }
        } else {
            if let decisions { result.decisions = decisions }
            if let artifacts { result.artifacts = artifacts }
        }
        if let workCapacity { result.workCapacity = workCapacity }
        if let confirmedRequestIds { result.confirmedRequestIds = confirmedRequestIds }
        result.cursor = cursor
        result.serverTime = serverTime
        return result
    }

    static func mergeBotReceipt(_ receipt: Bot, original: Bot, current: Bot) -> Bot {
        guard original.sessionId == current.sessionId else { return current }
        var result = current
        func update<Value: Equatable>(_ field: WritableKeyPath<Bot, Value>) {
            if current[keyPath: field] == original[keyPath: field] { result[keyPath: field] = receipt[keyPath: field] }
        }
        update(\.name)
        update(\.role)
        update(\.avatar)
        update(\.color)
        update(\.instructions)
        update(\.archived)
        update(\.memory)
        update(\.pinned)
        update(\.preferences)
        update(\.modelOverride)
        update(\.effectiveModel)
        update(\.notifications)
        update(\.approvalOverride)
        update(\.effectiveApproval)
        update(\.workerLimit)
        update(\.modelError)
        update(\.health)
        return result
    }

    static func mergeRoutineReceipt(_ receipt: Routine, original: Routine, current: Routine) -> Routine {
        var result = current
        func update<Value: Equatable>(_ field: WritableKeyPath<Routine, Value>) {
            if current[keyPath: field] == original[keyPath: field] { result[keyPath: field] = receipt[keyPath: field] }
        }
        update(\.name)
        update(\.prompt)
        update(\.cron)
        update(\.timezone)
        update(\.enabled)
        update(\.nextRunAt)
        update(\.lastRunAt)
        update(\.lastTaskId)
        update(\.updatedAt)
        update(\.error)
        update(\.oneTime)
        update(\.state)
        update(\.runAt)
        return result
    }

    static func sameRoutineSettings(_ left: Routine?, _ right: Routine?) -> Bool {
        guard let left, let right else { return left == nil && right == nil }
        return left.id == right.id && left.botId == right.botId
            && (left.name, left.prompt, left.cron, left.timezone, left.enabled) == (right.name, right.prompt, right.cron, right.timezone, right.enabled)
    }

    /// An update of a few values finds each place with a scan from the end, where the newest values are. A table of
    /// the places of all values costs more than the scans then.
    static let scannedUpdates = 8

    static func merge<T: Identifiable>(_ updates: [T], into values: [T]) -> [T] where T.ID: Hashable {
        var result = values
        if updates.count <= scannedUpdates {
            for value in updates {
                if let index = result.lastIndex(where: { $0.id == value.id }) { result[index] = value }
                else { result.append(value) }
            }
            return result
        }
        var positions = Dictionary(uniqueKeysWithValues: values.enumerated().map { ($0.element.id, $0.offset) })
        for value in updates {
            if let index = positions[value.id] { result[index] = value }
            else { positions[value.id] = result.count; result.append(value) }
        }
        return result
    }
}

/// One item of `team_directs_changed`: the room and the other member of a direct chat.
struct TeamDirectChange: Decodable, Equatable, Sendable {
    let team: String
    let member: String
}

/// One item of `team_bot_directs_changed`: the room and the team bot ID of a direct chat with a bot.
struct TeamBotDirectChange: Decodable, Equatable, Sendable {
    let team: String
    let bot: String
}

struct HistoryPage: Decodable, Sendable {
    let messages: [Message]
    let tasks: [AgentTask]?
    let decisions: [Decision]?
    /// The cards that older messages show. Recent content leaves out old finished Work.
    let artifacts: [Artifact]?
    let before: Int?
}

struct CardSnapshot: Decodable, Sendable {
    let decisions: [Decision]?
    let tasks: [AgentTask]?
    let decision: Decision
    let task: AgentTask
    let bot: Bot
    let owner: Bot
    let messages: [Message]?
    let cursor: Int
}

struct NativeSnapshot: Decodable, Sendable {
    let cursor: Int
    let bots: [Bot]
    let modelCatalog: ModelCatalog?
    let modelConfigured: Bool
    let approvals: ApprovalSettings?
    let parallelWork: ParallelWorkSettings?
    let fallback: FallbackSettings?
    let mcpServers: [MCPServer]?
    let routines: [Routine]?
    let pushConfigured: Bool?
}
