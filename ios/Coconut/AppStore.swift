import Foundation
import Observation
import UIKit
import CryptoKit
import Network
import OSLog

@MainActor @Observable
final class AppStore {
    private static let deliveryLog = Logger(subsystem: "com.gumroad.coconut", category: "delivery")
    var bots: [Bot] = []
    var tasks: [AgentTask] = []
    var messages: [Message] = []
    var outgoingMessages: [OutgoingMessage] = []
    var outgoingAnswers: [OutgoingAnswer] = []
    @ObservationIgnored private var deliveryTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var deliveryID = UUID()
    var reactionOverrides: [String: [String]] = [:]
    var decisions: [Decision] = []
    var answeringDecisionIds: Set<String> = []
    var decisionReplyDrafts: [String: String] = [:]
    var artifacts: [Artifact] = []
    var workVisibility: [String: Bool] = [:]
    var workUndo: [WorkDismissal] = []
    @ObservationIgnored private var workMutationTail: Task<Void, Never>?
    @ObservationIgnored private var workMutationIds: [String: UUID] = [:]
    var approvals: ApprovalSettings?
    var parallelWork: ParallelWorkSettings?
    var apiFallback: APIFallbackSettings?
    var workCapacity: ParallelWorkSettings?
    var routines: [Routine] = []
    var learning: [LearningGroup] = []
    var connection: ConnectionState = .unconfigured
    var lastSync: Date?
    var error: String?
    var syncIssue: String?
    var serverURL = "https://mac-mini.local:8960"
    var fingerprint = ""
    var pairCode = ""
    var model = ""
    var modelBaseURL = ""
    var modelConfigured = false
    var mcpServers: [MCPServer] = []
    var modelCatalog: ModelCatalog?
    var provider = ""
    var pushConfigured = false
    var pushRegistrationError: String?
    @ObservationIgnored private var registeredPushSignature: String?
    @ObservationIgnored private var registeringPush = false
    @ObservationIgnored private var pushSyncRequested = false
    let isDemo: Bool

    private var pairing: PairingConfiguration?
    private var writeCount = 0
    @ObservationIgnored private var client: APIClient?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var foreground = false
    @ObservationIgnored private var cursor = 0
    @ObservationIgnored private var connectionGeneration = UUID()
    @ObservationIgnored private var bootstrapPairing = false
    @ObservationIgnored private var uncertainWriteKeys: [String: String] = [:]
    @ObservationIgnored private var networkMonitor: NWPathMonitor?
    @ObservationIgnored private var networkAvailable: Bool?
    @ObservationIgnored private var cachedState: StateSnapshot?
    @ObservationIgnored private var cacheTask: Task<Void, Never>?
    @ObservationIgnored private let cacheQueue = DispatchQueue(label: "coconut.display-cache", qos: .utility)
    @ObservationIgnored private var nativeSettingsTask: Task<Void, Never>?
    @ObservationIgnored private var nativeSettingsReadAt: Date?
    @ObservationIgnored private var nativeSettingsAttemptAt: Date?
    @ObservationIgnored private var nativeSettingsPending = false
    @ObservationIgnored private var nativeSettingsRequiredCursor = 0
    var historyLoading: Set<String> = []
    var remoteSearchQuery = ""
    var remoteSearchBotIds: Set<String> = []
    @ObservationIgnored private var snapshotEpoch = UUID()
    @ObservationIgnored private var snapshotRequest: (id: UUID, generation: UUID, epoch: UUID, client: APIClient, task: Task<StateSnapshot, Error>)?

    var hasPairing: Bool { pairing != nil }
    var canSubmitDecision: Bool { isDemo || hasPairing }
    var isBusy: Bool { writeCount > 0 }
    var pendingDecisions: [Decision] { decisions.filter(\.isPending) }
    var inboxBadgeCount: Int { pendingDecisions.filter { !answeringDecisionIds.contains($0.id) }.count }
    var reconciledBadgeCount: Int? { lastSync.map { _ in inboxBadgeCount } }
    @ObservationIgnored var setBadge: (Int) -> Void = { PushNotifications.shared.setBadgeCount($0) }

    func reconcileBadge() {
        if let count = reconciledBadgeCount { setBadge(count) }
    }
    var activeTasks: [AgentTask] { tasks.filter(\.isActive) }
    func isBotActive(_ bot: Bot) -> Bool {
        guard !bot.archived else { return false }
        if outgoingMessages.contains(where: { $0.expectsResponse && $0.executorIds.contains(bot.id) }) { return true }
        guard isDemo || connection == .connected else { return false }
        // A pending decision can mask independent work in the bot's aggregate status.
        return ["working", "running"].contains(bot.status)
            || tasks.contains { $0.botId == bot.id && ["working", "running"].contains($0.status) }
    }
    func isConversationResponding(_ botId: String) -> Bool {
        if outgoingMessages.contains(where: { $0.botId == botId && $0.expectsResponse }) { return true }
        guard isDemo || connection == .connected else { return false }
        return tasks.contains { $0.chatBotId == botId && ["working", "running"].contains($0.status) }
            || messages.contains { $0.botId == botId && $0.role == "assistant" && $0.streaming }
    }
    func conversationBotId(for decision: Decision) -> String {
        tasks.first { $0.id == decision.taskId }?.chatBotId ?? decision.botId
    }

    func decisionChatRoute(for decision: Decision) -> ConversationRoute {
        ConversationRoute(botId: conversationBotId(for: decision), decisionId: decision.id)
    }

    func decisionActor(_ decision: Decision) -> String {
        if let deviceId = pairing?.deviceId, decision.decidedBy == deviceId { return "You" }
        if let name = decision.decidedByName?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty { return name }
        switch decision.decidedBy {
        case "cancellation": return "Task cancellation"
        case "You", "you", "You (preview)": return "You"
        default: return "Paired device"
        }
    }

    var connectionDetail: String {
        switch connection {
        case .preview: return "Preview data. No agents run in this mode."
        case .unconfigured: return "Pair with your Mac to start."
        case .connecting: return "Connecting to your Mac…"
        case .connected: return "Your Mac runs the agents."
        case .disconnected: return "The phone cannot confirm current activity. Work can continue on your Mac."
        }
    }

    init(demo: Bool = ProcessInfo.processInfo.arguments.contains("--demo")) {
        isDemo = demo
        if demo {
            setBadge = { _ in }
            apply(Self.previewSnapshot(), persist: false)
            connection = .preview
            return
        }
        if let configuration: PairingConfiguration = ProtectedCache.read("pairing.json"), PairingToken.read() != nil {
            workVisibility = [:]; workUndo = []; workMutationIds = [:]
            pairing = configuration
            serverURL = configuration.serverURL
            fingerprint = configuration.fingerprint
            connection = .disconnected
            if let cache: OutgoingCache = ProtectedCache.read("outgoing.json"), cache.deviceId == configuration.deviceId {
                outgoingMessages = cache.messages.map(\.afterRestart)
                outgoingAnswers = cache.answers ?? []
                answeringDecisionIds = Set(outgoingAnswers.map(\.decisionId))
            }
            if let cache: SnapshotCache = ProtectedCache.read("snapshot.json") {
                apply(cache.snapshotForRestore, persist: false)
                lastSync = cache.syncedAt
            }
        }
        #if DEBUG && targetEnvironment(simulator)
        configureFromLaunchEnvironment()
        #endif
    }

    func start() {
        if !isDemo, networkMonitor == nil {
            let monitor = NWPathMonitor()
            monitor.pathUpdateHandler = { [weak self] path in
                let available = path.status == .satisfied
                Task { @MainActor [weak self] in self?.networkChanged(available: available) }
            }
            monitor.start(queue: DispatchQueue(label: "coconut.network"))
            networkMonitor = monitor
        }
        setForeground(true)
    }

    func networkChanged(available: Bool) {
        let recovered = networkAvailable == false && available
        networkAvailable = available
        // A default route does not prove whether the Mac is reachable.
        if recovered, hasPairing, foreground {
            stopPolling()
            client = nil
            beginPolling()
        }
    }

    private func stopPolling() {
        connectionGeneration = UUID()
        pollTask?.cancel()
        pollTask = nil
        nativeSettingsPending = false
        nativeSettingsTask?.cancel()
        nativeSettingsTask = nil
        snapshotRequest?.task.cancel()
        snapshotRequest = nil
    }

    func setForeground(_ active: Bool) {
        guard active != foreground else { return }
        foreground = active
        guard !isDemo else { return }
        if active {
            Task { await PushNotifications.shared.refresh(); await syncPushRegistration() }
            if bootstrapPairing {
                bootstrapPairing = false
                Task { await pair() }
                return
            }
            guard pairing != nil, pollTask == nil else { return }
            if lastSync.map({ Date().timeIntervalSince($0) > 30 }) ?? true { connection = .connecting }
            beginPolling()
        } else {
            stopPolling()
            if let cachedState { saveCache(cachedState, immediately: true) }
        }
    }

