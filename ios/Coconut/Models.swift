import Foundation

struct Bot: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var sessionId: String
    var name: String
    var role: String
    var avatar: String
    var color: String
    var instructions: String
    var status: String
    var lastMessage: String
    var unreadCount: Int
    var archived: Bool
    var createdAt: String
    var memory: String? = nil
    var pinned: Bool = false
    var preferences: [BotPreference] = []
    var modelOverride: ModelOverride? = nil
    var effectiveModel: ModelOverride? = nil
    var notifications: Bool = true
    var approvalOverride: String? = nil
    var effectiveApproval: String? = nil
    var workerLimit: Int? = nil
    var modelError: String? = nil
    var health: BotHealth? = nil
}

/// A bot that cannot run: its provider limit or error, and the source items that wait for it.
struct BotHealth: Codable, Equatable, Sendable {
    var state: String
    var errorCode: String?
    var until: String?
    var uncovered: Int

    var label: String {
        var text = "Provider error"
        if state == "rate_limited" {
            text = "Rate limited"
            if let date = until?.apiDate {
                let time = date.formatted(date: Calendar.current.isDateInToday(date) ? .omitted : .abbreviated, time: .shortened)
                text += " until \(time)"
            }
        }
        return uncovered > 0 ? "\(text) · \(uncovered) waiting" : text
    }
}

struct BotPreference: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var text: String
    var sourceMessageId: String
    var sourceText: String
    var createdAt: String
    var updatedAt: String
}

struct BotPatch: Encodable {
    var name: String?
    var role: String?
    var avatar: String?
    var color: String?
    var instructions: String?
    var notifications: Bool?

    init(updated: Bot, original: Bot) {
        name = updated.name != original.name ? updated.name : nil
        role = updated.role != original.role ? updated.role : nil
        avatar = updated.avatar != original.avatar ? updated.avatar : nil
        color = updated.color != original.color ? updated.color : nil
        instructions = updated.instructions != original.instructions ? updated.instructions : nil
        notifications = updated.notifications != original.notifications ? updated.notifications : nil
    }

    var isEmpty: Bool {
        name == nil && role == nil && avatar == nil && color == nil && instructions == nil && notifications == nil
    }

    func apply(to bot: inout Bot) {
        if let name { bot.name = name }
        if let role { bot.role = role }
        if let avatar { bot.avatar = avatar }
        if let color { bot.color = color }
        if let instructions { bot.instructions = instructions }
        if let notifications { bot.notifications = notifications }
    }
}

extension Bot {
    enum CodingKeys: String, CodingKey {
        case id, sessionId, name, role, avatar, color, instructions, status
        case lastMessage, unreadCount, archived, createdAt, memory, pinned, preferences, modelOverride, effectiveModel, modelError, notifications, approvalOverride, effectiveApproval, workerLimit, health
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        name = try values.decode(String.self, forKey: .name)
        role = try values.decode(String.self, forKey: .role)
        avatar = try values.decode(String.self, forKey: .avatar)
        color = try values.decode(String.self, forKey: .color)
        instructions = try values.decode(String.self, forKey: .instructions)
        status = try values.decode(String.self, forKey: .status)
        lastMessage = try values.decode(String.self, forKey: .lastMessage)
        unreadCount = try values.decode(Int.self, forKey: .unreadCount)
        archived = try values.decode(Bool.self, forKey: .archived)
        createdAt = try values.decode(String.self, forKey: .createdAt)
        memory = try values.decodeIfPresent(String.self, forKey: .memory)
        pinned = try values.decodeIfPresent(Bool.self, forKey: .pinned) ?? false
        preferences = try values.decodeIfPresent([BotPreference].self, forKey: .preferences) ?? []
        modelOverride = try values.decodeIfPresent(ModelOverride.self, forKey: .modelOverride)
        effectiveModel = try values.decodeIfPresent(ModelOverride.self, forKey: .effectiveModel)
        notifications = try values.decodeIfPresent(Bool.self, forKey: .notifications) ?? true
        approvalOverride = try values.decodeIfPresent(String.self, forKey: .approvalOverride)
        effectiveApproval = try values.decodeIfPresent(String.self, forKey: .effectiveApproval)
        workerLimit = try values.decodeIfPresent(Int.self, forKey: .workerLimit)
        modelError = try values.decodeIfPresent(String.self, forKey: .modelError)
        health = try values.decodeIfPresent(BotHealth.self, forKey: .health)
    }
}

