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
        case lastMessage, unreadCount, archived, createdAt, memory, pinned, preferences, modelOverride, effectiveModel, modelError, notifications, approvalOverride, effectiveApproval, workerLimit
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
    var role: String
}

struct MessageReactionEvent: Codable, Equatable, Sendable {
    var emoji: String
    var action: String
}

enum MessageReactions {
    static let selectable = ["👍", "👎"]
    static let all = ["👍", "👎", "😄", "🎉", "😕", "❤️", "🚀", "👀"]
    static let names = ["Thumbs up", "Thumbs down", "Laugh", "Celebrate", "Confused", "Heart", "Rocket", "Eyes"]
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
        guard options.isEmpty, actionType == "create_file" else { return options }
        // File decisions without named choices use the service's direct answer contract.
        return [
            DecisionOption(id: "save-file", label: "Save file", explanation: "Create \(resource) on the Mac.", answer: "approve"),
            DecisionOption(id: "do-not-save", label: "Do not save", explanation: "Create no file.", answer: "reject")
        ]
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

    var workIdentity: String {
        if kind == "work" || workOutcomeId != nil { return "work:" + (workOutcomeId ?? id) }
        return url?.lowercased() ?? id
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
    var workOutcomeId: String? = nil
    var nativeTaskId: String? = nil
    var checkedAt: String? = nil
    var observedAt: String? = nil
    var headSha: String? = nil
    var refreshError: String? = nil
    var prState: String? = nil
    var draft: Bool? = nil
    var checksState: String? = nil
    var activity: String? = nil
    var activityState: String? = nil
    var activityObservedAt: String? = nil
    var activityBotId: String? = nil
    var workDismissalId: String? = nil
    var workState: String? = nil
    var checkoutRecoveryStopped: Bool? = nil
    var workNextStep: String? = nil
    var workDecisionId: String? = nil
    var workQueuePosition: Int? = nil
    var completionCondition: String? = nil
    var completionEvidence: String? = nil
    var nativeStatus: String? = nil
    var nativeLastActivityAt: String? = nil
    var nativeDiagnostics: [NativeDiagnostic]? = nil
}

/// A distress signal that Hermes computes for a Kanban task, such as a task blocked for a day without a comment.
struct NativeDiagnostic: Codable, Equatable, Sendable {
    var kind: String
    var severity: String
    var title: String
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
    var error: String? = nil
    var scheduleLabel: String { enabled ? cron : "Paused · " + cron }
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
    /// Bots whose Task details can load finished Work that the Mac left out. The phone removes a bot when it loads it.
    var olderWorkBotIds: [String]? = nil
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
    var apiFallback: APIFallbackSettings? = nil
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
    // `disconnected` is for a version 1 pairing over TLS. A version 2 pairing names the failure.
    case unconfigured, connecting, connected, disconnected, macOffline, relayUnreachable, needsRepair, preview

    init(_ failure: TunnelError) {
        switch TunnelConnection.State(failure) {
        case .macOffline: self = .macOffline
        case .needsRepair: self = .needsRepair
        default: self = .relayUnreachable
        }
    }

    // A calm banner names these states. The cached state stays visible below it.
    var bannerMessage: String? {
        switch self {
        case .macOffline: "Your Mac is offline. Coconut shows the last known state."
        case .relayUnreachable: "Coconut cannot reach the relay. It tries again on its own."
        case .needsRepair: "This iPhone needs a new pairing link from your Hermes."
        default: nil
        }
    }
}

struct HermesModelChoice: Codable, Equatable, Sendable {
    var model: String
    var provider: String?
}

struct HermesSetupProfile: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var name: String
    var soul: Bool
    var memories: Int
    var skills: Int
    var botId: String?

    var contents: String {
        var parts = soul ? ["Personality"] : []
        if memories > 0 { parts.append(memories == 1 ? "1 memory file" : "\(memories) memory files") }
        if skills > 0 { parts.append(skills == 1 ? "1 skill" : "\(skills) skills") }
        return parts.isEmpty ? "Empty profile" : parts.joined(separator: " · ")
    }
}

struct HermesLogin: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var label: String
}

struct HermesMCPServer: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var transport: String
    // The server signs in with OAuth or refers to a variable that the setup does not define.
    var needsSetup: Bool
}

struct HermesSetup: Codable, Equatable, Sendable {
    var found: Bool
    var profiles: [HermesSetupProfile]
    // API keys that Coconut can copy. OAuth sign-ins cannot be shared between two Hermes homes.
    var logins: [HermesLogin]
    var signIns: [HermesLogin]
    var model: HermesModelChoice?
    // A Mac before this field sends no MCP servers.
    var mcpServers: [HermesMCPServer]? = nil

    var importable: [HermesSetupProfile] { profiles.filter { $0.botId == nil } }

    // Coconut adds a server only when it has no server with the same name.
    func newMCPServers(existing: [MCPServer]) -> [HermesMCPServer] {
        let names = Set(existing.map(\.id))
        return (mcpServers ?? []).filter { !names.contains($0.id) }
    }

    // The setup's default model, when the Mac's catalog has the same route. Without a provider, exactly one entry must match.
    func modelChoice(in catalog: ModelCatalog?) -> ModelOverride? {
        guard let model, let catalog else { return nil }
        let matches = catalog.models.filter { $0.id == model.model && (model.provider == nil || $0.provider == model.provider) }
        guard matches.count == 1, let entry = matches.first else { return nil }
        return ModelOverride(model: entry.id, provider: entry.provider, reasoningEffort: nil)
    }

    static let sample = HermesSetup(found: true, profiles: [
        HermesSetupProfile(id: "default", name: "Hermes", soul: true, memories: 2, skills: 4, botId: nil),
        HermesSetupProfile(id: "profile:writer", name: "Writer", soul: true, memories: 1, skills: 0, botId: nil),
    ], logins: [HermesLogin(id: "ANTHROPIC_API_KEY", label: "Anthropic API key")],
    signIns: [HermesLogin(id: "nous", label: "Nous")],
    model: HermesModelChoice(model: "claude-opus-5-5", provider: "anthropic"),
    mcpServers: [HermesMCPServer(id: "github", transport: "stdio", needsSetup: false),
                 HermesMCPServer(id: "notion", transport: "http", needsSetup: true)])
}

struct HermesImportRequest: Encodable, Sendable {
    var profileIds: [String]
    var logins: Bool
    var mcpServers = false
}

struct HermesImportResult: Decodable, Sendable {
    var bots: [Bot]
    var logins: [String]
    var skippedSkills: [String]? = nil
    var mcpServers: [String]? = nil
    // Added servers that stay off until the person finishes them in Hermes on the Mac.
    var mcpNeedsSetup: [String]? = nil
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

struct APIFallbackSettings: Codable, Equatable, Sendable {
    var enabled: Bool
    var available: Bool
    var model: String?
    var canEnable: Bool
}

struct APIFallbackRequest: Encodable {
    let enabled: Bool
}