    func pair() async {
        guard !isDemo else { return }
        guard !pairCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            error = "Enter the one-time pairing code shown on your Mac."
            return
        }
        writeCount += 1
        defer { writeCount -= 1 }
        connection = .connecting
        do {
            let address = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
            let pin = APIClient.normalizeFingerprint(fingerprint)
            let candidate = try APIClient(serverURL: address, fingerprint: pin, token: nil)
            let response: PairResponse = try await candidate.request("pair", method: "POST", body: APIJSON.encode(
                PairRequest(code: pairCode.trimmingCharacters(in: .whitespacesAndNewlines), deviceName: UIDevice.current.name)), retryTransient: false)
            disconnect()
            try PairingToken.save(response.token)
            let configuration = PairingConfiguration(serverURL: address, fingerprint: pin, deviceId: response.deviceId)
            try ProtectedCache.write(configuration, name: "pairing.json")
            resetDelivery()
            outgoingMessages = []; outgoingAnswers = []; answeringDecisionIds = []
            ProtectedCache.remove("outgoing.json")
            workVisibility = [:]; workUndo = []; workMutationIds = [:]
            pairing = configuration
            pairCode = ""
            cursor = 0
            error = nil
            await reconnect()
        } catch {
            report(error)
            if let networkError = error as? URLError, [.networkConnectionLost, .timedOut].contains(networkError.code) {
                self.error = "The pairing result is unknown. Get a fresh one-time code from your Mac, then pair again."
            }
            connection = pairing == nil ? .unconfigured : .disconnected
        }
    }

    func reconnect() async {
        guard !isDemo else { return }
        stopPolling()
        guard let pairing, let token = PairingToken.read() else {
            connection = .unconfigured
            return
        }
        let generation = connectionGeneration
        if connection != .connected { connection = .connecting }
        do {
            client = try APIClient(serverURL: pairing.serverURL, fingerprint: pairing.fingerprint, token: token)
            if let client { try await refresh(using: client) }
            if foreground { beginPolling(refreshImmediately: false) }
        } catch {
            guard connectionGeneration == generation else { return }
            recordSyncFailure(error)
            if foreground { beginPolling() }
        }
    }

    func disconnect() {
        workVisibility = [:]; workUndo = []; workMutationIds = [:]
        if let deviceId = pairing?.deviceId {
            PushRelayClient.shared.disable(deviceId: deviceId)
            NotificationKeys.forget(deviceId: deviceId)
        }
        if let client {
            Task { let _: PushRegistrationResponse? = try? await client.request("device/push", method: "DELETE", body: Data("{}".utf8), idempotencyKey: UUID().uuidString) }
        }
        registeredPushSignature = nil
        pushConfigured = false
        PushNotifications.shared.target = nil
        stopPolling()
        cacheTask?.cancel()
        cachedState = nil
        syncIssue = nil
        client = nil
        uncertainWriteKeys = [:]
        resetDelivery()
        outgoingMessages = []; outgoingAnswers = []; answeringDecisionIds = []
        pairing = nil
        if !isDemo {
            PairingToken.delete()
            ProtectedCache.remove("pairing.json")
            cacheQueue.sync { ProtectedCache.remove("snapshot.json") }
            ProtectedCache.remove("outgoing.json")
        }
        bots = []; tasks = []; messages = []; decisions = []; artifacts = []; learning = []
        routines = []
        approvals = nil; parallelWork = nil; apiFallback = nil; workCapacity = nil; mcpServers = []
        reactionOverrides = [:]; decisionReplyDrafts = [:]; answeringDecisionIds = []
        cursor = 0
        lastSync = nil
        setBadge(0)
        model = ""
        modelBaseURL = ""
        modelConfigured = false
        modelCatalog = nil
        nativeSettingsReadAt = nil
        nativeSettingsAttemptAt = nil
        nativeSettingsRequiredCursor = 0
        remoteSearchQuery = ""
        remoteSearchBotIds = []
        provider = ""
        pairCode = ""
        connection = .unconfigured
    }

    func createBot(name: String, role: String, avatar: String, color: String, instructions: String, memory: String = "", pinned: Bool = false) async {
        if isDemo {
            let bot = Bot(id: UUID().uuidString, sessionId: UUID().uuidString, name: name, role: role, avatar: avatar,
                          color: color, instructions: instructions, status: "idle", lastMessage: "Ready when you are.",
                          unreadCount: 0, archived: false, createdAt: Self.now, memory: memory, pinned: pinned)
            bots.append(bot)
            return
        }
        let _: Bot? = await write("bots", payload: BotRequest(name: name, role: role, avatar: avatar, color: color, instructions: instructions, memory: memory, pinned: pinned), as: Bot.self)
    }

    func updateBot(_ bot: Bot, comparedTo original: Bot) async {
        guard bot.id == original.id else { return }
        let patch = BotPatch(updated: bot, original: original)
        guard !patch.isEmpty else { return }
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) {
                patch.apply(to: &bots[index])
            }
            return
        }
        let _: Bot? = await write("bots/\(bot.id)", method: "PATCH", payload: patch, as: Bot.self)
    }

    func updateBotInstructions(bot: Bot, text: String) async {
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].instructions = text }
            return
        }
        let _: Bot? = await write("bots/\(bot.id)", method: "PATCH", payload: InstructionsRequest(instructions: text), as: Bot.self)
    }





    func createRoutine(bot: Bot, name: String, prompt: String, cron: String, timezone: String, enabled: Bool = true) async {
        if isDemo {
            routines.append(Routine(id: UUID().uuidString, botId: bot.id, name: name, prompt: prompt, cron: cron, timezone: timezone, enabled: enabled, nextRunAt: nil, lastRunAt: nil,
                                    lastTaskId: nil, createdAt: Self.now, updatedAt: Self.now))
            return
        }
        let _: Routine? = await write("bots/\(bot.id)/routines", payload: RoutineRequest(name: name, prompt: prompt, cron: cron, timezone: timezone, enabled: enabled), as: Routine.self)
    }

    func updateRoutine(_ routine: Routine) async {
        if isDemo {
            if let index = routines.firstIndex(where: { $0.id == routine.id }) {
                routines[index].name = routine.name
                routines[index].prompt = routine.prompt
                routines[index].cron = routine.cron
                routines[index].timezone = routine.timezone
                routines[index].enabled = routine.enabled
                routines[index].updatedAt = Self.now
                routines[index].nextRunAt = nil
            }
            return
        }
        let _: Routine? = await write("routines/\(routine.id)", method: "PATCH", payload: RoutineRequest(name: routine.name, prompt: routine.prompt,
            cron: routine.cron, timezone: routine.timezone, enabled: routine.enabled), as: Routine.self)
    }

    func deleteRoutine(_ routine: Routine) async {
        if isDemo { routines.removeAll { $0.id == routine.id }; return }
        let _: Acknowledgement? = await write("routines/\(routine.id)", method: "DELETE", payload: EmptyRequest(), as: Acknowledgement.self)
    }

    @discardableResult
    func runRoutine(_ routine: Routine) async -> AgentTask? {
        if isDemo {
            guard let index = routines.firstIndex(where: { $0.id == routine.id }) else { return nil }
            let routine = routines[index]
            guard let bot = bots.first(where: { $0.id == routine.botId }), !bot.archived else {
                error = "Restore this bot before running its routine."
                return nil
            }
            if let taskId = routine.lastTaskId,
               let active = tasks.first(where: { $0.id == taskId && ["queued", "working", "scheduled", "needs_you", "paused"].contains($0.status) }) {
                return active
            }
            let id = UUID().uuidString
            let task = AgentTask(id: id, botId: bot.id, sessionId: bot.sessionId, title: routine.name, status: "queued", summary: "Preview routine. No agent execution.", createdAt: Self.now,
                                 updatedAt: Self.now, scheduledAt: nil, threadId: id, conversationBotId: bot.id)
            tasks.append(task)
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: id, role: "user", kind: "text", text: routine.prompt,
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: id))
            routines[index].lastRunAt = Self.now
            routines[index].lastTaskId = id
            routines[index].updatedAt = Self.now
            return task
        }
        return await write("routines/\(routine.id)/run", payload: EmptyRequest(), as: AgentTask.self)
    }

    func pinBot(_ bot: Bot, pinned: Bool) async {
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].pinned = pinned }
            return
        }
        let _: Bot? = await write("bots/\(bot.id)", method: "PATCH", payload: PinRequest(pinned: pinned), as: Bot.self)
    }

    func archiveBot(_ bot: Bot) async {
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].archived.toggle() }
            return
        }
        let _: Bot? = await write("bots/\(bot.id)", method: "PATCH", payload: ArchiveRequest(archived: !bot.archived), as: Bot.self)
    }

    func deleteBot(_ bot: Bot) async {
        if isDemo {
            bots.removeAll { $0.id == bot.id }; tasks.removeAll { $0.botId == bot.id }; messages.removeAll { $0.botId == bot.id }
            decisions.removeAll { $0.botId == bot.id }; artifacts.removeAll { $0.botId == bot.id }
            routines.removeAll { $0.botId == bot.id }
            return
        }
        let _: Acknowledgement? = await write("bots/\(bot.id)", method: "DELETE", payload: EmptyRequest(), as: Acknowledgement.self)
    }

    func markRead(_ bot: Bot) async {
        guard bot.unreadCount > 0 else { return }
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].unreadCount = 0 }
            return
        }
        let _: Acknowledgement? = await write("bots/\(bot.id)/read", payload: EmptyRequest(), as: Acknowledgement.self, quiet: true)
    }

    @discardableResult
    func send(bot: Bot, text: String, mode: String = "auto", taskId: String? = nil, attachments: [String] = [], scheduledAt: String? = nil, mentionedBotIds: [String] = [], replyTo: MessageReference? = nil, workContext: WorkReference? = nil, workStatusOnly: Bool = false) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !attachments.isEmpty else { return false }
        guard ["auto", "steer", "queue"].contains(mode) else { return false }
        let selectedMode = workContext != nil ? "queue" : (scheduledAt == nil ? mode : "queue")
        let currentTask = selectedMode == "auto"
            ? tasks.last { $0.botId == bot.id && $0.chatBotId == bot.id && $0.sourceBotId == nil && $0.scheduledAt == nil && ["working", "queued", "waiting"].contains($0.status) }
            : taskId.flatMap { id in tasks.first { $0.id == id && $0.chatBotId == bot.id } }
        if selectedMode == "steer" {
            guard let currentTask, ["working", "needs_you"].contains(currentTask.status) else {
                error = "The selected task cannot receive steering now. Queue a new task instead."
                return false
            }
        }
        if isDemo {
            let target: AgentTask
            if selectedMode == "queue" || (selectedMode == "auto" && currentTask == nil) {
                let id = UUID().uuidString
                target = AgentTask(id: id, botId: bot.id, sessionId: bot.sessionId, title: String(text.prefix(72)), status: scheduledAt == nil ? "queued" : "scheduled", summary: "Preview: no agent execution.",
                                   createdAt: Self.now, updatedAt: Self.now, scheduledAt: scheduledAt, threadId: id, conversationBotId: bot.id)
                tasks.append(target)
            } else if let currentTask { target = currentTask }
            else { return false }
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "user", kind: "text", text: text,
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId, replyTo: replyTo, workContext: workContext))
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "system", kind: "text",
                                    text: "Preview: no agent execution.",
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            return true
        }
        let request = MessageRequest(text: text, mode: selectedMode,
            taskId: selectedMode == "steer" ? currentTask?.id : nil, scheduledAt: scheduledAt,
            attachmentIds: attachments, recipientBotIds: [], replyToMessageId: replyTo?.messageId, mentionedBotIds: mentionedBotIds, workArtifactId: workContext?.id, workStatusOnly: workStatusOnly)
        let outgoing = OutgoingMessage(id: UUID().uuidString, botId: bot.id,
            executorIds: [selectedMode == "steer" ? currentTask!.botId : bot.id],
            request: request, createdAt: Self.now, status: .sending, replyTo: replyTo, workContext: workContext, sequence: nextDeliverySequence)
        outgoingMessages.append(outgoing)
        do { try saveOutgoing() }
        catch {
            outgoingMessages.removeAll { $0.id == outgoing.id }
            report(error)
            return false
        }
        scheduleDelivery()
        // The draft is saved locally even when the Mac has not confirmed it yet.
        return true
    }

    func retryOutgoing(_ id: String) async {
        guard let index = outgoingMessages.firstIndex(where: { $0.id == id && $0.status == .failed }) else { return }
        outgoingMessages[index].status = .sending
        scheduleDelivery()
    }

    private func deliverOutgoing(_ id: String) async {
        guard let outgoing = outgoingMessages.first(where: { $0.id == id }) else { return }
        let result: AgentTask? = await write("bots/\(outgoing.botId)/messages", payload: outgoing.request,
            as: AgentTask.self, quiet: true, refreshInBackground: true, idempotencyKey: id, onFailure: { failure in
                guard let index = self.outgoingMessages.firstIndex(where: { $0.id == id }),
                      let serviceError = failure as? ServiceError,
                      case .response(let status, let reason) = serviceError,
                      (400..<500).contains(status), ![408, 429].contains(status) else { return }
                self.outgoingMessages[index].status = .rejected
                self.outgoingMessages[index].failureReason = reason
            })
        if let index = outgoingMessages.firstIndex(where: { $0.id == id }) {
            if outgoingMessages[index].status != .rejected {
                outgoingMessages[index].status = result == nil ? .failed : .accepted
            }
            try? saveOutgoing()
        }
    }

    func replaceRejected(_ outgoing: OutgoingMessage, text: String) async {
        guard outgoingMessages.contains(where: { $0.id == outgoing.id && $0.status == .rejected }),
              let bot = bots.first(where: { $0.id == outgoing.botId }) else { return }
        outgoingMessages.removeAll { $0.id == outgoing.id }
        let saved = await send(bot: bot, text: text, attachments: outgoing.request.attachmentIds,
                              scheduledAt: outgoing.request.scheduledAt, mentionedBotIds: outgoing.request.mentionedBotIds ?? outgoing.request.recipientBotIds, replyTo: outgoing.replyTo, workContext: outgoing.workContext, workStatusOnly: outgoing.request.workStatusOnly ?? false)
        if !saved { outgoingMessages.append(outgoing); try? saveOutgoing() }
    }

    private func saveOutgoing() throws {
        guard let pairing else { return }
        try ProtectedCache.write(OutgoingCache(deviceId: pairing.deviceId, messages: outgoingMessages, answers: outgoingAnswers), name: "outgoing.json")
    }

    func decisionReference(for id: String) -> MessageReference? {
        guard let decision = decisions.first(where: { $0.id == id }) else { return nil }
        return MessageReference(messageId: id, text: decision.title,
            author: bots.first { $0.id == decision.botId }?.name ?? "Bot", role: "assistant")
    }

    func messageReference(for message: Message) -> MessageReference? {
        guard message.kind == "text", ["user", "assistant"].contains(message.role),
              !message.streaming, message.reactionEvent == nil, !message.isBotExchange,
              let saved = messages.first(where: { $0.id == message.id || (message.clientRequestId != nil && $0.clientRequestId == message.clientRequestId) }) else { return nil }
        return MessageReference(messageId: saved.id, text: saved.text,
            author: saved.role == "user" ? "You" : bots.first { $0.id == (saved.senderBotId ?? saved.botId) }?.name ?? "Bot",
            role: saved.role)
    }

    func reactions(for message: Message) -> [String] {
        guard let reference = messageReference(for: message) else { return message.reactions ?? [] }
        return reactionOverrides[reference.messageId] ?? messages.first { $0.id == reference.messageId }?.reactions ?? []
    }

    func react(to message: Message, emoji: String) async {
        guard MessageReactions.all.contains(emoji), let reference = messageReference(for: message),
              reactionOverrides[reference.messageId] == nil else { return }
        let id = reference.messageId
        let current = reactions(for: message)
        let active = !current.contains(emoji)
        let updated = active ? current + [emoji] : current.filter { $0 != emoji }
        if isDemo {
            if let index = messages.firstIndex(where: { $0.id == id }) { messages[index].reactions = updated }
            return
        }
        let generation = connectionGeneration
        reactionOverrides[id] = updated
        defer { reactionOverrides[id] = nil }
        let result: Message? = await write("messages/\(id)/reactions", payload: ReactionRequest(emoji: emoji, active: active),
            as: Message.self, quiet: true, refreshInBackground: true, onSuccess: { receipt in
                guard generation == self.connectionGeneration else { return }
                self.snapshotEpoch = UUID()
                self.snapshotRequest = nil
                if let index = self.messages.firstIndex(where: { $0.id == id }) { self.messages[index].reactions = receipt.reactions }
                if let index = self.cachedState?.messages.firstIndex(where: { $0.id == id }) { self.cachedState?.messages[index].reactions = receipt.reactions }
            })
        if result == nil && generation == connectionGeneration { error = "Reaction not confirmed. Try again." }
    }

    func displayedMessageId(for serverId: String) -> String {
        guard let requestId = messages.first(where: { $0.id == serverId })?.clientRequestId else { return serverId }
        return "outgoing-" + requestId
    }

    func conversationMessages(for botId: String) -> [Message] {
        let confirmed = messages.filter { $0.botId == botId || ($0.isBotExchange && $0.exchangeBotIds.contains(botId)) }
        let confirmedRequests = Set(confirmed.compactMap(\.clientRequestId))
        return (confirmed.map { message in
            var displayed = message
            if let requestId = message.clientRequestId { displayed.id = "outgoing-" + requestId }
            return displayed
        } + outgoingMessages.filter { $0.botId == botId && !confirmedRequests.contains($0.id) }.map(\.message)
          + outgoingAnswers.filter { $0.botId == botId && !confirmedRequests.contains($0.id) }.map(\.message))
            .sorted { $0.createdAt < $1.createdAt }
    }

    func conversationTranscript(for botId: String, focusDecisionId: String? = nil) -> [Message] {
        let messages = conversationMessages(for: botId).map { message in
            var row = message
            if row.kind == "decision", let id = row.decisionId { row.id = id }
            return row
        }
        let represented = Set(messages.filter { $0.kind == "decision" }.compactMap(\.decisionId))
        let missing = decisions.filter {
            conversationBotId(for: $0) == botId && ($0.isPending || $0.isRefreshing || $0.id == focusDecisionId)
                && !represented.contains($0.id)
        }.map { decision in
            Message(id: decision.id, botId: botId, taskId: decision.taskId, role: "assistant", kind: "decision",
                    text: "", decisionId: decision.id, artifactId: nil, createdAt: decision.createdAt, streaming: false)
        }
        // Cards can outlive the loaded history page. Keep their original place in the transcript.
        let timeline = (messages + missing).sorted { $0.createdAt < $1.createdAt }
        return TranscriptPresentation.visibleMessages(timeline, artifacts: artifacts, decisions: decisions)
            .filter { $0.kind != "activity" }
    }

    @discardableResult
    func reply(task: AgentTask, text: String, attachments: [String] = [], decisionId: String? = nil, mentionedBotIds: [String] = []) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return false }
        if isDemo {
            guard let current = tasks.first(where: { $0.id == task.id }) else { return false }
            if current.status == "needs_you" {
                guard let decisionId, let decision = decisions.first(where: { $0.id == decisionId && $0.taskId == task.id && $0.isPending }) else {
                    error = "Choose the pending decision before asking for context."
                    return false
                }
                await requestContext(decision, question: text)
                return true
            }
            var target = current
            if ["completed", "failed", "cancelled"].contains(current.status) {
                target = AgentTask(id: UUID().uuidString, botId: current.botId, sessionId: current.sessionId,
                                   title: current.title, status: "queued", summary: "Preview: continuation queued in this thread.",
                                   createdAt: Self.now, updatedAt: Self.now, scheduledAt: nil, threadId: current.threadId,
                                   conversationBotId: current.conversationBotId)
                tasks.append(target)
            }
            let conversationBotId = target.conversationBotId ?? target.botId
            messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: target.id, role: "user", kind: "text", text: text,
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: target.id, role: "system", kind: "text",
                                    text: current.status == "working" ? "Preview: your reply applies to this task at its next safe turn." : "Preview: your reply stays in this task thread.",
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            return true
        }
        let result: AgentTask? = await write("tasks/\(task.id)/replies", payload: ReplyRequest(text: text, attachmentIds: attachments,
                                                                                         decisionId: decisionId, mentionedBotIds: mentionedBotIds), as: AgentTask.self)
        return result != nil
    }

    func resumeWork(_ item: TrackedWorkItem) async {
        if isDemo {
            for index in artifacts.indices where artifacts[index].id == item.artifact.id {
                artifacts[index].workState = "queued"
                artifacts[index].activity = "Queued"
            }
            return
        }
        let _: Acknowledgement? = await write("work/items/\(item.artifact.id)/resume", payload: EmptyRequest(), as: Acknowledgement.self)
    }

    @discardableResult
    func cancelWork(_ item: TrackedWorkItem) async -> String? {
        if isDemo {
            for index in artifacts.indices where artifacts[index].id == item.artifact.id {
                artifacts[index].workState = "cancelled"
                artifacts[index].activity = "Stopped"
            }
            return nil
        }
        let generation = connectionGeneration
        var failure: String?
        let receipt = await write("work/items/\(item.artifact.id)/cancel", payload: EmptyRequest(), as: WorkCancellationReceipt.self,
            refreshInBackground: true, onFailure: { failure = $0.localizedDescription })
        guard let receipt, receipt.ok else { return failure ?? "Cancellation was not confirmed. Try again." }
        guard generation == connectionGeneration else { return nil }
        if let updated = receipt.applying(to: artifacts, after: cursor) {
            // Discard reads that started before the cancellation receipt arrived.
            snapshotEpoch = UUID()
            snapshotRequest = nil
            artifacts = updated
            cachedState?.artifacts = updated
        }
        return nil
    }

    func cancel(_ task: AgentTask) async {
        if isDemo {
            if let index = tasks.firstIndex(where: { $0.id == task.id }) { tasks[index].status = "cancelled"; tasks[index].summary = "You stopped this task." }
            if let index = bots.firstIndex(where: { $0.id == task.botId }) { bots[index].status = "idle" }
            for index in decisions.indices where decisions[index].taskId == task.id && decisions[index].isPending {
                decisions[index].status = "rejected"
                decisions[index].decidedBy = "You (preview)"
                decisions[index].decidedAt = Self.now
                decisions[index].outcome = "You stopped this task."
            }
            return
        }
        let _: AgentTask? = await write("tasks/\(task.id)/cancel", payload: EmptyRequest(), as: AgentTask.self)
    }

    @discardableResult
    func answer(_ decision: Decision, choice: String, correction: String? = nil, optionId: String? = nil) async -> Bool {
        guard ["approve", "reject", "modify", "reply"].contains(choice) else { return false }
        guard !answeringDecisionIds.contains(decision.id) else { return false }
        answeringDecisionIds.insert(decision.id)
        defer {
            if !outgoingAnswers.contains(where: { $0.decisionId == decision.id }) { answeringDecisionIds.remove(decision.id) }
        }
        if isDemo {
            guard let index = decisions.firstIndex(where: { $0.id == decision.id }), decisions[index].isPending else { return false }
            let option = decisions[index].options.first { $0.id == optionId }
            if choice == "approve" && !decision.options.isEmpty && option == nil {
                error = "Choose an option before approval."
                return false
            }
            if let option, option.answer != choice { error = "This option requires a different response."; return false }
            decisions[index].status = choice == "reply" ? "replied" : choice == "modify" ? "modified" : choice == "approve" ? (decision.draft == nil ? "executed" : "approved") : "rejected"
            decisions[index].correction = correction ?? option?.modification
            decisions[index].selectedOptionId = option?.id
            decisions[index].selectedOptionLabel = option?.label
            if let action = option?.action {
                decisions[index].resource = action.filename
                decisions[index].proposalPreview = action.content
            }
            decisions[index].decidedBy = "You (preview)"
            decisions[index].decidedAt = Self.now
            decisions[index].outcome = choice == "reply" ? "Reply received. The bot will continue." : choice == "modify" ? "Preview: the bot would prepare a new proposal. No revised action is approved." : choice == "approve" ? "Preview result saved. No real action ran." : "You rejected the action."
            if let taskIndex = tasks.firstIndex(where: { $0.id == decision.taskId }) {
                tasks[taskIndex].status = ["modify", "reply"].contains(choice) ? "queued" : "completed"
                tasks[taskIndex].summary = decisions[index].outcome ?? ""
            }
            if let botIndex = bots.firstIndex(where: { $0.id == decision.botId }) { bots[botIndex].status = ["modify", "reply"].contains(choice) ? "working" : "idle" }
            if choice == "reply", let correction {
                let task = tasks.first { $0.id == decision.taskId }
                messages.append(Message(id: UUID().uuidString, botId: task?.conversationBotId ?? decision.botId,
                    taskId: decision.taskId, role: "user", kind: "text", text: correction,
                    decisionId: decision.id, artifactId: nil, createdAt: Self.now, streaming: false, threadId: task?.threadId))
            }
            if choice == "approve" && decision.draft == nil {
                let task = tasks.first { $0.id == decision.taskId }
                let conversationBotId = task?.conversationBotId ?? decision.botId
                let artifact = Artifact(id: UUID().uuidString, botId: decision.botId, taskId: decision.taskId, title: option?.action?.filename ?? "retry-fix.patch", kind: "file",
                                        status: "ready", preview: decisions[index].proposalPreview ?? "Preview result.", url: nil, createdAt: Self.now)
                artifacts.append(artifact)
                messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: decision.taskId, role: "assistant", kind: "artifact", text: "",
                                        decisionId: nil, artifactId: artifact.id, createdAt: Self.now, streaming: false,
                                        threadId: task?.threadId, senderBotId: decision.botId))
            }
            return true
        }
        guard hasPairing else { error = "Pair with your Mac before replying."; return false }
        let queued = OutgoingAnswer(id: UUID().uuidString, decisionId: decision.id,
            botId: conversationBotId(for: decision), createdAt: Self.now, sequence: nextDeliverySequence,
            text: correction ?? decision.options.first { $0.id == optionId }?.label ?? choice.capitalized,
            request: AnswerRequest(choice: choice, revision: decision.revision, correction: correction,
                optionId: optionId, draftHash: decision.draftHash, previewsHash: decision.previewsHash),
            replyTo: choice == "reply" ? decisionReference(for: decision.id) : nil)
        outgoingAnswers.append(queued)
        do { try saveOutgoing() }
        catch {
            outgoingAnswers.removeAll { $0.id == queued.id }
            report(error)
            return false
        }
        scheduleDelivery()
        return true
    }

    private var nextDeliverySequence: Int {
        max(outgoingMessages.compactMap(\.sequence).max() ?? 0, outgoingAnswers.map(\.sequence).max() ?? 0) + 1
    }

    private func resetDelivery() {
        deliveryID = UUID()
        for task in deliveryTasks.values { task.cancel() }
        deliveryTasks = [:]
    }

    private func scheduleDelivery() {
        guard !isDemo, hasPairing else { return }
        let identity = deliveryID
        let bots = Set(outgoingMessages.filter { [.sending, .failed].contains($0.status) }.map(\.botId))
            .union(outgoingAnswers.map(\.botId))
        for botId in bots where deliveryTasks[botId] == nil {
            deliveryTasks[botId] = Task { [weak self] in
                guard let self else { return }
                defer { if self.deliveryID == identity { self.deliveryTasks[botId] = nil } }
                while !Task.isCancelled, self.deliveryID == identity {
                    let message = self.outgoingMessages.first { $0.botId == botId && [.sending, .failed].contains($0.status) }
                    let answer = self.outgoingAnswers.first { $0.botId == botId }
                    if let message, answer == nil || (message.sequence ?? 0) < answer!.sequence {
                        await self.deliverOutgoing(message.id)
                        if self.outgoingMessages.contains(where: { $0.id == message.id && $0.status == .failed }) { break }
                    } else if let answer {
                        if !(await self.deliverAnswer(answer)) { break }
                    } else { break }
                }
            }
        }
    }

    private func deliverAnswer(_ answer: OutgoingAnswer) async -> Bool {
        let identity = deliveryID
        var rejected = false
        let recorded: Decision? = await write("decisions/\(answer.decisionId)/answer", payload: answer.request,
            as: Decision.self, quiet: true, refreshInBackground: true, idempotencyKey: answer.id,
            onFailure: { failure in
                if let serviceError = failure as? ServiceError,
                   case .response(let status, let reason) = serviceError,
                   (400..<500).contains(status), ![408, 429].contains(status) {
                    rejected = true
                    if identity == self.deliveryID { self.error = reason }
                }
            }, onSuccess: { receipt in
                guard identity == self.deliveryID else { return }
                self.snapshotEpoch = UUID()
                self.snapshotRequest = nil
                if let index = self.decisions.firstIndex(where: { $0.id == receipt.id }),
                   self.decisions[index].revision == receipt.revision { self.decisions[index] = receipt }
                if let index = self.cachedState?.decisions.firstIndex(where: { $0.id == receipt.id }) {
                    self.cachedState?.decisions[index] = receipt
                }
            })
        guard identity == deliveryID else { return false }
        if recorded != nil || rejected {
            if rejected, answer.request.choice == "reply", let text = answer.request.correction {
                decisionReplyDrafts[answer.decisionId] = text
            }
            outgoingAnswers.removeAll { $0.id == answer.id }
            answeringDecisionIds.remove(answer.decisionId)
            try? saveOutgoing()
            return true
        }
        return false
    }

    func editDraft(_ decision: Decision, draft: MessageDraft) async -> Bool {
        if isDemo, let index = decisions.firstIndex(where: { $0.id == decision.id && $0.isPending }) {
            decisions[index].draft = draft
            decisions[index].revision += 1
            return true
        }
        let generation = connectionGeneration
        let recorded: Decision? = await write("decisions/\(decision.id)/draft",
            payload: DraftEditRequest(revision: decision.revision, draft: draft), as: Decision.self,
            refreshInBackground: true, onSuccess: { receipt in
                guard generation == self.connectionGeneration else { return }
                self.snapshotEpoch = UUID()
                self.snapshotRequest = nil
                if let index = self.decisions.firstIndex(where: { $0.id == receipt.id }),
                   self.decisions[index].revision <= receipt.revision {
                    self.decisions[index] = receipt
                }
                self.cachedState?.decisions = self.decisions
            })
        return recorded != nil
    }

    func updateInbox(_ decision: Decision, action: String, idempotencyKey: String) async -> InboxMutationResult {
        if isDemo, let index = decisions.firstIndex(where: { $0.id == decision.id }) {
            decisions[index].inboxSwipeId = action == "restore" ? nil : UUID().uuidString
            decisions[index].inboxUndoUntil = action == "restore" ? nil : ISO8601DateFormatter().string(from: Date().addingTimeInterval(10))
            if action == "dismiss" { decisions[index].status = "dismissed" }
            else if action == "restore" { decisions[index].status = "pending"; decisions[index].inboxDeferredAt = nil }
            else { decisions[index].inboxDeferredAt = Self.now }
            return InboxMutationResult(decision: decisions[index], isUncertain: false)
        }
        var uncertain = false
        let recorded: Decision? = await write("decisions/\(decision.id)/inbox",
            payload: InboxRequest(action: action, revision: decision.revision, swipeId: decision.inboxSwipeId), as: Decision.self, refreshInBackground: true, idempotencyKey: idempotencyKey, onFailure: { error in
                if let serviceError = error as? ServiceError, case .response = serviceError { uncertain = false }
                else { uncertain = true }
            }, onSuccess: { recorded in
                self.applyInboxReceipt(recorded, replacing: decision)
            })
        return InboxMutationResult(decision: recorded, isUncertain: uncertain)
    }

    func applyInboxReceipt(_ recorded: Decision, replacing original: Decision) {
        guard let index = decisions.firstIndex(where: { $0.id == recorded.id }),
              decisions[index].revision == original.revision, decisions[index].status == original.status,
              decisions[index].inboxSwipeId == original.inboxSwipeId else { return }
        // Ignore snapshots started before this receipt. Keep message-only replay consistent too.
        snapshotEpoch = UUID()
        snapshotRequest = nil
        // Preserve snapshot fields. A later snapshot can replace this confirmed mutation.
        decisions[index].status = recorded.status
        decisions[index].revision = recorded.revision
        decisions[index].inboxDeferredAt = recorded.inboxDeferredAt
        decisions[index].inboxUndoUntil = recorded.inboxUndoUntil
        decisions[index].inboxSwipeId = recorded.inboxSwipeId
        cachedState?.decisions = decisions
    }

    func replyToDecision(_ decision: Decision, text: String) async -> Bool {
        let reply = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !reply.isEmpty else { return false }
        return await answer(decision, choice: "reply", correction: reply)
    }

    func requestContext(_ decision: Decision, question: String) async {
        guard !question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        if isDemo {
            let task = tasks.first { $0.id == decision.taskId }
            let conversationBotId = task?.conversationBotId ?? decision.botId
            messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: decision.taskId, role: "user", kind: "text", text: question,
                                    decisionId: decision.id, artifactId: nil, createdAt: Self.now, streaming: false, threadId: task?.threadId))
            messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: decision.taskId, role: "assistant", kind: "text",
                                    text: "The patch changes two files. All six focused checks pass. Approval creates a local result; it does not publish or merge anything.",
                                    decisionId: decision.id, artifactId: nil, createdAt: Self.now, streaming: false,
                                    threadId: task?.threadId, senderBotId: decision.botId))
            return
        }
        let _: Message? = await write("decisions/\(decision.id)/context", payload: ContextRequest(question: question), as: Message.self)
    }



    func refreshSettings() async {
        guard !isDemo else { return }
        guard let client, connection == .connected else {
            if hasPairing { await reconnect() }
            return
        }
        do {
            try await refresh(using: client)
            refreshNativeSettings(using: client, force: true)
            while let pending = nativeSettingsTask { await pending.value }
        }
        catch is CancellationError { }
        catch let error as URLError where error.code == .cancelled && Task.isCancelled { }
        catch { recordSyncFailure(error) }
    }

    func setModel(_ selection: ModelOverride) async -> Bool {
        guard modelCatalog?.contains(selection) == true else {
            error = "Choose a model and effort from the Mac's catalog."
            return false
        }
        if isDemo {
            let resolved = modelCatalog?.resolved(selection) ?? selection
            model = selection.model
            modelBaseURL = modelCatalog?.entry(for: selection)?.baseUrl ?? ""
            modelCatalog?.defaults.model = selection.model
            modelCatalog?.defaults.provider = selection.provider
            modelCatalog?.defaults.baseUrl = modelBaseURL.isEmpty ? nil : modelBaseURL
            modelCatalog?.defaults.reasoningEffort = resolved.reasoningEffort
            modelConfigured = true
            for index in bots.indices where bots[index].modelOverride == nil { bots[index].effectiveModel = resolved }
            return true
        }
        let response: ModelResponse? = await write("settings/model", method: "PUT", payload: selection, as: ModelResponse.self)
        return response != nil
    }

    func googleStatus() async throws -> GoogleConnectionStatus {
        if isDemo { return GoogleConnectionStatus(connected: false, email: nil, needsReconnect: false) }
        guard let client else { throw ServiceError.configuration("Connect your Mac to continue.") }
        return try await client.request("settings/google")
    }

    func googleRequest<Response: Decodable & Sendable>(_ action: String, request: GoogleConnectionRequest, as type: Response.Type) async throws -> Response {
        guard let client else { throw ServiceError.configuration("Connect your Mac to continue.") }
        let generation = connectionGeneration
        let response: Response = try await client.request("settings/google/\(action)", method: "POST", body: APIJSON.encode(request), retryTransient: true)
        guard generation == connectionGeneration else { throw CancellationError() }
        return response
    }

    func updateMCP(_ request: MCPRequest) async -> MCPResponse? {
        let result: MCPResponse? = await write("settings/mcp", payload: request, as: MCPResponse.self)
        return result
    }

    func setWorkCapacity(_ value: Int?) async {
        if isDemo {
            let automatic = workCapacity?.automaticLimit ?? 4
            workCapacity = ParallelWorkSettings(limit: value, effectiveLimit: value ?? automatic, automaticLimit: automatic)
            return
        }
        let _: ParallelWorkSettings? = await write("settings/work-capacity", method: "PUT",
            payload: WorkerLimitRequest(limit: value, bot: false), as: ParallelWorkSettings.self)
    }

    func setAPIFallback(_ enabled: Bool) async {
        if isDemo {
            apiFallback?.enabled = enabled
            return
        }
        let _: APIFallbackSettings? = await write("settings/api-fallback", method: "PUT",
            payload: APIFallbackRequest(enabled: enabled), as: APIFallbackSettings.self)
    }

    func setWorkerLimit(_ value: Int?, botId: String? = nil) async {
        if isDemo {
            if let botId, let index = bots.firstIndex(where: { $0.id == botId }) {
                bots[index].workerLimit = value
            } else if let settings = parallelWork {
                parallelWork = ParallelWorkSettings(limit: value, effectiveLimit: value ?? settings.automaticLimit, automaticLimit: settings.automaticLimit)
            }
            return
        }
        if let botId {
            let _: Bot? = await write("bots/\(botId)", method: "PATCH", payload: WorkerLimitRequest(limit: value, bot: true), as: Bot.self)
        } else {
            let _: ParallelWorkSettings? = await write("settings/parallel-work", method: "PUT", payload: WorkerLimitRequest(limit: value, bot: false), as: ParallelWorkSettings.self)
        }
    }

    func setApproval(_ value: String?, botId: String? = nil) async {
        if isDemo {
            if let botId, let index = bots.firstIndex(where: { $0.id == botId }) {
                bots[index].approvalOverride = value
            } else if let value {
                if let settings = approvals { approvals = ApprovalSettings(defaults: value, modes: settings.modes) }
            }
            return
        }
        if let botId {
            let _: Bot? = await write("bots/\(botId)", method: "PATCH", payload: BotApprovalRequest(value: value), as: Bot.self)
        } else if let value {
            let _: ApprovalSettings? = await write("settings/approvals", method: "PUT", payload: ApprovalRequest(mode: value), as: ApprovalSettings.self)
        }
    }

    func updateBotModel(botId: String, selection: ModelOverride?) async -> Bool {
        if let selection, modelCatalog?.contains(selection) != true {
            error = "Choose a model and effort from the Mac's catalog."
            return false
        }
        if isDemo {
            guard let index = bots.firstIndex(where: { $0.id == botId }) else { return false }
            bots[index].modelOverride = selection
            bots[index].effectiveModel = selection.map { modelCatalog?.resolved($0) ?? $0 } ?? modelCatalog?.defaults.selection
            return true
        }
        let response: Bot? = await write("bots/\(botId)", method: "PATCH",
                                          payload: BotModelRequest(modelOverride: selection), as: Bot.self)
        return response != nil
    }

    func uploadAttachment(bot: Bot, url: URL) async -> Artifact? {
        guard !isDemo else { error = "Attachments require a paired Mac. Preview data stays on this screen."; return nil }
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        do {
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard values.isRegularFile == true, let size = values.fileSize, size > 0, size <= 4 * 1024 * 1024 else {
                throw ServiceError.configuration("Choose a file between 1 byte and 4 MB.")
            }
            let data = try Data(contentsOf: url, options: .mappedIfSafe)
            return await write("attachments", payload: AttachmentRequest(botId: bot.id, filename: url.lastPathComponent,
                                                                         contentBase64: data.base64EncodedString()), as: Artifact.self)
        } catch { report(error); return nil }
    }

    func dismissWork(_ pr: TrackedPullRequest) {
        dismissWork(TrackedWorkItem(artifact: pr.artifact, pullRequests: [pr]))
    }

    func dismissWork(_ item: TrackedWorkItem) {
        guard item.isFinished else { return }
        let dismissal = WorkDismissal(id: UUID().uuidString, artifactId: item.artifact.id, url: item.id)
        workUndo.append(dismissal)
        changeWorkVisibility(dismissal, undo: false)
    }

    func undoWorkDismissal() {
        guard let dismissal = workUndo.popLast() else { return }
        changeWorkVisibility(dismissal, undo: true)
    }

    private func changeWorkVisibility(_ dismissal: WorkDismissal, undo: Bool) {
        workVisibility[dismissal.url] = !undo
        let operation = UUID()
        workMutationIds[dismissal.url] = operation
        let deviceId = pairing?.deviceId
        let previous = workMutationTail
        workMutationTail = Task {
            await previous?.value
            guard deviceId == pairing?.deviceId else { return }
            let succeeded: Bool
            if isDemo {
                applyWorkReceipt(dismissal, undo: undo)
                succeeded = true
            } else {
                let collection = dismissal.url.hasPrefix("work:") ? "items" : "pull-requests"
                let result = await write("work/\(collection)/\(dismissal.artifactId)/dismiss",
                    payload: WorkDismissRequest(dismissalId: dismissal.id, undo: undo), as: WorkRefreshResponse.self,
                    onSuccess: { receipt in
                        guard receipt.ok, deviceId == self.pairing?.deviceId else { return }
                        self.applyWorkReceipt(dismissal, undo: undo)
                    })
                succeeded = result?.ok == true
            }
            guard deviceId == pairing?.deviceId, workMutationIds[dismissal.url] == operation else { return }
            if !succeeded && undo {
                workUndo.append(dismissal)
            } else if !succeeded {
                workUndo.removeAll { $0.id == dismissal.id }
            }
            workVisibility.removeValue(forKey: dismissal.url)
            workMutationIds.removeValue(forKey: dismissal.url)
        }
    }

    private func applyWorkReceipt(_ dismissal: WorkDismissal, undo: Bool) {
        // Reject older snapshots and keep message replay consistent with the confirmed write.
        snapshotEpoch = UUID()
        snapshotRequest = nil
        for index in artifacts.indices where artifacts[index].workIdentity == dismissal.url {
            artifacts[index].workDismissalId = undo ? nil : dismissal.id
        }
        cachedState?.artifacts = artifacts
    }

    func refreshWork() async {
        guard !isDemo, foreground, let client else { return }
        let generation = connectionGeneration
        do {
            let _: WorkRefreshResponse = try await client.request("work/refresh", method: "POST",
                body: Data("{}".utf8), idempotencyKey: UUID().uuidString)
            guard generation == connectionGeneration else { return }
            try await refresh(using: client)
        } catch {
            // Keep observed PR data. Its timestamp marks stale status in Work.
        }
    }

    func decisionPreviewData(id: String) async throws -> Data {
        guard let client else { throw ServiceError.configuration("Connect to your Mac to load the previews.") }
        return try await client.artifactContent(id: id)
    }

    func downloadArtifact(_ artifact: Artifact) async -> URL? {
        do {
            let data: Data
            if isDemo {
                data = Data(("Preview file. No agent execution.\n\n" + artifact.preview).utf8)
            } else {
                guard let client else { error = "Connect to your Mac to open this file."; return nil }
                data = try await client.artifactContent(id: artifact.id)
            }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("CoconutArtifacts", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
            let filename = URL(fileURLWithPath: artifact.title).lastPathComponent
            let file = directory.appendingPathComponent(artifact.id + "-" + (filename.isEmpty ? "result" : filename))
            try data.write(to: file, options: [.atomic, .completeFileProtection])
            return file
        } catch { report(error); return nil }
    }

    func loadLearning() async {
        if isDemo {
            learning = [LearningGroup(botId: bots.first?.id ?? "", actionType: "create_file", total: 8, agreed: 7, agreement: 0.875,
                                      suggestion: "Review more examples before changing permissions.")]
            return
        }
        guard let client else { return }
        do { let response: LearningResponse = try await client.request("learning"); learning = response.groups }
        catch { report(error) }
    }

    func restartBots() async -> Bool {
        if isDemo { return true }
        let result: Acknowledgement? = await write("bots/restart", payload: EmptyRequest(), as: Acknowledgement.self)
        return result?.ok == true
    }

    private func write<Response: Decodable & Sendable, Payload: Encodable>(_ path: String, method: String = "POST", payload: Payload, as type: Response.Type, quiet: Bool = false, refreshInBackground: Bool = false, idempotencyKey: String? = nil, onFailure: ((Error) -> Void)? = nil, onSuccess: ((Response) -> Void)? = nil) async -> Response? {
        guard let pairing, let token = PairingToken.read() else {
            if !quiet { error = "Pair with your Mac in Settings to continue." }
            return nil
        }
        func belongsToPairing() -> Bool {
            self.pairing?.deviceId == pairing.deviceId && self.pairing?.serverURL == pairing.serverURL
                && self.pairing?.fingerprint == pairing.fingerprint
        }
        writeCount += 1
        defer { writeCount -= 1 }
        var signature: String?
        let generation = connectionGeneration
        var requestClient: APIClient?
        do {
            if client == nil { client = try APIClient(serverURL: pairing.serverURL, fingerprint: pairing.fingerprint, token: token) }
            guard let client else { return nil }
            requestClient = client
            let body = try APIJSON.encode(payload)
            let requestSignature = method + " " + path + " " + SHA256.hash(data: body).description
            signature = requestSignature
            let key = idempotencyKey ?? uncertainWriteKeys[requestSignature] ?? UUID().uuidString
            if idempotencyKey == nil { uncertainWriteKeys[requestSignature] = key }
            let receiptModel = model
            let receiptBaseURL = modelBaseURL
            let receiptCatalog = modelCatalog
            let receiptApproval = approvals?.defaults
            let receiptMCP = mcpServers
            let receiptCapacityLimit = workCapacity?.limit
            let receiptParallelLimit = parallelWork?.limit
            let receiptFallback = apiFallback
            let receiptMessages = messages
            let receiptDecisions = decisions
            let receiptArtifacts = artifacts
            let receiptBots = bots
            let receiptRoutines = routines
            let receiptNativeReadAt = nativeSettingsReadAt
            let receiptCursor = cachedState?.cursor
            let receiptEpoch = snapshotEpoch
            let result: Response = try await client.request(path, method: method, body: body, idempotencyKey: key)
            guard belongsToPairing() else { return nil }
            uncertainWriteKeys.removeValue(forKey: requestSignature)
            if !quiet { error = nil }
            let updatesNativeSettings = path.hasPrefix("settings/") || path.contains("routines") || (path.hasPrefix("bots/") && method == "PATCH")
            let receiptIsCurrent = cachedState?.cursor == receiptCursor && snapshotEpoch == receiptEpoch
            if updatesNativeSettings {
                snapshotEpoch = UUID()
                nativeSettingsTask?.cancel()
            }
            if let bot = result as? Bot {
                if let previous = receiptBots.first(where: { $0.id == bot.id }), let current = bots.first(where: { $0.id == bot.id }) {
                    bots = StateUpdates.merge([StateUpdates.mergeBotReceipt(bot, original: previous, current: current)], into: bots)
                } else if receiptIsCurrent { bots = StateUpdates.merge([bot], into: bots) }
                cachedState?.bots = bots
            } else if let routine = result as? Routine {
                if bots.contains(where: { $0.id == routine.botId }) {
                    let previous = receiptRoutines.first { $0.id == routine.id }
                    let current = routines.first { $0.id == routine.id }
                    if let previous, let current {
                        routines = StateUpdates.merge([StateUpdates.mergeRoutineReceipt(routine, original: previous, current: current)], into: routines)
                    } else if previous == nil, current == nil, receiptNativeReadAt == nativeSettingsReadAt {
                        routines.append(routine)
                    }
                    cachedState?.routines = routines
                }
            }
            if let settings = result as? ModelResponse, model == receiptModel, modelBaseURL == receiptBaseURL, modelCatalog == receiptCatalog {
                model = settings.model; modelConfigured = settings.modelConfigured
                modelBaseURL = settings.baseUrl ?? ""; modelCatalog = settings.modelCatalog
                for index in bots.indices where bots[index].modelOverride == nil {
                    bots[index].effectiveModel = settings.modelCatalog?.defaults.selection
                }
            } else if let settings = result as? ApprovalSettings, approvals?.defaults == receiptApproval { approvals = settings }
            else if let settings = result as? APIFallbackSettings, apiFallback == receiptFallback { apiFallback = settings }
            else if let settings = result as? MCPResponse, mcpServers == receiptMCP { mcpServers = settings.servers }
            else if let settings = result as? ParallelWorkSettings {
                if path == "settings/work-capacity", workCapacity?.limit == receiptCapacityLimit {
                    workCapacity = settings; cachedState?.workCapacity = settings
                } else if path == "settings/parallel-work", parallelWork?.limit == receiptParallelLimit { parallelWork = settings }
            }
            if method == "DELETE", path.hasPrefix("routines/"),
               StateUpdates.sameRoutineSettings(receiptRoutines.first(where: { "routines/" + $0.id == path }), routines.first(where: { "routines/" + $0.id == path })) {
                routines.removeAll { "routines/" + $0.id == path }
                cachedState?.routines = routines
            }
            var callbackIsCurrent = receiptIsCurrent
            if let receipt = result as? Message {
                callbackIsCurrent = receiptMessages.first(where: { $0.id == receipt.id })?.reactions == messages.first(where: { $0.id == receipt.id })?.reactions
            } else if let receipt = result as? Decision {
                callbackIsCurrent = receiptDecisions.first(where: { $0.id == receipt.id }) == decisions.first(where: { $0.id == receipt.id })
            } else if result is WorkRefreshResponse, path.hasSuffix("/dismiss") {
                let identity = path.split(separator: "/").dropFirst(2).first.map(String.init)
                callbackIsCurrent = receiptArtifacts.first(where: { $0.id == identity })?.workDismissalId == artifacts.first(where: { $0.id == identity })?.workDismissalId
            }
            if callbackIsCurrent { onSuccess?(result) }
            if refreshInBackground {
                Task { await refreshAfterWrite(using: client) }
            } else {
                await refreshAfterWrite(using: client)
                guard belongsToPairing() else { return nil }
                guard self.client === client, connectionGeneration == generation else { return result }
                if cachedState?.syncVersion == 1 && updatesNativeSettings {
                    refreshNativeSettings(using: client, force: true)
                }
            }
            return result
        } catch {
            guard belongsToPairing() else { return nil }
            let transportIsCurrent = connectionGeneration == generation && (requestClient == nil || self.client === requestClient)
            onFailure?(error)
            if let signature, let serviceError = error as? ServiceError,
               case .response(let status, _) = serviceError, (400..<500).contains(status) {
                uncertainWriteKeys.removeValue(forKey: signature)
            }
            if quiet {
                // An HTTP error confirms reachability even when the action fails.
                if let serviceError = error as? ServiceError, case .response = serviceError {} else if transportIsCurrent { recordSyncFailure(error) }
            }
            else {
                report(error, updateConnection: transportIsCurrent)
                if error is URLError && transportIsCurrent { recordSyncFailure(error) }
            }
            return nil
        }
    }

    private func refreshAfterWrite(using client: APIClient) async {
        let generation = connectionGeneration
        guard self.client === client else { return }
        do { try await refresh(using: client) }
        catch {
            guard self.client === client, connectionGeneration == generation else { return }
            recordSyncFailure(error)
            beginPolling()
        }
    }

    private func beginPolling(refreshImmediately: Bool = true) {
        guard foreground, !isDemo, pairing != nil, pollTask == nil else { return }
        let generation = connectionGeneration
        pollTask = Task { [weak self] in
            guard let self else { return }
            var failures = 0
            var needsSnapshot = refreshImmediately
            while !Task.isCancelled, self.foreground, self.connectionGeneration == generation {
                do {
                    if self.client == nil {
                        guard let pairing = self.pairing, let token = PairingToken.read() else { self.connection = .unconfigured; break }
                        self.client = try APIClient(serverURL: pairing.serverURL, fingerprint: pairing.fingerprint, token: token)
                    }
                    guard let client = self.client else { break }
                    if needsSnapshot || self.connection != .connected {
                        try await self.refresh(using: client)
                        needsSnapshot = false
                    }
                    if self.cachedState?.syncVersion == 1 {
                        let epoch = self.snapshotEpoch
                        let page: StateUpdates = try await client.request("updates?after=\(self.cursor)&wait=20")
                        try Task.checkCancellation()
                        guard self.connectionGeneration == generation else { break }
                        if epoch == self.snapshotEpoch, let state = self.cachedState, let next = page.applying(to: state) {
                            if page.reset != nil || page.removedMessages?.isEmpty == false { self.snapshotEpoch = UUID() }
                            self.apply(next, persist: page.reset != nil || page.cursor > page.after)
                            self.connection = .connected
                            self.syncIssue = nil
                            failures = 0
                            self.refreshNativeSettings(using: client, force: page.settingsChanged == true || page.reset != nil)
                            Task { await self.syncPushRegistration() }
                        }
                        self.scheduleDelivery()
                        continue
                    }
                    let page: EventPage = try await client.request("events?after=\(self.cursor)&wait=20")
                    try Task.checkCancellation()
                    guard self.connectionGeneration == generation else { break }
                    if page.cursor > self.cursor {
                        if let messages = MessageReplay.applying(page, after: self.cursor, to: self.messages), var snapshot = self.cachedState {
                            snapshot.messages = messages
                            snapshot.cursor = page.cursor
                            snapshot.serverTime = page.serverTime
                            self.apply(snapshot, persist: true)
                        } else { try await self.refresh(using: client) }
                    }
                    self.lastSync = Date()
                    self.connection = .connected
                    self.syncIssue = nil
                    self.scheduleDelivery()
                    Task { await self.syncPushRegistration() }
                    failures = 0
                } catch is CancellationError { break }
                catch let error as URLError where error.code == .cancelled && Task.isCancelled { break }
                catch {
                    guard self.connectionGeneration == generation else { break }
                    self.recordSyncFailure(error)
                    if error is URLError { self.client = nil }
                    needsSnapshot = true
                    failures += 1
                    do { try await Task.sleep(for: .milliseconds(min(10_000, 500 * (1 << min(failures - 1, 5))))) }
                    catch { break }
                }
            }
            if self.connectionGeneration == generation { self.pollTask = nil }
        }
    }

    func refresh(using client: APIClient) async throws {
        guard self.client === client else { throw CancellationError() }
        let generation = connectionGeneration
        if cachedState?.syncVersion == 1 {
            let epoch = snapshotEpoch
            do {
                let page: StateUpdates = try await client.request("updates?after=\(cursor)")
                guard self.client === client, generation == connectionGeneration, epoch == snapshotEpoch else { return }
                if page.after != cursor { return }
                if let cachedState, let next = page.applying(to: cachedState) {
                    if page.reset != nil || page.removedMessages?.isEmpty == false { snapshotEpoch = UUID() }
                    apply(next, persist: true)
                    connection = .connected
                    syncIssue = nil
                    refreshNativeSettings(using: client, force: page.settingsChanged == true || page.reset != nil)
                    Task { await self.syncPushRegistration() }
                    return
                }
            } catch let error as ServiceError {
                switch error {
                case .response(404, _), .invalidResponse: break
                default: throw error
                }
            } catch is DecodingError {
                // A previous server can return a different synchronization format.
            }
            guard self.client === client, generation == connectionGeneration, epoch == snapshotEpoch else { return }
        }
        if snapshotRequest == nil || snapshotRequest?.generation != generation || snapshotRequest?.client !== client {
            snapshotRequest = (UUID(), generation, snapshotEpoch, client, Task { try await client.request("state") })
        }
        guard let request = snapshotRequest else { return }
        defer { if snapshotRequest?.id == request.id { snapshotRequest = nil } }
        let snapshot: StateSnapshot
        do { snapshot = try await request.task.value }
        catch {
            if request.epoch != snapshotEpoch, !Task.isCancelled { return }
            throw error
        }
        try Task.checkCancellation()
        guard self.client === client, connectionGeneration == generation else { throw CancellationError() }
        guard request.epoch == snapshotEpoch else { return }
        if snapshot.cursor >= cursor { apply(snapshot, persist: true) }
        connection = .connected
        syncIssue = nil
        Task { await syncPushRegistration() }
        refreshNativeSettings(using: client)
    }

    nonisolated static func newReplies(in incoming: StateSnapshot, after previous: StateSnapshot?) -> [Message] {
        guard let previous, incoming.cursor > previous.cursor else { return [] }
        let finished = Set(previous.messages.filter { !$0.streaming }.map(\.id))
        return incoming.messages.filter {
            $0.role == "assistant" && $0.kind == "text" && !$0.streaming && !finished.contains($0.id)
        }
    }

    private func apply(_ incoming: StateSnapshot, persist: Bool) {
        for reply in Self.newReplies(in: incoming, after: cachedState) {
            Self.deliveryLog.info("reply_applied task=\(reply.taskId ?? "", privacy: .public) message=\(reply.id, privacy: .public) created=\(reply.createdAt, privacy: .public)")
        }
        var snapshot = incoming
        if snapshot.contentOnly == true && cachedState != nil {
            snapshot.model = model
            snapshot.modelConfigured = modelConfigured
            snapshot.baseUrl = modelBaseURL
            snapshot.modelCatalog = modelCatalog
            snapshot.approvals = approvals
            snapshot.parallelWork = parallelWork
            snapshot.apiFallback = apiFallback
            snapshot.mcpServers = mcpServers
            snapshot.routines = routines
            snapshot.bots = preservingNativeSettings(in: snapshot.bots)
        }
        let confirmed = Set(snapshot.messages.compactMap(\.clientRequestId) + (snapshot.confirmedRequestIds ?? []))
        if outgoingMessages.contains(where: { confirmed.contains($0.id) }) {
            outgoingMessages.removeAll { confirmed.contains($0.id) }
            try? saveOutgoing()
        }
        if bots != snapshot.bots { bots = snapshot.bots }
        if tasks != snapshot.tasks { tasks = snapshot.tasks }
        if messages != snapshot.messages { messages = snapshot.messages }
        if decisions != snapshot.decisions { decisions = snapshot.decisions }
        if artifacts != snapshot.artifacts { artifacts = snapshot.artifacts }
        approvals = snapshot.approvals
        workCapacity = snapshot.workCapacity
        parallelWork = snapshot.parallelWork
        apiFallback = snapshot.apiFallback
        if isDemo && apiFallback == nil { apiFallback = APIFallbackSettings(enabled: false, available: true, model: "gpt-6-astra", canEnable: true) }
        if isDemo && workCapacity == nil { workCapacity = ParallelWorkSettings(limit: nil, effectiveLimit: 4, automaticLimit: 4) }
        if isDemo && parallelWork == nil { parallelWork = ParallelWorkSettings(limit: nil, effectiveLimit: 4, automaticLimit: 4) }
        mcpServers = snapshot.mcpServers ?? []
        if routines != snapshot.routines ?? [] { routines = snapshot.routines ?? [] }
        if foreground { scheduleDelivery() }
        cursor = snapshot.cursor
        provider = snapshot.provider
        model = snapshot.model
        modelBaseURL = snapshot.baseUrl ?? ""
        modelConfigured = snapshot.modelConfigured
        modelCatalog = snapshot.modelCatalog
        pushConfigured = snapshot.pushConfigured ?? false
        lastSync = Date()
        cachedState = snapshot
        if persist { saveCache(snapshot) }
    }

    private func saveCache(_ snapshot: StateSnapshot, immediately: Bool = false) {
        cacheTask?.cancel()
        let cache = SnapshotCache(snapshot: snapshot, syncedAt: lastSync ?? Date())
        let queue = cacheQueue
        cacheTask = Task {
            if !immediately { try? await Task.sleep(for: .milliseconds(300)) }
            guard !Task.isCancelled else { return }
            queue.async { try? ProtectedCache.write(cache, name: "snapshot.json") }
        }
    }

    private func recordSyncFailure(_ failure: Error) {
        if failure is CancellationError || ((failure as? URLError)?.code == .cancelled && Task.isCancelled) { return }
        connection = .disconnected
        syncIssue = (failure as? URLError).map { ConnectionFailure.message(for: $0, serverURL: serverURL) }
            ?? failure.localizedDescription
    }

    func syncPushRegistration() async {
        guard !isDemo, let client, let pairing, connection == .connected else { return }
        if registeringPush { pushSyncRequested = true; return }
        registeringPush = true
        defer {
            registeringPush = false
            if pushSyncRequested {
                pushSyncRequested = false
                Task { await syncPushRegistration() }
            }
        }
        let push = PushNotifications.shared
        await push.refresh(requestPermission: pushConfigured)
        guard self.client === client else { return }
        let preferences = push.preferences
        let signature = [pairing.deviceId, push.environment, push.deviceToken ?? "", String(push.allowed), String(preferences.decisions), String(preferences.messages)].joined(separator: ":")
        guard registeredPushSignature != signature else { return }
        do {
            let response: PushRegistrationResponse
            if push.allowed {
                guard let token = push.deviceToken else { return }
                let configuration: PushRelayConfiguration = try await client.request("push/config")
                let relay = try await PushRelayClient.shared.register(deviceId: pairing.deviceId, token: token,
                    environment: push.environment, configuration: configuration)
                guard self.client === client, push.allowed else {
                    PushRelayClient.shared.disable(deviceId: pairing.deviceId)
                    return
                }
                response = try await client.request("device/push", method: "PUT",
                    body: APIJSON.encode(PushRegistration(token: token, environment: push.environment, enabled: true, relay: relay, preferences: preferences)),
                    idempotencyKey: UUID().uuidString)
            } else {
                PushRelayClient.shared.disable(deviceId: pairing.deviceId)
                response = try await client.request("device/push", method: "DELETE", body: Data("{}".utf8), idempotencyKey: UUID().uuidString)
            }
            guard self.client === client else { return }
            registeredPushSignature = signature
            pushConfigured = response.configured
            pushRegistrationError = nil
        } catch {
            pushRegistrationError = "Notification setup has not reached your Mac. Coconut will retry when connected."
        }
    }

    private func preservingNativeSettings(in incoming: [Bot]) -> [Bot] {
        var values = incoming
        let previous = Dictionary(uniqueKeysWithValues: bots.map { ($0.id, $0) })
        for index in values.indices {
            if let native = previous[values[index].id] {
                values[index].name = native.name
                values[index].role = native.role
                values[index].instructions = native.instructions
                values[index].modelOverride = native.modelOverride
                values[index].effectiveModel = native.effectiveModel
                values[index].approvalOverride = native.approvalOverride
                values[index].effectiveApproval = native.effectiveApproval
                values[index].workerLimit = native.workerLimit
                values[index].modelError = native.modelError
            }
        }
        return values
    }

    private func refreshNativeSettings(using client: APIClient, force: Bool = false) {
        guard cachedState?.syncVersion == 1 else { return }
        if force {
            nativeSettingsRequiredCursor = cursor
            if nativeSettingsTask != nil { nativeSettingsPending = true }
        }
        guard nativeSettingsTask == nil,
              force || (nativeSettingsReadAt.map({ Date().timeIntervalSince($0) > 60 }) ?? true)
                && (nativeSettingsAttemptAt.map({ Date().timeIntervalSince($0) > 5 }) ?? true) else { return }
        nativeSettingsAttemptAt = Date()
        let generation = connectionGeneration
        nativeSettingsTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.connectionGeneration == generation {
                    self.nativeSettingsTask = nil
                    if self.nativeSettingsPending {
                        self.nativeSettingsPending = false
                        if let current = self.client { self.refreshNativeSettings(using: current, force: true) }
                    }
                }
            }
            do {
                let native: NativeSnapshot = try await client.request("state/settings")
                guard !Task.isCancelled, self.client === client, generation == self.connectionGeneration else { return }
                guard native.cursor >= self.nativeSettingsRequiredCursor else { self.nativeSettingsPending = true; return }
                let values = Dictionary(uniqueKeysWithValues: native.bots.map { ($0.id, $0) })
                for index in self.bots.indices {
                    guard let bot = values[self.bots[index].id] else { continue }
                    self.bots[index].name = bot.name
                    self.bots[index].role = bot.role
                    self.bots[index].instructions = bot.instructions
                    self.bots[index].modelOverride = bot.modelOverride
                    self.bots[index].effectiveModel = bot.effectiveModel
                    self.bots[index].approvalOverride = bot.approvalOverride
                    self.bots[index].effectiveApproval = bot.effectiveApproval
                    self.bots[index].workerLimit = bot.workerLimit
                    self.bots[index].modelError = bot.modelError
                }
                self.modelCatalog = native.modelCatalog
                self.model = native.modelCatalog?.defaults.model ?? ""
                self.modelBaseURL = native.modelCatalog?.defaults.baseUrl ?? ""
                self.modelConfigured = native.modelConfigured
                self.approvals = native.approvals
                self.parallelWork = native.parallelWork
                self.apiFallback = native.apiFallback
                self.mcpServers = native.mcpServers ?? []
                self.routines = native.routines ?? []
                if let configured = native.pushConfigured { self.pushConfigured = configured }
                self.nativeSettingsReadAt = Date()
                if var state = self.cachedState {
                    state.bots = self.bots
                    state.pushConfigured = self.pushConfigured
                    self.apply(state, persist: true)
                    Task { await self.syncPushRegistration() }
                }
            } catch { /* Content remains usable while native settings are unavailable. */ }
        }
    }

    func searchConversations(_ text: String) async {
        remoteSearchQuery = text
        remoteSearchBotIds = []
        guard cachedState?.syncVersion == 1, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let client else { return }
        do {
            try await Task.sleep(for: .milliseconds(200))
            var query = URLComponents()
            query.queryItems = [URLQueryItem(name: "q", value: text)]
            struct SearchResult: Decodable, Sendable { let botIds: [String] }
            let result: SearchResult = try await client.request("search?" + (query.percentEncodedQuery ?? "").replacingOccurrences(of: "+", with: "%2B"))
            guard !Task.isCancelled, self.client === client, remoteSearchQuery == text else { return }
            remoteSearchBotIds = Set(result.botIds)
        } catch { /* Keep local search results when the Mac is unavailable. */ }
    }

    func hasOlderMessages(_ botId: String) -> Bool { cachedState?.historyBefore?[botId] != nil }

    @discardableResult
    func loadOlderMessages(_ botId: String) async -> Bool {
        guard let client, let before = cachedState?.historyBefore?[botId], !historyLoading.contains(botId) else { return false }
        let generation = connectionGeneration
        let epoch = snapshotEpoch
        historyLoading.insert(botId)
        defer { historyLoading.remove(botId) }
        do {
            let page: HistoryPage = try await client.request("history/\(botId)?before=\(before)")
            guard self.client === client, generation == connectionGeneration, epoch == snapshotEpoch,
                  var state = cachedState, state.historyBefore?[botId] == before else { return false }
            // Current messages win if replay updates a row while its history request runs.
            state.messages = StateUpdates.merge(state.messages, into: page.messages)
            state.tasks = StateUpdates.merge(state.tasks, into: page.tasks ?? [])
            state.decisions = StateUpdates.merge(state.decisions, into: page.decisions ?? [])
            state.historyBefore?[botId] = page.before
            apply(state, persist: true)
            return page.before != before
        } catch { report(error); return false }
    }

    func notificationDestination(_ target: NotificationTarget) async -> NotificationDestination? {
        guard acceptsNotification(target) else { return nil }
        if target.decisionId == nil { return .conversation(target.conversationRoute) }
        guard let client else { return nil }
        let epoch = snapshotEpoch
        do {
            if cachedState?.syncVersion == 1, let identity = target.decisionId {
                let card: CardSnapshot = try await client.request("cards/\(identity)")
                guard self.client === client, acceptsNotification(target), epoch == snapshotEpoch else { return nil }
                if card.cursor >= cursor {
                    // Discard responses that started before this card read. Replay still uses the unchanged cursor.
                    snapshotEpoch = UUID()
                    if let replies = card.messages { messages = StateUpdates.merge(replies, into: messages).sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) } }
                    cachedState?.messages = messages
                    decisions = StateUpdates.merge(card.decisions ?? [card.decision], into: decisions)
                    tasks = StateUpdates.merge(card.tasks ?? [card.task], into: tasks)
                    bots = StateUpdates.merge(preservingNativeSettings(in: card.bot.id == card.owner.id ? [card.bot] : [card.bot, card.owner]), into: bots)
                    cachedState?.decisions = decisions
                    cachedState?.tasks = tasks
                    cachedState?.bots = bots
                }
                return target.destination(pendingDecisionIds: Set(decisions.filter(\.isPending).map(\.id)), snapshotReady: true)
            }
            // Fetch after the tap. A cached card can already be answered on another phone.
            let snapshot: StateSnapshot = try await client.request("state")
            guard self.client === client, acceptsNotification(target), epoch == snapshotEpoch else { return nil }
            if snapshot.cursor >= cursor { apply(snapshot, persist: true) }
            return target.destination(pendingDecisionIds: Set(decisions.filter(\.isPending).map(\.id)), snapshotReady: true)
        } catch { return nil }
    }

    func acceptsNotification(_ target: NotificationTarget) -> Bool {
        target.deviceId == pairing?.deviceId
    }

    private func report(_ error: Error, updateConnection: Bool = true) {
        if let error = error as? URLError {
            self.error = "Couldn't complete this request. Try again."
            if updateConnection { syncIssue = ConnectionFailure.message(for: error, serverURL: serverURL) }
            #if DEBUG
            NSLog("Coconut connection: %@", ConnectionFailure.diagnostic(for: error))
            #endif
        } else { self.error = error.localizedDescription }
    }

    #if DEBUG && targetEnvironment(simulator)
    private func configureFromLaunchEnvironment() {
        let environment = ProcessInfo.processInfo.environment
        guard let address = environment["COCONUT_SERVER_URL"] else { return }
        let pin = environment["COCONUT_CERT_SHA256"] ?? ""
        serverURL = address
        fingerprint = pin
        if let code = environment["COCONUT_PAIR_CODE"], !code.isEmpty {
            pairCode = code
            bootstrapPairing = true
        } else if let token = environment["COCONUT_DEVICE_TOKEN"], !token.isEmpty {
            do {
                _ = try APIClient(serverURL: address, fingerprint: pin, token: token)
                try PairingToken.save(token)
                let configuration = PairingConfiguration(serverURL: address, fingerprint: pin, deviceId: "simulator-bootstrap")
                try ProtectedCache.write(configuration, name: "pairing.json")
                workVisibility = [:]; workUndo = []; workMutationIds = [:]
            pairing = configuration
                connection = .disconnected
                cursor = 0
            } catch { report(error) }
        }
    }
    #endif

    private static var now: String { ISO8601DateFormatter().string(from: Date()) }
}