struct ModelOverride: Codable, Equatable, Sendable {
    var model: String
    var provider: String
    var reasoningEffort: String?

    enum CodingKeys: String, CodingKey { case model, provider, reasoningEffort }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(model, forKey: .model)
        try values.encode(provider, forKey: .provider)
        try values.encode(reasoningEffort, forKey: .reasoningEffort)
    }

    var selectionKey: String { provider + "\u{1f}" + model }
}

struct ModelDefaults: Codable, Equatable, Sendable {
    var model: String
    var provider: String
    var baseUrl: String?
    var reasoningEffort: String?
    var supported: Bool? = nil
    var unavailableReason: String? = nil

    var selection: ModelOverride { ModelOverride(model: model, provider: provider, reasoningEffort: reasoningEffort) }
}

struct ModelCatalogEntry: Codable, Equatable, Sendable {
    var id: String
    var name: String
    var provider: String
    var baseUrl: String?
    var reasoningEfforts: [String]
    var defaultReasoningEffort: String? = nil

    var selectionKey: String { provider + "\u{1f}" + id }
}

struct ModelCatalog: Codable, Equatable, Sendable {
    var defaults: ModelDefaults
    var models: [ModelCatalogEntry]
    var modelSource: String?
    var defaultSource: String?
    var loading: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case defaults = "default"
        case models, modelSource, defaultSource, loading
    }

    func entry(for selection: ModelOverride) -> ModelCatalogEntry? {
        models.first { $0.id == selection.model && $0.provider == selection.provider }
    }

    func contains(_ selection: ModelOverride) -> Bool {
        guard let entry = entry(for: selection) else { return false }
        return selection.reasoningEffort.map(entry.reasoningEfforts.contains) ?? true
    }

    func name(for selection: ModelOverride) -> String {
        entry(for: selection)?.name ?? selection.model
    }

    func resolved(_ selection: ModelOverride) -> ModelOverride {
        var result = selection
        result.reasoningEffort = selection.reasoningEffort ?? entry(for: selection)?.defaultReasoningEffort
        return result
    }
}

struct BotModelRequest: Encodable {
    let modelOverride: ModelOverride?

    enum CodingKeys: String, CodingKey { case modelOverride }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(modelOverride, forKey: .modelOverride)
    }
}

struct AgentTask: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var sessionId: String
    var title: String
    var status: String
    var summary: String
    var createdAt: String
    var updatedAt: String
    var scheduledAt: String?
    var threadId: String
    var conversationBotId: String? = nil
    var sourceBotId: String? = nil
    var phase: String? = nil
    var turnStartedAt: String? = nil

    var chatBotId: String { sourceBotId != nil ? botId : (conversationBotId ?? botId) }
    var isRunning: Bool { ["working", "running"].contains(status) }
    var isActive: Bool { ["working", "running", "queued", "waiting", "needs_you", "scheduled", "cancelling"].contains(status) }
}

struct MessageReference: Codable, Equatable, Sendable {
    var messageId: String
    var text: String
    var author: String
    /// "user", "assistant", or "document" for a Library page that the message asks about.
    var role: String
    var documentId: String? = nil

    /// A Library page as the subject of a message: "Ask the bot about this page".
    static func page(_ document: BotDocument) -> MessageReference {
        MessageReference(messageId: "", text: document.title, author: "Library", role: "document", documentId: document.id)
    }
}

struct MessageReactionEvent: Codable, Equatable, Sendable {
    var emoji: String
    var action: String
}

enum MessageReactions {
    static let selectable = ["👍", "👎"]
    static let all = ["👍", "👎", "😄", "🎉", "😕", "❤️", "🚀", "👀"]
    static let names = ["Thumbs up", "Thumbs down", "Laugh", "Celebrate", "Confused", "Heart", "Rocket", "Eyes"]
    static func name(_ emoji: String) -> String { all.firstIndex(of: emoji).map { names[$0] } ?? emoji }
}

