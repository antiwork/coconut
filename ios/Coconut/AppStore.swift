import Foundation
import Observation
import UIKit
import CryptoKit
import Network

@MainActor @Observable
final class AppStore {
    var bots: [Bot] = []
    var tasks: [AgentTask] = []
    var messages: [Message] = []
    var outgoingMessages: [OutgoingMessage] = []
    var decisions: [Decision] = []
    var artifacts: [Artifact] = []
    var approvals: ApprovalSettings?
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
    @ObservationIgnored private var receivedNetworkPath = false
    @ObservationIgnored private var cachedState: StateSnapshot?
    @ObservationIgnored private var cacheTask: Task<Void, Never>?
    @ObservationIgnored private let cacheQueue = DispatchQueue(label: "coconut.display-cache", qos: .utility)
    @ObservationIgnored private var snapshotRequest: (id: UUID, generation: UUID, client: APIClient, task: Task<StateSnapshot, Error>)?

    var hasPairing: Bool { pairing != nil }
    var isBusy: Bool { writeCount > 0 }
    var pendingDecisions: [Decision] { decisions.filter(\.isPending) }
    var activeTasks: [AgentTask] { tasks.filter(\.isActive) }
    func isBotActive(_ bot: Bot) -> Bool {
        guard !bot.archived else { return false }
        if outgoingMessages.contains(where: { $0.expectsResponse && $0.executorIds.contains(bot.id) }) { return true }
        guard isDemo || connection == .connected else { return false }
        // A pending decision can mask independent work in the bot's aggregate status.
        return ["working", "running"].contains(bot.status)
            || tasks.contains { $0.botId == bot.id && ["working", "running"].contains($0.status) }
    }
    func conversationBotId(for decision: Decision) -> String {
        tasks.first { $0.id == decision.taskId }?.chatBotId ?? decision.botId
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
            apply(Self.previewSnapshot(), persist: false)
            connection = .preview
            return
        }
        if let configuration: PairingConfiguration = ProtectedCache.read("pairing.json"), PairingToken.read() != nil {
            pairing = configuration
            serverURL = configuration.serverURL
            fingerprint = configuration.fingerprint
            connection = .disconnected
            if let cache: OutgoingCache = ProtectedCache.read("outgoing.json"), cache.deviceId == configuration.deviceId {
                outgoingMessages = cache.messages.map(\.afterRestart)
            }
            if let cache: SnapshotCache = ProtectedCache.read("snapshot.json") {
                apply(cache.snapshot, persist: false)
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

    private func networkChanged(available: Bool) {
        let initial = !receivedNetworkPath
        receivedNetworkPath = true
        // A default route does not prove whether the Mac is reachable.
        if available, hasPairing, foreground, !initial {
            stopPolling()
            beginPolling()
        }
    }

    private func stopPolling() {
        connectionGeneration = UUID()
        pollTask?.cancel()
        pollTask = nil
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
            try PairingToken.save(response.token)
            let configuration = PairingConfiguration(serverURL: address, fingerprint: pin, deviceId: response.deviceId)
            try ProtectedCache.write(configuration, name: "pairing.json")
            outgoingMessages = []
            ProtectedCache.remove("outgoing.json")
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
        outgoingMessages = []
        ProtectedCache.remove("outgoing.json")
        pairing = nil
        PairingToken.delete()
        ProtectedCache.remove("pairing.json")
        cacheQueue.sync { ProtectedCache.remove("snapshot.json") }
        bots = []; tasks = []; messages = []; decisions = []; artifacts = []; learning = []
        routines = []
        cursor = 0
        lastSync = nil
        model = ""
        modelBaseURL = ""
        modelConfigured = false
        modelCatalog = nil
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
    func send(bot: Bot, text: String, mode: String = "auto", taskId: String? = nil, attachments: [String] = [], scheduledAt: String? = nil, recipientBotIds: [String] = []) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !attachments.isEmpty else { return false }
        guard ["auto", "steer", "queue"].contains(mode) else { return false }
        let selectedMode = recipientBotIds.isEmpty && scheduledAt == nil ? mode : "queue"
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
            if !recipientBotIds.isEmpty {
                return !previewMentionTasks(conversationBotId: bot.id, text: text, scheduledAt: scheduledAt, recipientBotIds: recipientBotIds).isEmpty
            }
            let target: AgentTask
            if selectedMode == "queue" || (selectedMode == "auto" && currentTask == nil) {
                let id = UUID().uuidString
                target = AgentTask(id: id, botId: bot.id, sessionId: bot.sessionId, title: String(text.prefix(72)), status: scheduledAt == nil ? "queued" : "scheduled", summary: "Preview: no agent execution.",
                                   createdAt: Self.now, updatedAt: Self.now, scheduledAt: scheduledAt, threadId: id, conversationBotId: bot.id)
                tasks.append(target)
            } else if let currentTask { target = currentTask }
            else { return false }
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "user", kind: "text", text: text,
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "system", kind: "text",
                                    text: "Preview: no agent execution.",
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            return true
        }
        let request = MessageRequest(text: text, mode: selectedMode,
            taskId: selectedMode == "steer" ? currentTask?.id : nil, scheduledAt: scheduledAt,
            attachmentIds: attachments, recipientBotIds: recipientBotIds)
        let outgoing = OutgoingMessage(id: UUID().uuidString, botId: bot.id,
            executorIds: recipientBotIds.isEmpty ? [selectedMode == "steer" ? currentTask!.botId : bot.id] : recipientBotIds,
            request: request, createdAt: Self.now, status: .sending)
        outgoingMessages.append(outgoing)
        do { try saveOutgoing() }
        catch {
            outgoingMessages.removeAll { $0.id == outgoing.id }
            report(error)
            return false
        }
        await deliverOutgoing(outgoing.id)
        // The draft is saved locally even when the Mac has not confirmed it yet.
        return true
    }

    func retryOutgoing(_ id: String) async {
        guard let index = outgoingMessages.firstIndex(where: { $0.id == id && $0.status == .failed }) else { return }
        outgoingMessages[index].status = .sending
        await deliverOutgoing(id)
    }

    private func deliverOutgoing(_ id: String) async {
        guard let outgoing = outgoingMessages.first(where: { $0.id == id }) else { return }
        let result: AgentTask? = await write("bots/\(outgoing.botId)/messages", payload: outgoing.request,
            as: AgentTask.self, quiet: true, idempotencyKey: id, onFailure: { failure in
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
                              scheduledAt: outgoing.request.scheduledAt, recipientBotIds: outgoing.request.recipientBotIds)
        if !saved { outgoingMessages.append(outgoing); try? saveOutgoing() }
    }

    private func saveOutgoing() throws {
        guard let pairing else { return }
        try ProtectedCache.write(OutgoingCache(deviceId: pairing.deviceId, messages: outgoingMessages), name: "outgoing.json")
    }

    func conversationMessages(for botId: String) -> [Message] {
        let confirmed = messages.filter { $0.botId == botId || ($0.isBotExchange && $0.exchangeBotIds.contains(botId)) }
        let confirmedRequests = Set(confirmed.compactMap(\.clientRequestId))
        return (confirmed.map { message in
            var displayed = message
            if let requestId = message.clientRequestId { displayed.id = "outgoing-" + requestId }
            return displayed
        } + outgoingMessages.filter { $0.botId == botId && !confirmedRequests.contains($0.id) }.map(\.message))
            .sorted { $0.createdAt < $1.createdAt }
    }

    @discardableResult
    func reply(task: AgentTask, text: String, attachments: [String] = [], decisionId: String? = nil, recipientBotIds: [String] = []) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return false }
        if isDemo {
            guard let current = tasks.first(where: { $0.id == task.id }) else { return false }
            if !recipientBotIds.isEmpty {
                return !previewMentionTasks(conversationBotId: current.conversationBotId ?? current.botId, threadId: current.threadId,
                                            text: text, recipientBotIds: recipientBotIds).isEmpty
            }
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
                                                                                         decisionId: decisionId, recipientBotIds: recipientBotIds), as: AgentTask.self)
        return result != nil
    }

    private func previewMentionTasks(conversationBotId: String, threadId: String? = nil, text: String,
                                     scheduledAt: String? = nil, recipientBotIds: [String]) -> [AgentTask] {
        var seen = Set<String>()
        let recipients = recipientBotIds.filter { seen.insert($0).inserted }
        let selectedBots = recipients.compactMap { id in bots.first { $0.id == id && !$0.archived } }
        guard selectedBots.count == recipients.count else { error = "One of the selected bots is unavailable."; return [] }
        let threadId = threadId ?? UUID().uuidString
        let dispatched = selectedBots.map { bot in
            AgentTask(id: UUID().uuidString, botId: bot.id, sessionId: bot.sessionId, title: String(text.prefix(72)), status: scheduledAt == nil ? "queued" : "scheduled", summary: "Preview mention. No agent execution.",
                      createdAt: Self.now, updatedAt: Self.now, scheduledAt: scheduledAt, threadId: threadId, conversationBotId: conversationBotId)
        }
        guard let first = dispatched.first else { return [] }
        tasks.append(contentsOf: dispatched)
        messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: first.id, role: "user", kind: "text", text: text,
                                decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: threadId,
                                recipientBotIds: recipients))
        for task in dispatched {
            messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: task.id, role: "assistant", kind: "text",
                                    text: "Preview: I received your message. My task stays in this thread.", decisionId: nil, artifactId: nil,
                                    createdAt: Self.now, streaming: false, threadId: threadId, senderBotId: task.botId))
        }
        return dispatched
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

    func answer(_ decision: Decision, choice: String, correction: String? = nil, optionId: String? = nil) async {
        guard ["approve", "reject", "modify"].contains(choice) else { return }
        if isDemo {
            guard let index = decisions.firstIndex(where: { $0.id == decision.id }), decisions[index].isPending else { return }
            let option = decisions[index].options.first { $0.id == optionId }
            if choice == "approve" && !decision.options.isEmpty && option == nil {
                error = "Choose an option before approval."
                return
            }
            if let option, option.answer != choice { error = "This option requires a different response."; return }
            decisions[index].status = choice == "modify" ? "modified" : choice == "approve" ? "executed" : "rejected"
            decisions[index].correction = correction ?? option?.modification
            decisions[index].selectedOptionId = option?.id
            decisions[index].selectedOptionLabel = option?.label
            if let action = option?.action {
                decisions[index].resource = action.filename
                decisions[index].proposalPreview = action.content
            }
            decisions[index].decidedBy = "You (preview)"
            decisions[index].decidedAt = Self.now
            decisions[index].outcome = choice == "modify" ? "Preview: the bot would prepare a new proposal. No revised action is approved." : choice == "approve" ? "Preview result saved. No real action ran." : "You rejected the action."
            if let taskIndex = tasks.firstIndex(where: { $0.id == decision.taskId }) {
                tasks[taskIndex].status = choice == "modify" ? "queued" : "completed"
                tasks[taskIndex].summary = decisions[index].outcome ?? ""
            }
            if let botIndex = bots.firstIndex(where: { $0.id == decision.botId }) { bots[botIndex].status = choice == "modify" ? "working" : "idle" }
            if choice == "approve" {
                let task = tasks.first { $0.id == decision.taskId }
                let conversationBotId = task?.conversationBotId ?? decision.botId
                let artifact = Artifact(id: UUID().uuidString, botId: decision.botId, taskId: decision.taskId, title: option?.action?.filename ?? "retry-fix.patch", kind: "file",
                                        status: "ready", preview: decisions[index].proposalPreview ?? "Preview result.", url: nil, createdAt: Self.now)
                artifacts.append(artifact)
                messages.append(Message(id: UUID().uuidString, botId: conversationBotId, taskId: decision.taskId, role: "assistant", kind: "artifact", text: "",
                                        decisionId: nil, artifactId: artifact.id, createdAt: Self.now, streaming: false,
                                        threadId: task?.threadId, senderBotId: decision.botId))
            }
            return
        }
        let recorded: Decision? = await write("decisions/\(decision.id)/answer", payload: AnswerRequest(choice: choice, revision: decision.revision, correction: correction, optionId: optionId), as: Decision.self)
        // Keep the receipt if the answer succeeds before state sync recovers.
        if let recorded, let index = decisions.firstIndex(where: { $0.id == recorded.id }),
           decisions[index].isPending, decisions[index].revision == recorded.revision {
            decisions[index] = recorded
        }
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
        do { try await refresh(using: client) }
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

    func updateMCP(_ request: MCPRequest) async -> MCPResponse? {
        let result: MCPResponse? = await write("settings/mcp", payload: request, as: MCPResponse.self)
        if let result { mcpServers = result.servers }
        return result
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

    private func write<Response: Decodable & Sendable, Payload: Encodable>(_ path: String, method: String = "POST", payload: Payload, as type: Response.Type, quiet: Bool = false, idempotencyKey: String? = nil, onFailure: ((Error) -> Void)? = nil) async -> Response? {
        guard let pairing, let token = PairingToken.read() else {
            if !quiet { error = "Pair with your Mac in Settings to continue." }
            return nil
        }
        writeCount += 1
        defer { writeCount -= 1 }
        var signature: String?
        do {
            if client == nil { client = try APIClient(serverURL: pairing.serverURL, fingerprint: pairing.fingerprint, token: token) }
            guard let client else { return nil }
            let body = try APIJSON.encode(payload)
            let requestSignature = method + " " + path + " " + SHA256.hash(data: body).description
            signature = requestSignature
            let key = idempotencyKey ?? uncertainWriteKeys[requestSignature] ?? UUID().uuidString
            if idempotencyKey == nil { uncertainWriteKeys[requestSignature] = key }
            let result: Response = try await client.request(path, method: method, body: body, idempotencyKey: key)
            uncertainWriteKeys.removeValue(forKey: requestSignature)
            if !quiet { error = nil }
            do { try await refresh(using: client) }
            catch {
                recordSyncFailure(error)
                beginPolling()
            }
            return result
        } catch {
            onFailure?(error)
            if let signature, let serviceError = error as? ServiceError,
               case .response(let status, _) = serviceError, (400..<500).contains(status) {
                uncertainWriteKeys.removeValue(forKey: signature)
            }
            if quiet {
                // An HTTP error confirms reachability even when the action fails.
                if let serviceError = error as? ServiceError, case .response = serviceError {} else { recordSyncFailure(error) }
            }
            else {
                report(error)
                if error is URLError { recordSyncFailure(error) }
            }
            return nil
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
                    Task { await self.syncPushRegistration() }
                    failures = 0
                } catch is CancellationError { break }
                catch let error as URLError where error.code == .cancelled && Task.isCancelled { break }
                catch {
                    guard self.connectionGeneration == generation else { break }
                    self.recordSyncFailure(error)
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
        if snapshotRequest == nil || snapshotRequest?.generation != generation || snapshotRequest?.client !== client {
            snapshotRequest = (UUID(), generation, client, Task { try await client.request("state") })
        }
        guard let request = snapshotRequest else { return }
        defer { if snapshotRequest?.id == request.id { snapshotRequest = nil } }
        let snapshot = try await request.task.value
        try Task.checkCancellation()
        guard self.client === client, connectionGeneration == generation else { throw CancellationError() }
        if snapshot.cursor >= cursor { apply(snapshot, persist: true) }
        connection = .connected
        syncIssue = nil
        Task { await syncPushRegistration() }
    }

    private func apply(_ snapshot: StateSnapshot, persist: Bool) {
        let confirmed = Set(snapshot.messages.compactMap(\.clientRequestId))
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
        mcpServers = snapshot.mcpServers ?? []
        if routines != snapshot.routines ?? [] { routines = snapshot.routines ?? [] }
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
        guard !isDemo, !registeringPush, let client, let pairing, connection == .connected else { return }
        registeringPush = true
        defer { registeringPush = false }
        let push = PushNotifications.shared
        await push.refresh(requestPermission: pushConfigured)
        guard self.client === client else { return }
        let signature = [pairing.deviceId, push.environment, push.deviceToken ?? "", String(push.allowed)].joined(separator: ":")
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
                    body: APIJSON.encode(PushRegistration(token: token, environment: push.environment, enabled: true, relay: relay)),
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

    func acceptsNotification(_ target: NotificationTarget) -> Bool {
        target.deviceId == pairing?.deviceId
    }

    private func report(_ error: Error) {
        if let error = error as? URLError {
            self.error = "Couldn't complete this request. Try again."
            syncIssue = ConnectionFailure.message(for: error, serverURL: serverURL)
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
private struct SnapshotCache: Codable, Sendable { let snapshot: StateSnapshot; let syncedAt: Date }
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
struct MessageRequest: Codable { let text: String; let mode: String; let taskId: String?; let scheduledAt: String?; let attachmentIds: [String]; let recipientBotIds: [String] }
struct OutgoingMessage: Codable, Identifiable {
    enum Status: String, Codable { case sending, accepted, failed, rejected }
    let id: String
    let botId: String
    let executorIds: [String]
    let request: MessageRequest
    let createdAt: String
    var status: Status
    var failureReason: String? = nil
    var expectsResponse: Bool { [.sending, .accepted].contains(status) && request.scheduledAt == nil }
    var afterRestart: Self {
        var restored = self
        if [.sending, .accepted].contains(status) { restored.status = .failed }
        return restored
    }
    var message: Message {
        Message(id: "outgoing-" + id, botId: botId, taskId: request.taskId, role: "user", kind: "text", text: request.text,
                decisionId: nil, artifactId: nil, createdAt: createdAt, streaming: false,
                recipientBotIds: request.recipientBotIds, clientRequestId: id)
    }
}
private struct OutgoingCache: Codable { let deviceId: String; let messages: [OutgoingMessage] }
private struct AnswerRequest: Encodable { let choice: String; let revision: Int; let correction: String?; let optionId: String? }
private struct ReplyRequest: Encodable { let text: String; let attachmentIds: [String]; let decisionId: String?; let recipientBotIds: [String] }
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
        do { return try PreviewStateFixture.load() }
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