private struct PairingConfiguration: Codable { let serverURL: String; let fingerprint: String; let deviceId: String }
struct SnapshotCache: Codable, Sendable {
    let snapshot: StateSnapshot
    let syncedAt: Date
    var updateProtocol: Int? = 4

    var snapshotForRestore: StateSnapshot {
        var restored = snapshot
        // Older caches lack native IDs. Refresh once before compact updates.
        if updateProtocol != 4 { restored.syncVersion = nil }
        return restored
    }
}
private struct PairRequest: Encodable { let code: String; let deviceName: String }
private struct PairResponse: Decodable, Sendable { let token: String; let deviceId: String }
private struct EmptyRequest: Encodable {}
private struct Acknowledgement: Decodable, Sendable { let ok: Bool }
private struct BotRequest: Encodable { let name: String; let role: String; let avatar: String; let color: String; let instructions: String; let memory: String; let pinned: Bool? }
private struct PinRequest: Encodable { let pinned: Bool }
private struct InstructionsRequest: Encodable { let instructions: String }
private struct PreferenceRequest: Encodable { let text: String }
private struct RoutineRequest: Encodable { let name: String; let prompt: String; let cron: String; let timezone: String; let enabled: Bool }
private struct ArchiveRequest: Encodable { let archived: Bool }
struct MessageRequest: Codable { let text: String; let mode: String; let taskId: String?; let scheduledAt: String?; let attachmentIds: [String]; let recipientBotIds: [String]; var replyToMessageId: String? = nil; var mentionedBotIds: [String]? = nil; var workArtifactId: String? = nil; var workStatusOnly: Bool? = nil }
private struct ReactionRequest: Encodable { let emoji: String; let active: Bool }
struct OutgoingMessage: Codable, Identifiable {
    enum Status: String, Codable { case sending, accepted, failed, rejected }
    let id: String
    let botId: String
    let executorIds: [String]
    let request: MessageRequest
    let createdAt: String
    var status: Status
    var failureReason: String? = nil
    var replyTo: MessageReference? = nil
    var workContext: WorkReference? = nil
    var sequence: Int? = nil
    var expectsResponse: Bool { [.sending, .accepted].contains(status) && request.scheduledAt == nil }
    var afterRestart: Self {
        var restored = self
        if [.sending, .accepted].contains(status) { restored.status = .failed }
        return restored
    }
    var message: Message {
        Message(id: "outgoing-" + id, botId: botId, taskId: request.taskId, role: "user", kind: "text", text: request.text,
                decisionId: nil, artifactId: nil, createdAt: createdAt, streaming: false,
                recipientBotIds: request.recipientBotIds, clientRequestId: id, replyTo: replyTo, workContext: workContext)
    }
}
private struct OutgoingCache: Codable { let deviceId: String; let messages: [OutgoingMessage]; var answers: [OutgoingAnswer]? = nil }
struct OutgoingAnswer: Codable, Identifiable {
    let id: String
    let decisionId: String
    let botId: String
    let createdAt: String
    let sequence: Int
    let text: String
    let request: AnswerRequest
    var replyTo: MessageReference? = nil
    var message: Message {
        Message(id: "outgoing-" + id, botId: botId, taskId: nil, role: "user", kind: "text", text: text,
                decisionId: request.choice == "reply" ? decisionId : nil, artifactId: nil, createdAt: createdAt,
                streaming: false, clientRequestId: id, replyTo: replyTo)
    }
}
struct AnswerRequest: Codable { let choice: String; let revision: Int; let correction: String?; let optionId: String?; let draftHash: String?; let previewsHash: String? }
private struct DraftEditRequest: Encodable { let revision: Int; let draft: MessageDraft }
private struct InboxRequest: Encodable { let action: String; let revision: Int; let swipeId: String? }
private struct ReplyRequest: Encodable { let text: String; let attachmentIds: [String]; let decisionId: String?; let mentionedBotIds: [String] }
private struct ContextRequest: Encodable { let question: String }