struct Message: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var taskId: String?
    var role: String
    var kind: String
    var text: String
    var decisionId: String?
    var artifactId: String?
    var createdAt: String
    var streaming: Bool
    /// The Library document of a "document" message: a card that opens it.
    var documentId: String? = nil
    var threadId: String? = nil
    var senderBotId: String? = nil
    var recipientBotIds: [String]? = nil
    var exchangeRecipientBotIds: [String]? = nil
    var clientRequestId: String? = nil
    var replyTo: MessageReference? = nil
    var workContext: WorkReference? = nil
    var reactions: [String]? = nil
    var reactionEvent: MessageReactionEvent? = nil
}

struct MessageDraft: Codable, Equatable, Sendable {
    var channel: String
    var recipient: String
    var body: String
    var sender: String? = nil
    var subject: String? = nil
    var replyTo: String? = nil
}

struct Decision: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var taskId: String
    var title: String
    var explanation: String
    var actionType: String
    var resource: String
    var consequences: String
    var recommended: String
    var options: [DecisionOption]
    var status: String
    var urgency: String
    var createdAt: String
    var decidedAt: String?
    var decidedBy: String?
    var correction: String?
    var outcome: String?
    var revision: Int
    var draft: MessageDraft? = nil
    var draftHash: String? = nil
    var previews: [DecisionPreview]? = nil
    var previewsHash: String? = nil
    var proposalPreview: String? = nil
    var proposalHash: String? = nil
    var decidedByName: String? = nil
    var recommendedOptionId: String? = nil
    var selectedOptionId: String? = nil
    var selectedOptionLabel: String? = nil
    var previousDecisionId: String? = nil
    var replacesDecisionId: String? = nil
    var replacedByDecisionId: String? = nil

    var context: String? = nil
    var requestText: String? = nil
    var requestMessageId: String? = nil
    var requestBotId: String? = nil
    var requestCreatedAt: String? = nil
    var inboxKind: String? = nil
    var inboxDeferredAt: String? = nil
    var inboxUndoUntil: String? = nil
    var inboxSwipeId: String? = nil
    var uncertainty: String? = nil
    var risk: String? = nil
    var riskLevel: String? = nil
    var assessmentStale: Bool? = nil
    var replyPending: Bool? = nil
    var sources: [DecisionSource]? = nil
    var mergePrUrl: String? = nil
    var mergePrDiff: PullRequestDiff? = nil
    var sourceRefreshing: Bool? = nil

    var cardSources: [DecisionSource] {
        var result = sources ?? []
        if let mergePrUrl, !result.contains(where: { $0.url == mergePrUrl }),
           let url = URL(string: mergePrUrl), url.host == "github.com",
           url.pathComponents.count == 5, url.pathComponents[3] == "pull" {
            result.append(DecisionSource(kind: "pull_request",
                title: "\(url.pathComponents[2]) #\(url.lastPathComponent)", url: mergePrUrl))
        }
        var seen = Set<DecisionSource>()
        return result.filter { !$0.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && seen.insert($0).inserted }
    }

    var cardOptions: [DecisionOption] {
        if options.isEmpty, actionType == "create_file" {
            // File decisions without named choices use the service's direct answer contract.
            return [
                DecisionOption(id: "save-file", label: "Save file", explanation: "Create \(resource) on the Mac.", answer: "approve"),
                DecisionOption(id: "do-not-save", label: "Do not save", explanation: "Create no file.", answer: "reject")
            ]
        }
        // A card shows its recommended choice first. The service refuses a new card in another order, but older
        // cards can list it later. Only the display order changes: each choice keeps its ID and its answer.
        guard let index = options.firstIndex(where: { $0.id == recommendedOptionId }), index > 0 else { return options }
        var ordered = options
        ordered.insert(ordered.remove(at: index), at: 0)
        return ordered
    }

    var cardContext: String? {
        let recap = context?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let text = recap.isEmpty ? explanation.trimmingCharacters(in: .whitespacesAndNewlines) : recap
        var summary = text == title ? "" : text
        let unknown = uncertainty?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if assessmentStale != true, !unknown.isEmpty, !summary.localizedCaseInsensitiveContains(unknown) {
            summary = [summary, unknown].filter { !$0.isEmpty }.joined(separator: "\n\n")
        }
        if actionType == "native_approval", !resource.isEmpty, !summary.contains(resource) {
            return [summary, resource].filter { !$0.isEmpty }.joined(separator: "\n")
        }
        return summary.isEmpty ? nil : summary
    }

    var isProposal: Bool { inboxKind == "proposal" }
    var isRefreshing: Bool { status == "refreshing" || (status == "pending" && sourceRefreshing == true) }
    var isPending: Bool { status == "pending" && !isRefreshing }
}

struct DecisionSource: Codable, Hashable, Sendable {
    var kind: String
    var title: String
    var url: String? = nil

    var link: URL? {
        guard let url, let link = URL(string: url),
              ["https", "http"].contains(link.scheme?.lowercased() ?? ""),
              let host = link.host, !host.isEmpty,
              link.user == nil, link.password == nil else { return nil }
        return link
    }

    var label: String {
        switch kind {
        case "email": "Email"
        case "issue": "Issue"
        case "pull_request": "PR"
        default: "Source"
        }
    }

}

struct DecisionPreview: Codable, Identifiable, Equatable, Sendable {
    var artifactId: String
    var caption: String
    var width: Int
    var height: Int
    var id: String { artifactId }
}

/// The size of a PR's changes, as GitHub counts them for its Files tab.
struct PullRequestDiff: Codable, Equatable, Sendable {
    var additions: Int
    var deletions: Int
    var changedFiles: Int

    var summary: String {
        "+\(additions.formatted()) −\(deletions.formatted()) · \(changedFiles.formatted()) \(changedFiles == 1 ? "file" : "files")"
    }

    var accessibilityLabel: String {
        "\(additions) lines added, \(deletions) lines removed, in \(changedFiles) \(changedFiles == 1 ? "file" : "files")"
    }
}

enum DecisionRisk {
    static func label(_ level: String?) -> String {
        switch level {
        case "low": "Low risk"
        case "medium": "Medium risk"
        case "high": "High risk"
        case "not_assessed": "Risk not assessed"
        default: "Risk"
        }
    }
}

struct DecisionOption: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var label: String
    var explanation: String
    var answer: String
    var action: ProposedFileAction?
    var modification: String?
    var risk: String? = nil
    var riskLevel: String? = nil
}

struct ProposedFileAction: Codable, Equatable, Sendable {
    var type: String
    var filename: String
    var content: String
}

struct WorkReference: Codable, Hashable, Sendable {
    let id: String
    let title: String
}

struct Artifact: Codable, Identifiable, Equatable, Sendable {
    func isPRStatusStale(at date: Date) -> Bool {
        guard kind == "pr", status != "merged", !["closed", "merged"].contains(prState ?? "") else { return false }
        return refreshError != nil || observedAt?.apiDate.map { date.timeIntervalSince($0) > 120 } != false
    }

    func cardStatus(at date: Date) -> String {
        if isPRStatusStale(at: date) { return "status_out_of_date" }
        if kind == "pr", ["merged", "closed"].contains(prState ?? "") { return prState! }
        return status
    }

    func cardPreview(at date: Date) -> String {
        if isPRStatusStale(at: date) { return "The saved status is out of date. Open the pull request for its current status." }
        if kind == "pr", prState == "merged" || status == "merged" { return "GitHub confirms this pull request merged." }
        if kind == "pr", prState == "closed" { return "The pull request closed without a merge." }
        return preview
    }

    var id: String
    var botId: String
    var taskId: String?
    var title: String
    var kind: String
    var status: String
    var preview: String
    var url: String?
    var createdAt: String
    var observedAt: String? = nil
    var headSha: String? = nil
    var refreshError: String? = nil
    var prState: String? = nil
    var draft: Bool? = nil
    var checksState: String? = nil
}



struct Routine: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var name: String
    var prompt: String
    var cron: String
    var timezone: String
    var enabled: Bool
    var nextRunAt: String?
    var lastRunAt: String?
    var lastTaskId: String?
    var createdAt: String
    var updatedAt: String
    var script: String? = nil
    /// The script that Hermes runs before the agent of this routine. It can skip the run. Older Macs do not send it.
    var scriptCheck: String? = nil
    var error: String? = nil
    /// True for a Hermes job that runs one time. Older Macs do not send it.
    var oneTime: Bool? = nil
    /// The Hermes job state, for example "scheduled", "paused", or "completed". Older Macs do not send it.
    var state: String? = nil
    /// The time of a one-time job. Hermes keeps it when it clears `nextRunAt`.
    var runAt: String? = nil
    var scheduleLabel: String { enabled ? cron : "Paused · " + cron }
}