private struct ModelResponse: Decodable, Sendable { let model: String; let modelConfigured: Bool; let baseUrl: String?; let modelCatalog: ModelCatalog? }
private struct AttachmentRequest: Encodable { let botId: String; let filename: String; let contentBase64: String }
private struct LearningResponse: Decodable, Sendable { let groups: [LearningGroup] }

private enum ProtectedCache {
    private static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Coconut", isDirectory: true)
    }

    static func read<T: Decodable>(_ name: String) -> T? {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent(name)) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    static func write<T: Encodable>(_ value: T, name: String) throws {
        var directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try directory.setResourceValues(resourceValues)
        try JSONEncoder().encode(value).write(to: directory.appendingPathComponent(name), options: [.atomic, .completeFileProtection])
    }

    static func remove(_ name: String) { try? FileManager.default.removeItem(at: directory.appendingPathComponent(name)) }
}

private extension AppStore {
    static func previewSnapshot() -> StateSnapshot {
        var resource = "PreviewState"
        #if DEBUG && targetEnvironment(simulator)
        if ProcessInfo.processInfo.arguments.contains("--card-sources-preview") { resource = "CardSourcesPreview" }
        #endif
        do { return try PreviewStateFixture.load(resource: resource) }
        catch { preconditionFailure("The bundled preview fixture could not be loaded: \(error)") }
    }
}

private struct ApprovalRequest: Encodable { let mode: String }
private struct BotApprovalRequest: Encodable {
    let value: String?
    enum CodingKeys: String, CodingKey { case approvalOverride }
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let value { try container.encode(value, forKey: .approvalOverride) }
        else { try container.encodeNil(forKey: .approvalOverride) }
    }
}

private struct WorkRefreshResponse: Decodable { let ok: Bool }

struct WorkDismissal: Identifiable { let id: String; let artifactId: String; let url: String }
private struct WorkDismissRequest: Encodable { let dismissalId: String; let undo: Bool }