/// The routines of one bot settings page: recurring routines, and one-time jobs that still run.
/// The page hides a finished one-time job and a paused one-time job whose time has passed.
struct RoutineGroups: Equatable {
    var recurring: [Routine] = []
    var scheduledChecks: [Routine] = []

    init(_ routines: [Routine], now: Date = Date()) {
        var checks: [(routine: Routine, time: Date?)] = []
        for routine in routines {
            guard routine.oneTime == true else {
                recurring.append(routine)
                continue
            }
            if routine.state == "completed" || routine.state == "error" { continue }
            let time = (routine.nextRunAt ?? routine.runAt)?.apiDate
            let paused = routine.state == "paused" || !routine.enabled
            if paused, (time ?? .distantPast) < now { continue }
            checks.append((routine, time))
        }
        scheduledChecks = checks.sorted { ($0.time ?? .distantFuture) < ($1.time ?? .distantFuture) }.map(\.routine)
    }
}

struct LearningGroup: Codable, Identifiable, Equatable, Sendable {
    var botId: String
    var actionType: String
    var total: Int
    var agreed: Int
    var agreement: Double
    var suggestion: String?
    var id: String { botId + ":" + actionType }
}

struct StateSnapshot: Codable, Sendable {
    var syncVersion: Int? = nil
    var contentOnly: Bool? = nil
    var historyBefore: [String: Int]? = nil
    var confirmedRequestIds: [String]? = nil
    var bots: [Bot]
    var tasks: [AgentTask]
    var messages: [Message]
    var decisions: [Decision]
    var artifacts: [Artifact]
    var cursor: Int
    var serverTime: String
    var provider: String
    var model: String
    var modelConfigured: Bool
    var baseUrl: String? = nil
    var routines: [Routine]? = nil
    var modelCatalog: ModelCatalog? = nil
    var approvals: ApprovalSettings? = nil
    var parallelWork: ParallelWorkSettings? = nil
    var fallback: FallbackSettings? = nil
    var workCapacity: ParallelWorkSettings? = nil
    var mcpServers: [MCPServer]? = nil
    var pushConfigured: Bool? = nil
}

struct ServerEvent: Decodable, Sendable {
    let seq: Int
    let type: String
    let botId: String?
    let taskId: String?
    let createdAt: String
    let message: Message?

    private enum CodingKeys: String, CodingKey { case seq, type, botId, taskId, createdAt, data }
    private struct Visibility: Decodable { let _hidden: Bool?; let _deleted: Bool? }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        seq = try values.decode(Int.self, forKey: .seq)
        type = try values.decode(String.self, forKey: .type)
        botId = try values.decodeIfPresent(String.self, forKey: .botId)
        taskId = try values.decodeIfPresent(String.self, forKey: .taskId)
        createdAt = try values.decode(String.self, forKey: .createdAt)
        let visibility = try? values.decode(Visibility.self, forKey: .data)
        if type == "message.updated", visibility?._hidden != true, visibility?._deleted != true {
            message = try? values.decode(Message.self, forKey: .data)
        } else { message = nil }
    }
}

struct EventPage: Decodable, Sendable {
    let events: [ServerEvent]
    let cursor: Int
    let serverTime: String
}

enum ConnectionState: String, Sendable {
    // `disconnected` names no cause. A failure of the relay tunnel names its cause.
    case unconfigured, connecting, connected, disconnected, macOffline, relayUnreachable, needsRepair, preview

    init(_ failure: TunnelError) {
        switch TunnelConnection.State(failure) {
        case .macOffline: self = .macOffline
        case .needsRepair: self = .needsRepair
        case .connecting: self = .connecting
        default: self = .relayUnreachable
        }
    }

    // Settings and the connection row use this one name for each state.
    var title: String {
        switch self {
        case .unconfigured: "Not paired"
        case .connecting: "Connecting…"
        case .connected: "Connected"
        case .disconnected: "Waiting for your Mac"
        case .macOffline: "Mac offline"
        case .relayUnreachable: "Relay unreachable"
        case .needsRepair: "Needs a new pairing link"
        case .preview: "Preview"
        }
    }

    // A calm banner names these states. The cached state stays visible below it.
    var bannerMessage: String? {
        switch self {
        case .disconnected: "Coconut cannot reach your Mac. It tries again on its own."
        case .macOffline: "Your Mac is offline. Coconut shows the last known state."
        case .relayUnreachable: "Coconut cannot reach the relay. It tries again on its own."
        case .needsRepair: "This iPhone needs a new pairing link from your Hermes."
        default: nil
        }
    }

    // The banner for a state that needs a new pairing link offers Pair.
    var notice: ConnectionNotice? {
        if self == .needsRepair, let bannerMessage { return ConnectionNotice(message: bannerMessage, offersPairing: true) }
        return bannerMessage.map { ConnectionNotice(message: $0, offersPairing: false) }
    }
}

struct ConnectionNotice: Equatable, Sendable {
    let message: String
    let offersPairing: Bool
}

// GET /v1/setup. The app reads only the state of the Mac's Hermes. A Mac before this field sends no status.
struct HermesSetup: Codable, Equatable, Sendable {
    var hermes: HermesRuntimeStatus? = nil
}

struct HermesRuntimeStatus: Codable, Equatable, Sendable {
    var version: String?
    // False when Coconut has not tested this Hermes version. Nil when the Mac does not know.
    var tested: Bool?

    var untestedWarning: String? {
        guard tested == false else { return nil }
        return "Hermes \(version ?? "on this Mac") is not tested with Coconut. Some features can fail."
    }
}

enum APIJSON {
    static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    static func encode<T: Encodable>(_ value: T) throws -> Data { try encoder().encode(value) }
}

extension String {
    var apiDate: Date? {
        (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(self))
            ?? (try? Date.ISO8601FormatStyle().parse(self))
    }
}

struct ApprovalMode: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let label: String
    let description: String
}

struct ApprovalSettings: Codable, Equatable, Sendable {
    let defaults: String
    let modes: [ApprovalMode]
    enum CodingKeys: String, CodingKey { case defaults = "default", modes }
    func label(for value: String?) -> String { modes.first { $0.id == value }?.label ?? "Hermes default" }
}

struct MCPServer: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let url: String
    let transport: String
    let hasToken: Bool
    let editable: Bool
}

struct MCPResponse: Decodable, Sendable {
    let ok: Bool
    let servers: [MCPServer]
    let tools: [String]
    var error: String?
}

struct MCPRequest: Encodable {
    let action: String
    let name: String
    var url: String? = nil
    var token: String? = nil
}

/// The fallback chain of the default Hermes profile, in order. Hermes tries each model when the default model fails.
struct FallbackSettings: Codable, Equatable, Sendable {
    var models: [FallbackModel]
}

struct FallbackModel: Codable, Equatable, Hashable, Sendable {
    var model: String
    var provider: String

    var selection: ModelOverride { ModelOverride(model: model, provider: provider, reasoningEffort: nil) }
    var selectionKey: String { selection.selectionKey }
}

struct FallbackRequest: Encodable {
    let models: [FallbackModel]
}

/// The position of the first message with each ID and each client request ID. AppStore makes it again each time its
/// messages change, so a lookup finds what a scan from the start finds.
struct MessageIndex: Sendable {
    private var byId: [String: Int] = [:]
    private var byRequest: [String: Int] = [:]

    init(_ messages: [Message]) {
        byId.reserveCapacity(messages.count)
        for (position, message) in messages.enumerated() {
            if byId[message.id] == nil { byId[message.id] = position }
            if let request = message.clientRequestId, byRequest[request] == nil { byRequest[request] = position }
        }
    }

    /// The first position whose message has this ID, or, when `clientRequestId` is set, this client request ID.
    func first(id: String, clientRequestId: String? = nil) -> Int? {
        let request = clientRequestId.flatMap { byRequest[$0] }
        if let found = byId[id], let request { return min(found, request) }
        return byId[id] ?? request
    }
}
