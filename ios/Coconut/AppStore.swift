import Foundation
import Observation
import CryptoKit
import Network
import OSLog

@MainActor @Observable
final class AppStore {
    private static let deliveryLog = Logger(subsystem: "com.gumroad.coconut", category: "delivery")
    /// Each line names a reason why the app has no usable pairing. A line holds no token, key or code.
    private static let pairingLog = Logger(subsystem: "com.gumroad.coconut", category: "pairing")
    @ObservationIgnored let optimisticUpdates = OptimisticUpdates()
    var bots: [Bot] = []
    var tasks: [AgentTask] = []
    var messages: [Message] = [] {
        // Each row of a chat looks up its saved message. A scan of all messages for each row cost about 450 ms when a
        // chat of 1,270 rows opened. The index comes from the messages here only, so it cannot lag behind them.
        didSet {
            messageIndex.update(from: oldValue, to: messages)
            assign(\.chatSummaries, chatSummaries.updated(from: oldValue, to: messages))
        }
    }
    @ObservationIgnored private(set) var messageIndex = MessageIndex([])
    /// What the Chats list and the Mac sidebar show of the messages of each bot. They change only when a row changes.
    private(set) var chatSummaries = ChatSummaries()
    var outgoingMessages: [OutgoingMessage] = []
    var outgoingAnswers: [OutgoingAnswer] = []
    @ObservationIgnored private var deliveryTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var deliveryID = UUID()
    var reactionOverrides: [String: [String]] = [:]
    var decisions: [Decision] = []
    var answeringDecisionIds: Set<String> = []
    /// The answers whose request is on its way to the Mac. Undo cannot stop them.
    var sendingAnswerIds: Set<String> = []
    /// After Undo, Inbox shows this card first.
    var restoredDecisionId: String?
    /// The seconds that the app holds an answer before it sends it. Undo in this time sends nothing.
    static let answerUndoSeconds: TimeInterval = 10
    var decisionReplyDrafts: [String: String] = [:]
    var artifacts: [Artifact] = [] {
        didSet { assign(\.transcriptArtifacts, artifacts.filter { $0.kind == "task" }) }
    }
    /// Only saved task answers change a transcript. Work cards change on most updates, so conversations do not read them.
    private(set) var transcriptArtifacts: [Artifact] = []
    var approvals: ApprovalSettings?
    var parallelWork: ParallelWorkSettings?
    var fallback: FallbackSettings?
    var workCapacity: ParallelWorkSettings?
    var routines: [Routine] = []
    var learning: [LearningGroup] = []
    /// The documents of the Library, newest first, without their content. Nil until the first load.
    var documents: [BotDocument]?
    var documentContents: [String: BotDocument] = [:]
    var businessReports: [String: BusinessReport] = [:]
    var businessItems: [String: BusinessWork] = [:]
    @ObservationIgnored var businessGeneration = UUID()
    var businessCatalog: BusinessCatalog?
    var businessChanges = 0
    var demoBusinessWork = BusinessWork.previews
    /// The bots of other Macs that own the team pages in `documents`. They come with each list.
    var teamBots: [TeamBot] = []
    /// Counts the pins and the deletes that the Mac confirmed to this app. A list that the app asked for before such a
    /// change can be older than the change.
    @ObservationIgnored private var documentChanges = 0
    /// Numbers the list requests. Only the answer of the latest request replaces the list.
    @ObservationIgnored private var documentRequests = 0
    /// The load of the latest list request. The caller of an earlier request waits for its result.
    @ObservationIgnored private var latestDocumentLoad: Task<Bool, Never>?
    /// The teams of the paired Mac, for the Team screen. Nil until the first load.
    var team: TeamRooms?
    /// The text of the last failed load of the teams. The screen shows it only while it has no list.
    var teamLoadError: String?
    /// An invite link that the person tapped. The join sheet takes it. It stays in memory only.
    var teamJoin: TeamJoinRequest?
    /// Counts the team changes that the Mac confirmed to this app. A list that the app asked for before such a
    /// change can be older than the change.
    @ObservationIgnored private var teamChanges = 0
    /// Numbers the list requests of the teams. Only the answer of the latest request replaces the list.
    @ObservationIgnored private var teamRequests = 0
    @ObservationIgnored private var latestTeamLoad: Task<Bool, Never>?
    /// The number of the latest write that the app started, by room. The answer of an earlier write is older.
    @ObservationIgnored private var teamWrites: [String: Int] = [:]
    /// The new choice of each named chat whose request to mute or unmute runs now, by the ID of the chat
    /// (TeamChatMute). The Team screen shows it at once, and the actions of that chat wait until the request ends.
    private(set) var teamChatMuteChoices: [String: Bool] = [:]
    /// The new choice of each chat whose request to pin or unpin runs now, by TeamChatPin.key. Its item waits.
    private(set) var teamChatPinChoices: [String: Bool] = [:]

    /// The teams as the Chats list and the Mac sidebar show them: with each choice that waits for the Mac
    /// (TeamChatChoices).
    var shownTeamRooms: [TeamRoom] {
        TeamChatChoices.applying(mutes: teamChatMuteChoices, pins: teamChatPinChoices, to: team?.rooms ?? [])
    }
    /// The number of the latest share or stop of each page. Only the answer of that act goes into the list.
    @ObservationIgnored private var teamWriteCount = 0
    /// The filter chip of the Chats list. It stays while the app runs. A new pairing, an unpair and a Mac without a
    /// team set People or Agents to All (`ChatFilter.effective`). Unread also works without a team.
    var chatFilter: ChatFilter = .all
    /// The chats of the teams that the person opened, by chat: the Team chat of a room, or a direct chat. The updates
    /// keep them fresh.
    var teamChats: [TeamChatKey: TeamChat] = [:]
    /// The choice of the person in the filter of the alerts of a chat on screen, and the alerts of that choice
    /// (TeamAlertFilters). "All" has no entry. Both live in this app only, and both go when the chat closes.
    var teamAlertFilters: [TeamChatKey: TeamAlertFilter] = [:]
    var teamAlertChats: [TeamChatKey: TeamChat] = [:]
    @ObservationIgnored private var teamAlertRequests: [TeamChatKey: Int] = [:]
    /// The number of the latest request of the newest page, by chat. Only its answer goes into the chat.
    @ObservationIgnored private var teamChatRequests: [TeamChatKey: Int] = [:]
    @ObservationIgnored private var teamChatRequestCount = 0
    /// Counts the messages, edits and removals that the Mac confirmed, by chat. A page that the app asked for before
    /// such a change can be older than the change.
    @ObservationIgnored private var teamChatChanges: [TeamChatKey: Int] = [:]
    /// Counts the updates that say that a document changed. A page of a chat that the app asked for before such an
    /// update can show a page that went.
    @ObservationIgnored private var teamDocumentChanges = 0
    /// The read position that the Mac confirmed, and the one on its way, by chat.
    @ObservationIgnored private var teamReadPosted: [TeamChatKey: Int] = [:]
    @ObservationIgnored private var teamReadPosting: [TeamChatKey: Int] = [:]
    /// The chats on screen. Only they read their older pages again after a change.
    @ObservationIgnored private var teamChatsOnScreen: Set<TeamChatKey> = []
    /// The number of the latest pass that reads the older pages of a chat again, by chat.
    @ObservationIgnored private var teamOlderRequests: [TeamChatKey: Int] = [:]
    /// The number of the newest local change of each message (an edit, a removal, a send), by chat and message ID. An
    /// answer of the Mac applies only while its change is still the newest one of that message.
    @ObservationIgnored private var teamMessageChanges: [TeamChatKey: [String: Int]] = [:]
    @ObservationIgnored private var teamMessageChangeCount = 0
    /// The number of the writer of the reaction of this person, by chat and message ID (writeTeamReaction). At most
    /// one writer runs for a message.
    @ObservationIgnored private var teamReactionWriters: [TeamChatKey: [String: Int]] = [:]
    @ObservationIgnored private var teamReactionWriterCount = 0
    /// The pace of the calls that tell the Mac that the person types, by room (TeamTypingPace).
    @ObservationIgnored private var teamTypingPace: [TeamChatKey: TeamTypingPace] = [:]
    /// Counts the lists of the teams that the app took. A read answer from before a newer list does not set the count.
    @ObservationIgnored private var teamListVersion = 0
    /// A Library page that a chat quotes in its composer when it opens: "Ask the bot about this page". By bot ID.
    var pageQuotes: [String: MessageReference] = [:]
    /// Forwarded messages that a chat puts into its composer when it opens (MessageForward). By bot ID.
    var forwardedDrafts: [String: String] = [:]
    /// The new chat that puts the cursor in its message field when it opens (newChat). The chat takes it one time.
    var composerFocusRequest: String?
    /// A Library page that a direct chat with a team bot quotes in its composer when it opens: "Chat about this page".
    /// By chat key, so only the chat of that key shows it.
    var teamPageQuotes: [TeamChatKey: TeamAboutPage] = [:]
    var connection: ConnectionState = .unconfigured {
        // A photo from the Mac that failed while the phone was offline loads again.
        didSet { if connection == .connected, oldValue != .connected { NotificationCenter.default.post(name: .coconutMacConnected, object: nil) } }
    }
    var lastSync: Date?
    var error: String?
    var syncIssue: String?
    var model = ""
    var modelBaseURL = ""
    var modelConfigured = false
    // A setup link that the person tapped, waiting for the pairing page. It holds a one-time code.
    var setupLink: String?
    var mcpServers: [MCPServer] = []
    var modelCatalog: ModelCatalog?
    /// The usage of the agents by day (`GET /v1/usage`). Nil before the first read.
    var usage: UsageReport?
    /// Why the store has no usage: the Mac is not connected, or its Coconut is older than this app.
    var usageIssue: String?
    /// The tokens and the cost of each chat that the person opened (ChatSpend.swift), by chat ID.
    var chatUsage: [String: ChatUsage] = [:]
    @ObservationIgnored private var usageReadAt: Date?
    var provider = ""
    var pushConfigured = false
    var pushRegistrationError: String?
    var pushStatus: PushStatus?
    @ObservationIgnored private var registeredPushSignature: String?
    @ObservationIgnored private var registeredPushAt = Date.distantPast
    @ObservationIgnored private var sentPreviewReports: PreviewReports?
    @ObservationIgnored private var registeringPush = false
    @ObservationIgnored private var pushSyncRequested = false
    let isDemo: Bool
    /// The GitHub cards of the chats (GitHubPreview.swift).
    @ObservationIgnored let githubEmbeds = GitHubEmbeds()

    private var pairing: PairingConfiguration?
    /// True while pairing.json exists but the device cannot read it now. The file has complete protection, so a Mac
    /// with a locked screen refuses the read, and a locked iPhone too. An update often starts the Mac app again behind
    /// a locked screen. The pairing is saved, not missing: the app shows no pairing page and reads the file again.
    private var savedPairingLocked = false
    @ObservationIgnored private var savedPairingRetry: Task<Void, Never>?
    private var writeCount = 0
    @ObservationIgnored private var client: APIClient?
    // The one tunnel socket of a version 2 pairing. Every client of the pairing shares it.
    @ObservationIgnored private var tunnel: TunnelConnection?
    // The relay or the Mac refused this pairing. Automatic polling stays off until a pairing succeeds or the
    // person taps Reconnect, so the phone does not ask the relay again with the same rejected credentials.
    @ObservationIgnored private(set) var repairRequired = false
    @ObservationIgnored var tunnelSocketFactory: TunnelSocketFactory = URLSessionTunnelSocket.factory
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var foreground = false
    @ObservationIgnored private var cursor = 0
    @ObservationIgnored private var connectionGeneration = UUID()
    #if DEBUG && targetEnvironment(simulator)
    // A test server's loopback address and one-time code. The store pairs with them when it comes to the foreground.
    @ObservationIgnored private var loopbackBootstrap: (address: String, code: String)?
    #endif
    @ObservationIgnored private var uncertainWriteKeys: [String: String] = [:]
    @ObservationIgnored private var networkMonitor: NWPathMonitor?
    @ObservationIgnored private var networkAvailable: Bool?
    @ObservationIgnored private var cachedState: StateSnapshot?
    @ObservationIgnored private var cacheTask: Task<Void, Never>?
    @ObservationIgnored private var writeRefreshTask: Task<Void, Never>?
    @ObservationIgnored private var writeRefreshNeeded = false
    @ObservationIgnored private var pendingCache: SnapshotCache?
    @ObservationIgnored private var cacheWrittenAt = Date.distantPast
    @ObservationIgnored private let cacheQueue = DispatchQueue(label: "coconut.display-cache", qos: .utility)
    @ObservationIgnored private var nativeSettingsTask: Task<Void, Never>?
    @ObservationIgnored private var nativeSettingsReadAt: Date?
    @ObservationIgnored private var nativeSettingsAttemptAt: Date?
    @ObservationIgnored private var nativeSettingsPending = false
    @ObservationIgnored private var nativeSettingsRequiredCursor = 0
    var historyLoading: Set<String> = []
    /// The live activity of the turns whose live panel is open, by task (LiveActivity.swift).
    var turnActivity: [String: TurnActivity] = [:]
    /// The open live panels of each task, and the tasks whose feed loads now: true when the feed changed meanwhile.
    @ObservationIgnored private var activityWatchers: [String: Int] = [:]
    @ObservationIgnored private var activityLoads: [String: Bool] = [:]
    @ObservationIgnored let activityImages = NSCache<NSString, PlatformImage>()
    @ObservationIgnored private var snapshotEpoch = UUID()
    @ObservationIgnored private var snapshotRequest: (id: UUID, generation: UUID, epoch: UUID, client: APIClient, task: Task<StateSnapshot, Error>)?

    var hasPairing: Bool { pairing != nil || savedPairingLocked }
    /// The relay route of the paired Mac for the share extension of the iPhone, which sends through the relay itself
    /// (ShareDirectory.Endpoint). It holds no secret. Nil without a relay pairing, and on a Mac, where the app sends
    /// each share.
    var shareEndpoint: ShareDirectory.Endpoint? {
        #if os(macOS)
        nil
        #else
        guard let pairing, pairing.usesTunnel, let relay = pairing.relay, let route = pairing.route,
              let macKey = pairing.macKey else { return nil }
        return ShareDirectory.Endpoint(relay: relay, route: route, macKey: macKey)
        #endif
    }
    var pairedMacDescription: String {
        guard let pairing else { return savedPairingLocked ? "your Mac" : "no Mac" }
        return pairing.macName ?? "your Mac"
    }
    var canSubmitDecision: Bool { isDemo || hasPairing }
    var isBusy: Bool { writeCount > 0 }
    var pendingDecisions: [Decision] { decisions.filter(\.isPending) }
    /// The number on the app icon: the pending cards and the unread chats of "Unread" on Home, plus the items of
    /// "Needs attention" (AttentionRules.badgeCount). The Mac service counts the same for the push badge of the
    /// iPhone (backend app_badge.py).
    func appBadgeCount(now: Date = Date()) -> Int {
        HomeFeed.badge(homeUnread) + AttentionRules.badgeCount(attentionItems(now: now))
    }
    /// The pending cards that the person did not answer yet.
    var pendingCardCount: Int { pendingDecisions.filter { !answeringDecisionIds.contains($0.id) }.count }
    var reconciledBadgeCount: Int? { lastSync.map { _ in appBadgeCount() } }
    @ObservationIgnored var setBadge: (Int) -> Void = { PushNotifications.shared.setBadgeCount($0) }
    /// Gets the list of the teams before and after each list that the Mac sent. The list before is nil for the first
    /// list after a start or a new pairing. The Mac app posts its banners from it (MessageBanners). The iPhone app
    /// gets a push instead, so it does nothing here.
    @ObservationIgnored var teamListLoaded: (TeamRooms?, TeamRooms) -> Void = { _, _ in }

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
            || tasks.contains { $0.botId == bot.id && $0.isRunning }
    }
    // The newest running task describes what the bot does now.
    private func runningPhase(where matches: (AgentTask) -> Bool) -> (phase: BotPhase, startedAt: Date?, detail: String?)? {
        guard let task = tasks.last(where: { $0.isRunning && matches($0) }) else { return nil }
        let detail = task.phaseDetail.flatMap { $0.isEmpty ? nil : $0 }
        return (task.phase.flatMap(BotPhase.init(rawValue:)) ?? .thinking, task.turnStartedAt?.apiDate, detail)
    }
    /// The name of the model of an agent, as its settings show it.
    func modelName(_ botId: String) -> String? {
        guard let model = bots.first(where: { $0.id == botId })?.effectiveModel else { return nil }
        return modelCatalog?.name(for: model) ?? model.model
    }
    func botPhase(_ bot: Bot) -> BotPhase? {
        guard isBotActive(bot) else { return nil }
        return runningPhase { $0.botId == bot.id }?.phase ?? .thinking
    }
    // The status row stays during a lost connection, so work in progress does not seem to stop.
    func conversationActivity(_ botId: String) -> ConversationActivity? {
        let reconnecting = !isDemo && connection != .connected
        // A reply that streams in with text is its own sign of life. Until its first words, the row says Writing.
        let streams = messages.filter { $0.botId == botId && $0.role == "assistant" && $0.streaming }
        if !streams.isEmpty {
            if streams.contains(where: TranscriptPresentation.hasStreamingWords) && !reconnecting { return nil }
            let running = runningPhase(where: { $0.chatBotId == botId })
            return ConversationActivity(phase: .writing, startedAt: running?.startedAt, reconnecting: reconnecting,
                                        model: modelName(botId), queued: queuedCount(botId))
        }
        if let running = runningPhase(where: { $0.chatBotId == botId }) {
            return ConversationActivity(phase: running.phase, startedAt: running.startedAt, reconnecting: reconnecting,
                                        detail: running.detail, model: modelName(botId), queued: queuedCount(botId))
        }
        if outgoingMessages.contains(where: { $0.botId == botId && $0.expectsResponse }) {
            return ConversationActivity(phase: .thinking, startedAt: nil, reconnecting: reconnecting, model: modelName(botId))
        }
        return nil
    }
    func botName(_ id: String?) -> String { bots.first { $0.id == id }?.name ?? "Agent" }

    // A card waits for an answer in this bot's chat.
    func botNeedsYou(_ bot: Bot) -> Bool {
        decisions.contains { $0.isPending && !answeringDecisionIds.contains($0.id) && conversationBotId(for: $0) == bot.id }
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
        case .macOffline: return TunnelError.macOffline.message
        case .relayUnreachable: return TunnelError.relayUnreachable.message
        case .needsRepair: return TunnelError.needsRepair.message
        }
    }

    /// The demo that a person chose on the pairing screen: preview data on this device, with no Mac. It stays on until
    /// the person leaves it. App Review uses it, because a reviewer has no Mac with Hermes.
    static let demoKey = "coconut.demo"
    static var launchesDemo: Bool {
        ProcessInfo.processInfo.arguments.contains("--demo") || UserDefaults.standard.bool(forKey: demoKey)
    }

    init(demo: Bool = AppStore.launchesDemo) {
        isDemo = demo
        PhotoLoader.shared.macFile = { [weak self] id in await self?.macFileData(id: id) }
        PhotoLoader.shared.teamFile = { [weak self] address in await self?.teamFileData(address) }
        githubEmbeds.fetch = { [weak self] url in await self?.githubPreview(url) }
        if demo {
            setBadge = { _ in }
            apply(Self.previewSnapshot(), persist: false)
            connection = .preview
            #if DEBUG && targetEnvironment(simulator)
            if ProcessInfo.processInfo.arguments.contains("--attention-preview") { applyAttentionPreview() }
            if ProcessInfo.processInfo.arguments.contains("--team-preview") { applyTeamPreview() }
            #endif
            return
        }
        loadSavedPairing()
        #if DEBUG && targetEnvironment(simulator)
        configureFromLaunchEnvironment()
        #endif
    }

    /// Reads the saved pairing and the saved state. A device that cannot read the files now keeps the pairing, and
    /// the store reads them again each second. Before this, a Mac app that an update started behind a locked screen
    /// showed the pairing page until the person started it again.
    private func loadSavedPairing() {
        let saved: (configuration: PairingConfiguration, outgoing: OutgoingCache?, snapshot: SnapshotCache?)?
        do { saved = try Self.readSavedPairing() } catch {
            // The retry below runs already.
            guard !savedPairingLocked else { return }
            savedPairingLocked = true
            connection = .disconnected
            let failure = error as NSError
            Self.pairingLog.notice("The device cannot read the saved pairing now (\(failure.domain, privacy: .public) \(failure.code)). The app keeps the pairing and reads it again.")
            savedPairingRetry = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(1))
                    guard let self, !Task.isCancelled, self.savedPairingLocked else { return }
                    self.loadSavedPairing()
                }
            }
            return
        }
        let wasLocked = savedPairingLocked
        savedPairingLocked = false
        savedPairingRetry?.cancel()
        savedPairingRetry = nil
        guard let saved else {
            connection = .unconfigured
            return
        }
        pairing = saved.configuration
        connection = .disconnected
        if let cache = saved.outgoing, cache.deviceId == saved.configuration.deviceId {
            outgoingMessages = cache.messages.map(\.afterRestart)
            outgoingAnswers = cache.answers ?? []
            answeringDecisionIds = Set(outgoingAnswers.map(\.decisionId))
            for (chat, pending) in cache.pendingTeamMessages where !pending.isEmpty {
                teamChats[chat, default: TeamChat()].pending = pending.map(\.afterRestart)
            }
        }
        if let cache = saved.snapshot {
            apply(cache.snapshotForRestore, persist: false)
            lastSync = cache.syncedAt
        }
        if wasLocked {
            Self.pairingLog.notice("The device reads the saved pairing again.")
            resumePolling()
        }
    }

    /// The saved pairing with its saved state, or nil when this device has none. Throws when the device cannot read
    /// a file now.
    private static func readSavedPairing() throws -> (configuration: PairingConfiguration, outgoing: OutgoingCache?, snapshot: SnapshotCache?)? {
        guard let configuration: PairingConfiguration = try ProtectedCache.lookup("pairing.json") else {
            pairingLog.notice("The app has no usable pairing file, so it has no pairing.")
            return nil
        }
        // A token that the Keychain cannot read now still belongs to this pairing, and polling reads it again.
        let hasToken: Bool
        do { hasToken = try PairingToken.lookup() != nil } catch { hasToken = true }
        guard hasToken else {
            pairingLog.notice("The Keychain has no token for the pairing file, so the app has no pairing.")
            return nil
        }
        // The saved state helps the display. It is no part of the pairing, so a file of it that the device cannot read
        // is skipped, and it cannot keep a usable pairing offline.
        return (configuration, ProtectedCache.read("outgoing.json"), ProtectedCache.read("snapshot.json"))
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
            // The socket from before the change no longer reaches the relay. Close it before the next poll.
            guard let tunnel else { return beginPolling() }
            Task {
                await tunnel.close()
                beginPolling()
            }
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
            #if DEBUG && targetEnvironment(simulator)
            if let bootstrap = loopbackBootstrap {
                loopbackBootstrap = nil
                Task { await pairOverLoopback(address: bootstrap.address, code: bootstrap.code) }
                return
            }
            #endif
            // An iPhone that started the app while it was locked can read the saved pairing now.
            if savedPairingLocked { loadSavedPairing() }
            resumePolling()
        } else {
            stopPolling()
            if let cachedState { saveCache(cachedState, immediately: true) }
            if outgoingAnswers.isEmpty { closeTunnelWhenIdle() } else { sendAnswersThenClose() }
        }
    }

    /// Starts polling in the foreground. A state that is older than 30 seconds shows "Connecting" first.
    private func resumePolling() {
        guard foreground, pairing != nil, pollTask == nil, !repairRequired else { return }
        if lastSync.map({ Date().timeIntervalSince($0) > 30 }) ?? true { connection = .connecting }
        beginPolling()
    }

    // An answer can wait for its Undo window when the app leaves the foreground. Background time lets it send.
    // If iOS ends that time first, the saved answer sends at the next launch.
    private func sendAnswersThenClose() {
        let background = BackgroundTask()
        background.begin("Send answers") { [weak self] in self?.closeTunnelWhenIdle() }
        scheduleDelivery()
        Task { [weak self] in
            while let self, !self.foreground, !self.outgoingAnswers.isEmpty {
                try? await Task.sleep(for: .milliseconds(500))
            }
            if let self, !self.foreground { self.closeTunnelWhenIdle() }
            background.end()
        }
    }

    // In the background the socket closes after the writes in flight finish. Notifications arrive through APNs.
    private func closeTunnelWhenIdle() {
        guard let tunnel else { return }
        // Each background period has its own task. Ending one never ends a later one.
        let background = BackgroundTask()
        background.begin("Close the Mac connection") {
            // iOS ends the background time: close at once.
            Task { await tunnel.close() }
        }
        Task {
            await tunnel.closeWhenIdle()
            background.end()
        }
    }

    private func makeClient(_ pairing: PairingConfiguration, token: String) throws -> APIClient {
        guard pairing.usesTunnel else {
            #if DEBUG && targetEnvironment(simulator)
            if let address = pairing.serverURL { return try APIClient(loopback: address, token: token) }
            #endif
            throw TunnelError.needsRepair
        }
        if let tunnel, tunnel.endpoint == pairing.tunnelEndpoint { return APIClient(tunnel: tunnel, token: token) }
        // Only missing credentials need a new pairing. A Keychain that cannot read them now throws, and polling tries again.
        guard let endpoint = pairing.tunnelEndpoint, let credentials = try TunnelCredentials.lookup(),
              let phoneKey = credentials.phoneKey else { throw TunnelError.needsRepair }
        let connection = TunnelConnection(endpoint: endpoint, phoneKey: phoneKey, credential: .token(credentials.clientToken),
                                          makeSocket: tunnelSocketFactory)
        if let previous = tunnel { Task { await previous.close() } }
        tunnel = connection
        return APIClient(tunnel: connection, token: token)
    }

    static func isTransportFailure(_ error: Error) -> Bool { error is URLError || error is TunnelError }

    // Pairs through the relay with a version 2 link. A failure keeps the current pairing.
    func pair(setup: PairingSetup) async -> Bool {
        guard !isDemo else { return false }
        error = nil
        writeCount += 1
        defer { writeCount -= 1 }
        let previousConnection = connection
        connection = .connecting
        // Each pairing makes a new phone key. The Mac binds it to the new device.
        let phoneKey = Curve25519.KeyAgreement.PrivateKey()
        let pairingTunnel = TunnelConnection(endpoint: setup.endpoint, phoneKey: phoneKey,
                                             credential: .ticket(secret: setup.secret), makeSocket: tunnelSocketFactory)
        defer { Task { await pairingTunnel.close() } }
        do {
            let candidate = APIClient(tunnel: pairingTunnel, token: nil)
            let response: PairResponse = try await candidate.request("pair", method: "POST", body: APIJSON.encode(
                PairRequest(code: setup.code, deviceName: Device.name)), retryTransient: false)
            guard let handshakeHash = await pairingTunnel.handshakeHash else { throw TunnelError.protocolViolation }
            // A closed pairing screen cancels its pairing. Nothing is saved after that, so a late answer keeps the Mac.
            try Task.checkCancellation()
            let credentials = TunnelCredentials(phoneKey: phoneKey, clientToken: TunnelKeys.clientToken(handshakeHash: handshakeHash))
            disconnect()
            try PairingToken.save(response.token)
            try credentials.save()
            let configuration = PairingConfiguration(setup: setup, deviceId: response.deviceId)
            try ProtectedCache.write(configuration, name: "pairing.json")
            await adopt(configuration)
            return true
        } catch {
            if !(error is CancellationError) { self.error = Self.pairingFailure(error) }
            connection = hasPairing ? previousConnection : .unconfigured
            return false
        }
    }

    // The new pairing starts without the outgoing items and the cursor of the earlier one.
    private func adopt(_ configuration: PairingConfiguration) async {
        optimisticUpdates.reset()
        documentContents = [:]; resetBusiness()
        resetDelivery()
        chatFilter = .all
        outgoingMessages = []; outgoingAnswers = []; answeringDecisionIds = []; sendingAnswerIds = []; restoredDecisionId = nil
        ProtectedCache.remove("outgoing.json")
        pairing = configuration
        cursor = 0
        error = nil
        await reconnect()
    }

    static func pairingFailure(_ error: Error) -> String {
        switch error as? TunnelError {
        case .needsRepair?: "This pairing link no longer works. It can be used once, for ten minutes. Ask your Hermes for a new pairing link."
        case .macOffline?, .backendDown?: "Your Mac does not answer. Check that it is awake, then ask your Hermes for a new pairing link."
        case .relayUnreachable?: TunnelError.relayUnreachable.message
        case .timedOut?, .connectionLost?: "The pairing result is unknown. Ask your Hermes for a new pairing link, then pair again."
        case let failure?: failure.message
        case nil: error.localizedDescription
        }
    }

    #if DEBUG && targetEnvironment(simulator)
    // Pairs with a test server on the Mac's loopback, with its one-time code.
    func pairOverLoopback(address: String, code: String) async {
        guard !isDemo else { return }
        writeCount += 1
        defer { writeCount -= 1 }
        connection = .connecting
        do {
            let candidate = try APIClient(loopback: address, token: nil)
            let response: PairResponse = try await candidate.request("pair", method: "POST", body: APIJSON.encode(
                PairRequest(code: code, deviceName: Device.name)), retryTransient: false)
            disconnect()
            try PairingToken.save(response.token)
            let configuration = PairingConfiguration(loopback: address, deviceId: response.deviceId)
            try ProtectedCache.write(configuration, name: "pairing.json")
            await adopt(configuration)
        } catch {
            report(error)
            connection = hasPairing ? .disconnected : .unconfigured
        }
    }
    #endif

    // A new pairing and the Reconnect button call this. Both try the pairing again, also after a refusal.
    func reconnect() async {
        guard !isDemo else { return }
        repairRequired = false
        stopPolling()
        if savedPairingLocked {
            // The device cannot read the saved pairing yet. The store polls as soon as the read works.
            loadSavedPairing()
            return
        }
        guard let pairing else {
            connection = .unconfigured
            return
        }
        let token: String
        do {
            guard let found = try PairingToken.lookup() else {
                connection = .unconfigured
                return
            }
            token = found
        } catch {
            // The Keychain cannot read the token now. Polling tries again.
            recordSyncFailure(error)
            if foreground { beginPolling() }
            return
        }
        let generation = connectionGeneration
        if connection != .connected { connection = .connecting }
        do {
            client = try makeClient(pairing, token: token)
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
            PreviewDiagnostics.clear()
        }
        let previousTunnel = tunnel
        tunnel = nil
        if let client {
            Task {
                let _: PushRegistrationResponse? = try? await client.request("device/push", method: "DELETE", body: Data("{}".utf8), idempotencyKey: UUID().uuidString)
                await previousTunnel?.close()
            }
        } else if let previousTunnel {
            Task { await previousTunnel.close() }
        }
        registeredPushSignature = nil
        sentPreviewReports = nil
        pushStatus = nil
        pushConfigured = false
        PushNotifications.shared.target = nil
        stopPolling()
        cacheTask?.cancel()
        cacheTask = nil
        pendingCache = nil
        cachedState = nil
        syncIssue = nil
        client = nil
        uncertainWriteKeys = [:]
        resetDelivery()
        outgoingMessages = []; outgoingAnswers = []; answeringDecisionIds = []; sendingAnswerIds = []; restoredDecisionId = nil
        pairing = nil
        savedPairingLocked = false
        savedPairingRetry?.cancel()
        savedPairingRetry = nil
        repairRequired = false
        if !isDemo {
            Self.pairingLog.notice("The app removes its saved pairing.")
            PairingToken.delete()
            TunnelCredentials.delete()
            ProtectedCache.remove("pairing.json")
            cacheQueue.sync { ProtectedCache.remove("snapshot.json") }
            ProtectedCache.remove("outgoing.json")
        }
        bots = []; tasks = []; messages = []; decisions = []; artifacts = []; learning = []
        routines = []
        // The team bots name the team pages of the earlier Mac, so the Library of a later pairing would show them.
        documents = nil; teamBots = []; latestDocumentLoad = nil
        // The teams are the teams of the earlier Mac. A list request of that Mac that is still on its way gets no
        // place to go: its client is not the client of the store.
        team = nil; teamLoadError = nil; latestTeamLoad = nil; teamWrites = [:]; teamChatMuteChoices = [:]
        teamChatPinChoices = [:]
        teamChats = [:]; teamChatRequests = [:]; teamChatChanges = [:]; teamReadPosted = [:]; teamReadPosting = [:]
        teamChatsOnScreen = []; teamOlderRequests = [:]; teamMessageChanges = [:]; teamTypingPace = [:]
        teamReactionWriters = [:]
        teamAlertFilters = [:]; teamAlertChats = [:]; teamAlertRequests = [:]
        chatFilter = .all
        approvals = nil; parallelWork = nil; fallback = nil; workCapacity = nil; mcpServers = []
        optimisticUpdates.reset()
        documentContents = [:]; resetBusiness()
        reactionOverrides = [:]; decisionReplyDrafts = [:]; answeringDecisionIds = []
        cursor = 0
        lastSync = nil
        setBadge(0)
        model = ""
        modelBaseURL = ""
        modelConfigured = false
        modelCatalog = nil
        usage = nil; usageIssue = nil; usageReadAt = nil; chatUsage = [:]
        nativeSettingsReadAt = nil
        nativeSettingsAttemptAt = nil
        nativeSettingsRequiredCursor = 0
        provider = ""
        connection = .unconfigured
    }

    /// Acceptance is local. The shared error banner reports a failed save and the projection restores confirmed data.
    @discardableResult
    func optimistically(_ resource: String, change: (UUID) -> Void, save: @escaping @MainActor () async -> Void) -> Bool {
        guard isDemo || pairing != nil else { error = "Pair with your Mac in Settings to continue."; return false }
        let pairing = pairing
        error = nil
        let parts = resource.split(separator: "/")
        let queue = resource.hasPrefix("bot/") || resource.hasPrefix("settings/") || resource.hasPrefix("routine/") ? "native-settings"
            : parts.first == "team" && parts.count > 1 ? "team/" + String(parts[1]) : resource
        optimisticUpdates.enqueue(queue, change: change) {
            guard self.pairing == pairing else { return }
            await save()
        }
        return true
    }

    /// Makes an agent on the Mac. Returns the agent, or nil when the Mac did not make it.
    @discardableResult
    func createBot(name: String, role: String, avatar: String, color: String, instructions: String, memory: String = "", pinned: Bool = false) async -> Bot? {
        if isDemo {
            let bot = Bot(id: UUID().uuidString, sessionId: UUID().uuidString, name: name, role: role, avatar: avatar,
                          color: color, instructions: instructions, status: "idle", lastMessage: "Ready when you are.",
                          unreadCount: 0, archived: false, createdAt: Self.now, memory: memory, pinned: pinned)
            bots.append(bot)
            return bot
        }
        return await write("bots", payload: BotRequest(name: name, role: role, avatar: avatar, color: color, instructions: instructions, memory: memory, pinned: pinned), as: Bot.self)
    }

    /// Asks the Mac for a new chat of an agent (newChat).
    func startChat(_ agentId: String) async -> Bot? {
        await write("bots/\(agentId)/chats", payload: EmptyRequest(), as: Bot.self)
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
        optimistically("bot/\(bot.id)", change: { transaction in
            if let value = patch.name { optimisticBot(bot.id, \.name, value, in: transaction) }
            if let value = patch.role { optimisticBot(bot.id, \.role, value, in: transaction) }
            if let value = patch.avatar { optimisticBot(bot.id, \.avatar, value, in: transaction) }
            if let value = patch.color { optimisticBot(bot.id, \.color, value, in: transaction) }
            if let value = patch.instructions { optimisticBot(bot.id, \.instructions, value, in: transaction) }
            if let value = patch.notifications { optimisticBot(bot.id, \.notifications, value, in: transaction) }
        }, save: { let _: Bot? = await self.write("bots/\(bot.id)", method: "PATCH", payload: patch, as: Bot.self) })
    }

    func updateBotInstructions(bot: Bot, text: String) async {
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].instructions = text }
            return
        }
        optimistically("bot/\(bot.id)", change: { optimisticBot(bot.id, \.instructions, text, in: $0) },
                       save: { let _: Bot? = await self.write("bots/\(bot.id)", method: "PATCH", payload: InstructionsRequest(instructions: text), as: Bot.self) })
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
        optimistically("routine/\(routine.id)", change: { transaction in
            optimisticRoutine(routine.id, \.name, routine.name, in: transaction)
            optimisticRoutine(routine.id, \.prompt, routine.prompt, in: transaction)
            optimisticRoutine(routine.id, \.cron, routine.cron, in: transaction)
            optimisticRoutine(routine.id, \.timezone, routine.timezone, in: transaction)
            optimisticRoutine(routine.id, \.enabled, routine.enabled, in: transaction)
        }, save: { let _: Routine? = await self.write("routines/\(routine.id)", method: "PATCH", payload: RoutineRequest(name: routine.name, prompt: routine.prompt,
            cron: routine.cron, timezone: routine.timezone, enabled: routine.enabled), as: Routine.self) })
    }

    func deleteRoutine(_ routine: Routine) async {
        if isDemo { routines.removeAll { $0.id == routine.id }; return }
        optimistically("routine/\(routine.id)", change: { transaction in
            optimisticUpdates.set("0:routine/\(routine.id)", to: Optional<Routine>.none, in: transaction,
                read: { self.routines.first { $0.id == routine.id } }, write: { value in
                    if let value { self.routines = StateUpdates.merge([value], into: self.routines) }
                    else { self.routines.removeAll { $0.id == routine.id } }
                })
        }, save: { let _: Acknowledgement? = await self.write("routines/\(routine.id)", method: "DELETE", payload: EmptyRequest(), as: Acknowledgement.self) })
    }

    @discardableResult
    func runRoutine(_ routine: Routine) async -> AgentTask? {
        if isDemo {
            guard let index = routines.firstIndex(where: { $0.id == routine.id }) else { return nil }
            let routine = routines[index]
            guard let bot = bots.first(where: { $0.id == routine.botId }), !bot.archived else {
                error = "Restore this agent before running its routine."
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
        optimistically("bot/\(bot.id)", change: { optimisticBot(bot.id, \.pinned, pinned, in: $0) },
                       save: { let _: Bot? = await self.write("bots/\(bot.id)", method: "PATCH", payload: PinRequest(pinned: pinned), as: Bot.self) })
    }

    /// Archives an agent, or brings it back. An archived agent leaves the Chats list and the sidebar, and its routines
    /// stop. The section "Archived" lists it, with its conversation.
    func archiveBot(_ bot: Bot, archived: Bool) async {
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].archived = archived }
            return
        }
        optimistically("bot/\(bot.id)", change: { optimisticBot(bot.id, \.archived, archived, in: $0) },
                       save: { let _: Bot? = await self.write("bots/\(bot.id)", method: "PATCH", payload: ArchiveRequest(archived: archived), as: Bot.self) })
    }

    func deleteBot(_ bot: Bot) async {
        if isDemo {
            bots.removeAll { $0.id == bot.id }; tasks.removeAll { $0.botId == bot.id }; messages.removeAll { $0.botId == bot.id }
            decisions.removeAll { $0.botId == bot.id }; artifacts.removeAll { $0.botId == bot.id }
            routines.removeAll { $0.botId == bot.id }
            return
        }
        optimistically("bot/\(bot.id)", change: { transaction in
            optimisticUpdates.set("0:bot/\(bot.id)", to: Optional<Bot>.none, in: transaction,
                read: { self.bots.first { $0.id == bot.id } }, write: { value in
                    if let value { self.bots = StateUpdates.merge([value], into: self.bots) }
                    else { self.bots.removeAll { $0.id == bot.id } }
                })
        }, save: { let _: Acknowledgement? = await self.write("bots/\(bot.id)", method: "DELETE", payload: EmptyRequest(), as: Acknowledgement.self) })
    }

    func markRead(_ bot: Bot) async {
        guard bot.unreadCount > 0 else { return }
        if isDemo {
            if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].unreadCount = 0 }
            return
        }
        // The badge goes at once. The Mac confirms the read with its next update.
        if let index = bots.firstIndex(where: { $0.id == bot.id }) { bots[index].unreadCount = 0 }
        let _: Acknowledgement? = await write("bots/\(bot.id)/read", payload: EmptyRequest(), as: Acknowledgement.self, quiet: true)
    }

    @discardableResult
    func send(bot: Bot, text: String, mode: String = "auto", taskId: String? = nil, attachments: [String] = [], scheduledAt: String? = nil, mentionedBotIds: [String] = [], replyTo: MessageReference? = nil, workContext: WorkReference? = nil, workStatusOnly: Bool = false) async -> Bool {
        var text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !attachments.isEmpty else { return false }
        // The Mac needs a text for each message. Attachments alone go with the marker, which the chat does not show.
        if text.isEmpty { text = AttachmentOnly.text }
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
            } else if let currentTask {
                target = currentTask
                if currentTask.isRunning, let index = tasks.firstIndex(where: { $0.id == currentTask.id }) {
                    // The agent works, so the message waits in the queue of the task, as on the Mac.
                    let id = UUID().uuidString
                    messages.append(Message(id: id, botId: bot.id, taskId: target.id, role: "user", kind: "text", text: text,
                                            decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId, replyTo: replyTo))
                    tasks[index].queuedMessages = (tasks[index].queuedMessages ?? [])
                        + [QueuedMessage(id: id, text: text, createdAt: Self.now, attachmentIds: attachments)]
                    return true
                }
            }
            else { return false }
            nameDemoChat(bot.id, from: text)
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "user", kind: "text", text: text,
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId, replyTo: replyTo, workContext: workContext))
            messages.append(Message(id: UUID().uuidString, botId: bot.id, taskId: target.id, role: "system", kind: "text",
                                    text: "Preview: no agent execution.",
                                    decisionId: nil, artifactId: nil, createdAt: Self.now, streaming: false, threadId: target.threadId))
            return true
        }
        let request = MessageRequest(text: text, mode: selectedMode,
            taskId: selectedMode == "steer" ? currentTask?.id : nil, scheduledAt: scheduledAt,
            attachmentIds: attachments, recipientBotIds: [], replyToMessageId: replyTo?.documentId == nil ? replyTo?.messageId : nil, mentionedBotIds: mentionedBotIds, workArtifactId: workContext?.id, workStatusOnly: workStatusOnly, aboutDocumentId: replyTo?.documentId)
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
        askForNotificationsIfNeeded()
        // The draft is saved locally even when the Mac has not confirmed it yet.
        return true
    }

    // A bot answers later, so notifications start to matter with the first message. iOS asks only once.
    private func askForNotificationsIfNeeded() {
        guard PushNotifications.shared.authorization == .notDetermined else { return }
        Task {
            await PushNotifications.shared.refresh(requestPermission: true)
            await syncPushRegistration()
        }
    }

    func retryOutgoing(_ id: String) async {
        guard let index = outgoingMessages.firstIndex(where: { $0.id == id && $0.status == .failed }) else { return }
        outgoingMessages[index].status = .sending
        scheduleDelivery()
    }

    private func deliverOutgoing(_ id: String) async {
        guard let index = outgoingMessages.firstIndex(where: { $0.id == id }) else { return }
        // A retry of a failed message is on its way again, so Delete does not offer it while the Mac can receive it.
        outgoingMessages[index].status = .sending
        let outgoing = outgoingMessages[index]
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

    /// Removes a message that did not send. A failed message holds the messages after it to the same bot, because they
    /// go in order, so its removal lets them go. A message that reached the Mac before its answer was lost still shows
    /// from the Mac.
    @discardableResult
    func deleteUnsent(_ id: String) -> Bool {
        guard outgoingMessages.contains(where: { $0.id == id && [.failed, .rejected].contains($0.status) }) else { return false }
        let previous = outgoingMessages
        outgoingMessages.removeAll { $0.id == id }
        // Save the removal first. A saved message that stayed in outgoing.json would send at the next launch.
        do { try saveOutgoing() } catch {
            outgoingMessages = previous
            report(error)
            return false
        }
        scheduleDelivery()
        return true
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
        // The messages of the Team chats and the direct chats that the Mac did not confirm go into the same protected
        // file.
        var cache = OutgoingCache(deviceId: pairing.deviceId, messages: outgoingMessages, answers: outgoingAnswers)
        cache.pendingTeamMessages = teamChats.compactMapValues { $0.pending.isEmpty ? nil : $0.pending }
        try writeOutgoing(cache)
    }

    /// Writes outgoing.json. A test can replace it to make the write fail.
    @ObservationIgnored var writeOutgoing: (OutgoingCache) throws -> Void = { try ProtectedCache.write($0, name: "outgoing.json") }

    func decisionReference(for id: String) -> MessageReference? {
        guard let decision = decisions.first(where: { $0.id == id }) else { return nil }
        return MessageReference(messageId: id, text: decision.title,
            author: bots.first { $0.id == decision.botId }?.name ?? "Agent", role: "assistant")
    }

    func messageReference(for message: Message) -> MessageReference? {
        guard message.kind == "text", ["user", "assistant"].contains(message.role),
              !message.streaming, message.reactionEvent == nil, !message.isBotExchange,
              let saved = messageIndex.first(id: message.id, clientRequestId: message.clientRequestId).map({ messages[$0] }) else { return nil }
        // A photo message quotes as "Photo: <caption>", not as its address.
        return MessageReference(messageId: saved.id, text: saved.photo?.preview ?? saved.text,
            author: saved.role == "user" ? "You" : bots.first { $0.id == (saved.senderBotId ?? saved.botId) }?.name ?? "Agent",
            role: saved.role)
    }

    func reactions(for message: Message) -> [String] {
        guard let reference = messageReference(for: message) else { return message.reactions ?? [] }
        return reactionOverrides[reference.messageId] ?? savedMessage(id: reference.messageId)?.reactions ?? []
    }

    func react(to message: Message, emoji: String) async {
        guard MessageReactions.all.contains(emoji), let reference = messageReference(for: message),
              reactionOverrides[reference.messageId] == nil else { return }
        let id = reference.messageId
        let current = reactions(for: message)
        let active = !current.contains(emoji)
        let updated = active ? current + [emoji] : current.filter { $0 != emoji }
        if isDemo {
            if let index = messageIndex.first(id: id) { messages[index].reactions = updated }
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
                if let index = self.messageIndex.first(id: id) { self.messages[index].reactions = receipt.reactions }
                if let index = self.cachedState?.messages.firstIndex(where: { $0.id == id }) { self.cachedState?.messages[index].reactions = receipt.reactions }
            })
        if result == nil && generation == connectionGeneration { error = "Reaction not confirmed. Try again." }
    }

    func displayedMessageId(for serverId: String) -> String {
        guard let requestId = savedMessage(id: serverId)?.clientRequestId else { return serverId }
        return "outgoing-" + requestId
    }

    /// The first saved message with this ID, as `messages.first { $0.id == id }` finds it, without a scan.
    func savedMessage(id: String) -> Message? {
        messageIndex.first(id: id).map { messages[$0] }
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
            // Check the cheap state first. Finding the conversation scans every task.
            ($0.isPending || $0.isRefreshing || $0.id == focusDecisionId) && !represented.contains($0.id)
                && conversationBotId(for: $0) == botId
        }.map { decision in
            Message(id: decision.id, botId: botId, taskId: decision.taskId, role: "assistant", kind: "decision",
                    text: "", decisionId: decision.id, artifactId: nil, createdAt: decision.createdAt, streaming: false)
        }
        // Cards can outlive the loaded history page. Keep their original place in the transcript.
        let timeline = (messages + missing).sorted { $0.createdAt < $1.createdAt }
        // The messages that wait for the next turn show in the queue above the message field.
        let queued = queueHidden(botId)
        let shown = MessageQueue.transcript(timeline, without: queued.ids, attachments: queued.attachments)
        return TranscriptPresentation.visibleMessages(shown, artifacts: transcriptArtifacts, decisions: decisions)
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

    // The queue of a chat: the messages that wait for the next turn of its running task (MessageQueue.swift).

    /// The messages on their way that the running turn of this chat will hold. The Mac decides it (service `_send`):
    /// a plain message to a working task of the chat, with no card that waits.
    private func queueSending(_ botId: String) -> [OutgoingMessage] {
        guard outgoingMessages.contains(where: { $0.botId == botId }),
              let current = tasks.last(where: { $0.botId == botId && $0.chatBotId == botId && $0.sourceBotId == nil
                  && $0.scheduledAt == nil && ["working", "queued", "waiting"].contains($0.status) }),
              current.status == "working",
              !decisions.contains(where: { $0.isPending && conversationBotId(for: $0) == botId }) else { return [] }
        // A confirmed message is in the queue of its task already.
        let confirmed = Set(tasks.filter { $0.chatBotId == botId }.flatMap { $0.queuedMessages ?? [] }
            .compactMap { savedMessage(id: $0.id)?.clientRequestId })
        return outgoingMessages.filter {
            $0.botId == botId && [.sending, .accepted].contains($0.status) && $0.request.mode == "auto"
                && $0.request.scheduledAt == nil && $0.request.workArtifactId == nil && !confirmed.contains($0.id)
        }
    }

    /// The rows of the queue of a chat, in the order of their delivery.
    func messageQueue(_ botId: String) -> [QueueItem] {
        MessageQueue.items(tasks: tasks.filter { $0.chatBotId == botId && !($0.queuedMessages ?? []).isEmpty },
                           sending: queueSending(botId))
    }

    /// How many messages of this chat wait for the next turn.
    func queuedCount(_ botId: String) -> Int {
        tasks.reduce(0) { $0 + ($1.chatBotId == botId ? ($1.queuedMessages?.count ?? 0) : 0) } + queueSending(botId).count
    }

    /// The transcript rows that the queue shows instead: the IDs of the rows and the IDs of their files.
    func queueHidden(_ botId: String) -> (ids: Set<String>, attachments: Set<String>) {
        let held = tasks.filter { $0.chatBotId == botId }.flatMap { $0.queuedMessages ?? [] }
        let sending = queueSending(botId)
        return (Set(held.map { displayedMessageId(for: $0.id) } + sending.map { "outgoing-" + $0.id }),
                Set(held.flatMap(\.attachmentIds) + sending.flatMap(\.request.attachmentIds)))
    }

    /// Changes the queue of a task here at once, then on the Mac. A change that the Mac refuses goes back.
    private func changeQueue<Payload: Encodable>(_ taskId: String, path: String, method: String, payload: Payload,
                                                 change: (inout [QueuedMessage]) -> Void) async {
        guard let index = tasks.firstIndex(where: { $0.id == taskId }) else { return }
        let before = tasks[index].queuedMessages
        var queue = before ?? []
        change(&queue)
        let changed = queue.isEmpty ? nil : queue
        guard changed != before || method == "POST" else { return }
        tasks[index].queuedMessages = changed
        if isDemo { return }
        let result: AgentTask? = await write("tasks/\(taskId)/queue/\(path)", method: method, payload: payload, as: AgentTask.self)
        guard let index = tasks.firstIndex(where: { $0.id == taskId }) else { return }
        if let result { tasks[index].queuedMessages = result.queuedMessages }
        else if tasks[index].queuedMessages == changed { tasks[index].queuedMessages = before }
    }

    func editQueued(_ id: String, task: String, text: String) async {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let saved = messageIndex.first(id: id).map { messages[$0].text }
        if let index = messageIndex.first(id: id) { messages[index].text = text }
        await changeQueue(task, path: id, method: "PATCH", payload: QueueTextRequest(text: text)) { queue in
            if let index = queue.firstIndex(where: { $0.id == id }) { queue[index].text = text }
        }
        // The Mac refused the change, so the message keeps its text.
        if let saved, tasks.first(where: { $0.id == task })?.queuedMessages?.first(where: { $0.id == id })?.text == saved,
           let index = messageIndex.first(id: id) { messages[index].text = saved }
    }

    func removeQueued(_ id: String, task: String) async {
        let files = Set(tasks.first { $0.id == task }?.queuedMessages?.first { $0.id == id }?.attachmentIds ?? [])
        let removed = messages.filter { $0.id == id || ($0.taskId == task && $0.role == "user" && $0.kind == "artifact" && $0.artifactId.map(files.contains) == true) }
        // The message leaves the chat with its row of the queue, so it does not show in the transcript for a moment.
        if !removed.isEmpty { messages.removeAll { message in removed.contains { $0.id == message.id } } }
        await changeQueue(task, path: id, method: "DELETE", payload: EmptyRequest()) { queue in queue.removeAll { $0.id == id } }
        if tasks.first(where: { $0.id == task })?.queuedMessages?.contains(where: { $0.id == id }) == true, !removed.isEmpty,
           savedMessage(id: id) == nil {
            messages = (messages + removed).sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
        }
    }

    /// Puts the messages of the queue in the order of `ids`. Messages that the list does not name keep their places.
    func orderQueued(_ ids: [String], task: String) async {
        await changeQueue(task, path: "order", method: "POST", payload: QueueOrderRequest(messageIds: ids)) { queue in
            let named = Dictionary(uniqueKeysWithValues: queue.map { ($0.id, $0) })
            guard Set(ids) == Set(named.keys), ids.count == queue.count else { return }
            // The messages take the times of the queue in the new order, as the Mac gives them.
            let times = queue.map(\.createdAt).sorted()
            queue = ids.enumerated().map { index, id in
                var message = named[id]!
                message.createdAt = times[index]
                return message
            }
        }
    }

    /// Gives a queued message to the running turn at once, where the Mac can do that.
    func steerQueued(_ id: String, task: String) async {
        await changeQueue(task, path: "\(id)/steer", method: "POST", payload: EmptyRequest()) { queue in
            if let index = queue.firstIndex(where: { $0.id == id }) { queue[index].state = "steering"; queue[index].canSteer = false }
        }
    }

    @discardableResult
    func answer(_ decision: Decision, choice: String, correction: String? = nil, optionId: String? = nil,
                undoSeconds: TimeInterval = AppStore.answerUndoSeconds) async -> Bool {
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
            decisions[index].outcome = choice == "reply" ? "Reply received. The agent will continue." : choice == "modify" ? "Preview: the agent would prepare a new proposal. No revised action is approved." : choice == "approve" ? "Preview result saved. No real action ran." : "You rejected the action."
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
                let artifact = Artifact(id: UUID().uuidString, botId: decision.botId, taskId: decision.taskId, title: option?.action?.filename ?? "payout-retry-fix.patch", kind: "file",
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
            replyTo: choice == "reply" ? decisionReference(for: decision.id) : nil,
            sendAt: undoSeconds > 0 ? Date().addingTimeInterval(undoSeconds) : nil)
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
                        if let sendAt = answer.sendAt, sendAt > Date() {
                            // The answer waits for its Undo window. Undo removes it, and the loop then finds nothing.
                            try? await Task.sleep(for: .seconds(sendAt.timeIntervalSinceNow))
                            continue
                        }
                        if !(await self.deliverAnswer(answer)) { break }
                    } else { break }
                }
            }
        }
    }

    private func deliverAnswer(_ answer: OutgoingAnswer) async -> Bool {
        let identity = deliveryID
        // The card changed while the answer waited, for example while the app was closed. The answer was for the old card.
        if let card = decisions.first(where: { $0.id == answer.decisionId }), card.revision != answer.request.revision {
            dropAnswer(answer, reason: Self.answerNotSent(card))
            return true
        }
        sendingAnswerIds.insert(answer.id)
        defer { sendingAnswerIds.remove(answer.id) }
        var rejected = false
        let recorded: Decision? = await write("decisions/\(answer.decisionId)/answer", payload: answer.request,
            as: Decision.self, quiet: true, refreshInBackground: true, idempotencyKey: answer.id,
            onFailure: { failure in
                if let serviceError = failure as? ServiceError,
                   case .response(let status, let reason) = serviceError,
                   (400..<500).contains(status), ![408, 429].contains(status) {
                    rejected = true
                    if identity == self.deliveryID {
                        let card = self.decisions.first { $0.id == answer.decisionId }
                        self.error = status == 409 && card != nil ? Self.answerNotSent(card!) : reason
                    }
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

    static func answerNotSent(_ card: Decision) -> String {
        "Your answer to \u{201C}\(card.title)\u{201D} was not sent, because the card changed. Answer it again."
    }

    private func dropAnswer(_ answer: OutgoingAnswer, reason: String) {
        outgoingAnswers.removeAll { $0.id == answer.id }
        answeringDecisionIds.remove(answer.decisionId)
        // As after a rejected answer, the typed reply comes back, so the person can send it again.
        if answer.request.choice == "reply", let text = answer.request.correction { decisionReplyDrafts[answer.decisionId] = text }
        try? saveOutgoing()
        error = reason
    }

    /// The answer of this card that waits for its Undo window, or nil.
    func waitingAnswer(for decisionId: String, at now: Date = Date()) -> OutgoingAnswer? {
        outgoingAnswers.first { $0.decisionId == decisionId && $0.isWaiting(at: now) && !sendingAnswerIds.contains($0.id) }
    }

    /// The latest answer that waits for its Undo window. Inbox offers Undo for it.
    func latestWaitingAnswer(at now: Date = Date()) -> OutgoingAnswer? {
        outgoingAnswers.filter { $0.isWaiting(at: now) && !sendingAnswerIds.contains($0.id) }
            .max { ($0.sendAt ?? .distantPast) < ($1.sendAt ?? .distantPast) }
    }

    /// Cancel an answer that has not been sent. Nothing reached the Mac, so the card is pending as before.
    @discardableResult
    func undoAnswer(decisionId: String) -> Bool {
        guard let answer = waitingAnswer(for: decisionId) else { return false }
        let previous = outgoingAnswers
        outgoingAnswers.removeAll { $0.id == answer.id }
        // Save the removal first. A saved answer that stayed in outgoing.json would send at the next launch.
        do { try saveOutgoing() } catch {
            outgoingAnswers = previous
            report(error)
            return false
        }
        answeringDecisionIds.remove(decisionId)
        if answer.request.choice == "reply", let text = answer.request.correction { decisionReplyDrafts[decisionId] = text }
        restoredDecisionId = decisionId
        return true
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
                                    text: "The fix changes the payout job and its spec. All 212 payout specs pass. Approval saves the patch locally; it does not deploy anything.",
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
        return optimistically("settings/model", change: { transaction in
            let resolved = modelCatalog?.resolved(selection) ?? selection
            var catalog = modelCatalog
            catalog?.defaults.model = selection.model; catalog?.defaults.provider = selection.provider
            let baseUrl = catalog?.entry(for: selection)?.baseUrl
            catalog?.defaults.baseUrl = baseUrl
            catalog?.defaults.reasoningEffort = resolved.reasoningEffort
            optimisticValue(\.model, selection.model, in: transaction)
            optimisticValue(\.modelBaseURL, catalog?.defaults.baseUrl ?? "", in: transaction)
            optimisticValue(\.modelCatalog, catalog, in: transaction)
            optimisticValue(\.modelConfigured, true, in: transaction)
            for bot in bots where bot.modelOverride == nil { optimisticBot(bot.id, \.effectiveModel, Optional(resolved), in: transaction) }
        }, save: { let _: ModelResponse? = await self.write("settings/model", method: "PUT", payload: selection, as: ModelResponse.self) })
    }

    func businessRequest<Response: Decodable & Sendable>(_ path: String, method: String = "GET", body: Data? = nil, requestId: String? = nil) async throws -> Response {
        guard let client else { throw ServiceError.configuration("Connect your Mac to continue.") }
        let generation = connectionGeneration
        let business = businessGeneration
        let response: Response = try await client.request(path, method: method, body: body, idempotencyKey: requestId, retryTransient: method != "POST")
        guard generation == connectionGeneration, business == businessGeneration else { throw CancellationError() }
        return response
    }

    func githubStatus() async throws -> GitHubConnectionStatus {
        if isDemo { return GitHubConnectionStatus(connected: false, login: nil, needsReconnect: false, pending: nil) }
        guard let client else { throw ServiceError.configuration("Connect your Mac to continue.") }
        return try await client.request("settings/github")
    }

    func githubRequest(_ action: String) async throws -> GitHubConnectionStatus {
        guard let client else { throw ServiceError.configuration("Connect your Mac to continue.") }
        let generation = connectionGeneration
        let response: GitHubConnectionStatus = try await client.request("settings/github/\(action)", method: "POST", body: APIJSON.encode(EmptyRequest()), retryTransient: true)
        guard generation == connectionGeneration else { throw CancellationError() }
        return response
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
        let automatic = workCapacity?.automaticLimit ?? 4
        optimistically("settings/work-capacity", change: { optimisticValue(\.workCapacity, Optional(ParallelWorkSettings(limit: value, effectiveLimit: value ?? automatic, automaticLimit: automatic)), in: $0) },
                       save: { let _: ParallelWorkSettings? = await self.write("settings/work-capacity", method: "PUT", payload: WorkerLimitRequest(limit: value, bot: false), as: ParallelWorkSettings.self) })
    }

    /// Replace the whole fallback chain of Hermes with `models`, in order.
    func setFallback(_ models: [FallbackModel]) async {
        if isDemo {
            fallback = FallbackSettings(models: models)
            return
        }
        optimistically("settings/fallback", change: { optimisticValue(\.fallback, Optional(FallbackSettings(models: models)), in: $0) },
                       save: { let _: FallbackSettings? = await self.write("settings/fallback", method: "PUT", payload: FallbackRequest(models: models), as: FallbackSettings.self) })
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
            optimistically("bot/\(botId)", change: { optimisticBot(botId, \.workerLimit, value, in: $0) },
                           save: { let _: Bot? = await self.write("bots/\(botId)", method: "PATCH", payload: WorkerLimitRequest(limit: value, bot: true), as: Bot.self) })
        } else {
            let automatic = parallelWork?.automaticLimit ?? 4
            optimistically("settings/parallel-work", change: { optimisticValue(\.parallelWork, Optional(ParallelWorkSettings(limit: value, effectiveLimit: value ?? automatic, automaticLimit: automatic)), in: $0) },
                           save: { let _: ParallelWorkSettings? = await self.write("settings/parallel-work", method: "PUT", payload: WorkerLimitRequest(limit: value, bot: false), as: ParallelWorkSettings.self) })
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
            optimistically("bot/\(botId)", change: { transaction in
                optimisticBot(botId, \.approvalOverride, value, in: transaction)
                optimisticBot(botId, \.effectiveApproval, value ?? approvals?.defaults, in: transaction)
            }, save: { let _: Bot? = await self.write("bots/\(botId)", method: "PATCH", payload: BotApprovalRequest(value: value), as: Bot.self) })
        } else if let value {
            optimistically("settings/approvals", change: { transaction in
                if let settings = approvals { optimisticValue(\.approvals, Optional(ApprovalSettings(defaults: value, modes: settings.modes)), in: transaction) }
                for bot in bots where bot.approvalOverride == nil { optimisticBot(bot.id, \.effectiveApproval, Optional(value), in: transaction) }
            }, save: { let _: ApprovalSettings? = await self.write("settings/approvals", method: "PUT", payload: ApprovalRequest(mode: value), as: ApprovalSettings.self) })
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
        return optimistically("bot/\(botId)", change: { transaction in
            optimisticBot(botId, \.modelOverride, selection, in: transaction)
            optimisticBot(botId, \.effectiveModel, selection.map { modelCatalog?.resolved($0) ?? $0 } ?? modelCatalog?.defaults.selection, in: transaction)
        }, save: { let _: Bot? = await self.write("bots/\(botId)", method: "PATCH", payload: BotModelRequest(modelOverride: selection), as: Bot.self) })
    }

    func uploadAttachment(bot: Bot, url: URL) async -> Artifact? {
        guard !isDemo else { error = "Attachments require a paired Mac. Preview data stays on this screen."; return nil }
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        do {
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard values.isRegularFile == true, let size = values.fileSize, size > 0, size <= TeamFileRules.largest else {
                throw ServiceError.configuration(TeamFileRules.sizeText)
            }
            let data = try Data(contentsOf: url, options: .mappedIfSafe)
            return await uploadAttachment(bot: bot, data: data, filename: url.lastPathComponent)
        } catch { report(error); return nil }
    }

    /// Attaches bytes without a file, for example an image that the person pasted.
    func uploadAttachment(bot: Bot, data: Data, filename: String) async -> Artifact? {
        guard !isDemo else { error = "Attachments require a paired Mac. Preview data stays on this screen."; return nil }
        guard !data.isEmpty, data.count <= 4 * 1024 * 1024 else {
            error = "Attachments must be between 1 byte and 4 MB."
            return nil
        }
        // Each upload has its own key, as for a file of a team chat: two equal pastes give two attachments.
        return await write("attachments", payload: AttachmentRequest(botId: bot.id, filename: filename,
                                                                     contentBase64: data.base64EncodedString()), as: Artifact.self,
                           idempotencyKey: UUID().uuidString)
    }

    func decisionPreviewData(id: String) async throws -> Data {
        guard let client else { throw ServiceError.configuration("Connect to your Mac to load the previews.") }
        return try await client.artifactContent(id: id)
    }

    /// The bytes of a photo that a bot saved on the Mac, or nil without a paired Mac.
    func macFileData(id: String) async -> Data? {
        guard let client else { return nil }
        return try? await client.artifactContent(id: id)
    }

    // MARK: Live activity

    /// A live panel of a task opened. The store keeps the feed of the task fresh until each panel of it closed.
    func watchActivity(_ taskId: String) {
        activityWatchers[taskId, default: 0] += 1
        Task { await loadActivity(taskId) }
    }

    func unwatchActivity(_ taskId: String) {
        let count = (activityWatchers[taskId] ?? 1) - 1
        activityWatchers[taskId] = count > 0 ? count : nil
        if count <= 0 { turnActivity[taskId] = nil }
    }

    /// Reads the feed of a task from the Mac. One read runs for a task at a time: a change during a read starts
    /// one more read after it, so a burst of tool calls costs two reads, not one for each call.
    func loadActivity(_ taskId: String) async {
        if isDemo {
            if turnActivity[taskId] == nil {
                let running = tasks.first { $0.id == taskId }?.isRunning == true
                turnActivity[taskId] = running ? .preview(taskId: taskId) : .empty(taskId)
            }
            return
        }
        guard let client else { return }
        if activityLoads[taskId] != nil {
            activityLoads[taskId] = true
            return
        }
        repeat {
            activityLoads[taskId] = false
            let feed: TurnActivity? = try? await client.request("tasks/\(taskId)/activity")
            guard self.client === client, activityWatchers[taskId] != nil else { break }
            if let feed, turnActivity[taskId]?.isReplaced(by: feed) ?? true { turnActivity[taskId] = feed }
        } while activityLoads[taskId] == true
        activityLoads[taskId] = nil
    }

    /// The updates carry no feed. They name the tasks whose feed changed, and an open live panel reads its feed
    /// again. A reset skips the events between the cursors, so each open panel reads again.
    private func reloadChangedActivity(_ page: StateUpdates) {
        guard !activityWatchers.isEmpty else { return }
        let changed = page.reset != nil ? Set(activityWatchers.keys)
            : Set(page.activityChanged ?? []).intersection(activityWatchers.keys)
        for taskId in changed { Task { await loadActivity(taskId) } }
    }

    /// An image of a feed. The bytes of an image never change, so the store keeps the decoded image.
    func activityImage(taskId: String, image: ActivityImage) async -> PlatformImage? {
        let key = (taskId + "/" + image.id) as NSString
        if let kept = activityImages.object(forKey: key) { return kept }
        guard let client, let data = try? await client.activityImage(taskId: taskId, imageId: image.id),
              let decoded = downsampledImage(data, largestSide: 1280) else { return nil }
        let result = PlatformImage(decoded)
        activityImages.setObject(result, forKey: key)
        return result
    }

    /// The start of a text file that a turn touched, as it is on the Mac now.
    func activityFilePreview(_ route: ActivityFileRoute) async throws -> ActivityFilePreview {
        if isDemo { return .preview(path: route.path) }
        guard let client else { throw ServiceError.configuration("Connect to your Mac to open this file.") }
        return try await client.request(APIClient.activityFilePath(taskId: route.taskId, file: route.path))
    }

    /// The bytes of a file of a message of a team chat, or nil without a paired Mac or for a file that is not there.
    func teamFileData(_ address: TeamFileAddress) async -> Data? {
        guard let client else { return nil }
        return try? await client.teamFileContent(address)
    }

    /// Keeps a picked file as a draft of the Mac for the next message of a team chat. An image fits the limit first
    /// (FileFit). Nil after a failure, with its text in the error banner: a file that does not fit, or the answer of
    /// the Mac, for example a full space of the team. The bytes stay on the phone, so a message on its way shows them.
    func uploadTeamFile(_ chat: TeamChatKey, _ picked: PickedFile) async -> TeamFile? {
        guard !chat.isDirect else { return nil }
        let outcome = await FileFit.fitted(picked)
        guard let fitted = outcome.file else {
            error = outcome.text
            return nil
        }
        if isDemo {
            let file = TeamFile(id: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                                name: fitted.name, type: fitted.type, size: fitted.data.count, width: fitted.width,
                                height: fitted.height)
            PhotoLoader.shared.keepDraft(fitted.data, id: file.id)
            return file
        }
        let request = TeamFileRequest(name: fitted.name, content: fitted.data.base64EncodedString(), type: fitted.type,
                                      width: fitted.width, height: fitted.height)
        // Each upload has its own key. An equal request without an answer would else lend its key, and the Mac would
        // answer the same draft for two picks of the same file, for example a double Command-V.
        guard let answer = await write("\(chat.path)/files", payload: request, as: TeamFileAnswer.self,
                                       idempotencyKey: UUID().uuidString) else { return nil }
        PhotoLoader.shared.keepDraft(fitted.data, id: answer.file.id)
        return answer.file
    }

    /// The person removed a draft: the Mac and the phone drop its bytes. A failure changes nothing that the person
    /// sees: the Mac drops an old draft after 24 hours.
    func dropTeamFile(_ chat: TeamChatKey, _ file: TeamFile) {
        PhotoLoader.shared.dropDraft(file.id)
        guard !isDemo, TeamFileAddress.isID(file.id) else { return }
        Task { _ = await write("\(chat.path)/files/\(file.id)", method: "DELETE", payload: EmptyRequest(),
                               as: TeamFileDropAnswer.self, quiet: true) }
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

    // The state of the Mac's Hermes, for example an untested version. Demo mode has none.
    // A quiet check passes reportsErrors: false, so a failure shows no error.
    func loadHermesSetup(reportsErrors: Bool = true) async -> HermesSetup? {
        if isDemo { return nil }
        guard let client else { return nil }
        do { return try await client.request("setup") }
        catch {
            if reportsErrors { report(error) }
            return nil
        }
    }

    func restartBots() async -> Bool {
        if isDemo { return true }
        let result: Acknowledgement? = await write("bots/restart", payload: EmptyRequest(), as: Acknowledgement.self)
        return result?.ok == true
    }

    /// Reads the usage of one chat. A failed read keeps what the store has and shows nothing: the footer is quiet.
    func loadChatUsage(_ botId: String) async {
        if isDemo {
            if chatUsage[botId] == nil { chatUsage[botId] = .preview(botId: botId) }
            return
        }
        guard let client else { return }
        guard let usage: ChatUsage = try? await client.request("bots/\(botId)/usage?days=30") else { return }
        guard self.client === client else { return }
        if chatUsage[botId] != usage { chatUsage[botId] = usage }
    }

    /// Reads the usage of the agents for the longest range of the usage page. A page that opens within a minute of the
    /// last read shows what the store has. A failed read shows no notice: the person did not ask for it.
    func loadUsage(force: Bool = false) async {
        if isDemo {
            if usage == nil { usage = UsageReport.preview(bots: bots) }
            return
        }
        guard let client else {
            usageIssue = "Connect to your Mac to see usage."
            return
        }
        if !force, let usageReadAt, Date().timeIntervalSince(usageReadAt) < 60 { return }
        do {
            let report: UsageReport = try await client.request("usage?days=\(UsageRange.quarter.days)")
            guard self.client === client else { return }
            usage = report
            usageIssue = nil
            usageReadAt = Date()
        } catch {
            guard self.client === client, usage == nil else { return }
            if case ServiceError.response(404, _) = error {
                usageIssue = "Your Mac has an older Coconut than this app. Update Coconut on your Mac to see usage."
            } else {
                usageIssue = "Coconut could not read the usage from your Mac."
            }
        }
    }

    /// Loads the Library list. True when the list is fresh from the Mac. A quiet load shows no notice when it fails:
    /// the person did not ask for it.
    @discardableResult
    func loadDocuments(quiet: Bool = false) async -> Bool {
        if isDemo {
            // The demo keeps its pins and deletes. A new load must not bring the first documents back.
            if documents == nil { documents = BotDocument.previews(bots: bots) }
            return true
        }
        guard let client else { return false }
        documentRequests += 1
        let request = documentRequests
        // The store runs on the main actor, so the task starts only after `latestDocumentLoad` names it.
        let load = Task { await fetchDocuments(client, request: request, quiet: quiet) }
        latestDocumentLoad = load
        return await load.value
    }

    /// One list request. A later request asks for a newer list, so the answer of an earlier request does not replace
    /// the list. Its caller gets the result of the latest load. That load is always a later request, so no load waits
    /// for itself, and a failed load gives false to each caller that waits for it.
    private func fetchDocuments(_ client: APIClient, request: Int, quiet: Bool) async -> Bool {
        let changes = documentChanges
        let response: DocumentList
        do { response = try await client.request("documents") } catch {
            // An error of a request of an earlier pairing has no message for the state of this pairing.
            guard self.client === client else { return false }
            if request != documentRequests { return await latestDocumentLoad?.value ?? false }
            if !quiet { report(error) }
            return false
        }
        // A list of the Mac of an earlier pairing is not the list of this pairing.
        guard self.client === client else { return false }
        if request != documentRequests { return await latestDocumentLoad?.value ?? false }
        // The Mac confirmed a pin or a delete during this request, so this list can be older. Ask again.
        guard changes == documentChanges else { return await loadDocuments(quiet: quiet) }
        teamBots = response.teamBots ?? []
        optimisticUpdates.receive { documents = response.documents }
        return true
    }

    /// The updates carry no documents. They say that a document or a team page changed, so a Library that the app
    /// loaded before loads its list again. A reset skips the events between the cursors, also the changes of
    /// documents. A Library that never loaded loads when it opens. The chats that quote a page read again with the
    /// changed chats (reloadChangedTeam, AppStore.teamChatsToRead).
    private func reloadChangedDocuments(_ page: StateUpdates) {
        // A Library whose first request runs also loads again: that request can be older than the change.
        guard page.documentsChanged == true || page.reset != nil, documents != nil || latestDocumentLoad != nil else { return }
        Task { await loadDocuments() }
    }

    // MARK: Team

    /// Loads the teams of the paired Mac. True when the list is fresh from the Mac. The same rules as the Library
    /// list: only the answer of the latest request replaces the list, and a list of an earlier pairing goes nowhere.
    @discardableResult
    func loadTeam() async -> Bool {
        if isDemo {
            if team == nil { team = TeamRooms(rooms: [], helper: true) }
            return true
        }
        guard let client else { return false }
        teamRequests += 1
        let request = teamRequests
        let load = Task { await fetchTeam(client, request: request) }
        latestTeamLoad = load
        return await load.value
    }

    private func fetchTeam(_ client: APIClient, request: Int) async -> Bool {
        let changes = teamChanges
        let response: TeamRooms
        do { response = try await client.request("team/rooms") } catch {
            guard self.client === client else { return false }
            if request != teamRequests { return await latestTeamLoad?.value ?? false }
            // A screen with a list keeps it. The banner of the connection says when the Mac is away.
            if !(error is CancellationError) { teamLoadError = Self.teamMessage(error) }
            return false
        }
        guard self.client === client else { return false }
        if request != teamRequests { return await latestTeamLoad?.value ?? false }
        // The Mac confirmed a change during this request, so this list can be older. Ask again.
        guard changes == teamChanges else { return await loadTeam() }
        // Each change of a chat loads the list. A list that changes nothing does not draw the Chats list again.
        let before = team
        optimisticUpdates.receive { assign(\.team, response) }
        teamListLoaded(before, response)
        assign(\.teamLoadError, nil)
        teamListVersion += 1
        assign(\.chatFilter, ChatFilter.effective(chatFilter, rooms: response.rooms))
        // A room that left the list takes its chats with it, also unsent messages that a restart brought back. So does
        // a named chat that the room no longer has.
        dropTeamChats(teamChats.keys.filter { !teamListHas($0) })
        return true
    }

    /// The text of a failed load. The text of the Mac when it sent one, else the text of a failed request.
    private static func teamMessage(_ error: Error) -> String {
        if let error = error as? ServiceError { return error.localizedDescription }
        if let error = error as? TunnelError, error == .needsRepair { return error.message }
        return "Couldn't complete this request. Try again."
    }

    /// A Team screen that loaded before loads again when a team changed. A reset of the updates skips the events
    /// between the cursors, also the team changes. A screen that never loaded loads when it opens.
    private func reloadChangedTeam(_ page: StateUpdates) {
        let chats = page.changedTeamChats
        // A reset skips the events between the cursors, also the changes of documents.
        if page.documentsChanged == true || page.reset != nil { teamDocumentChanges += 1 }
        reloadTeamChats(Self.teamChatsToRead(after: page, chats: teamChats))
        // The row of a team or a direct chat shows its newest message and its unread count, so a change of the chat
        // loads the list too. The list also says who types.
        let typing = !(page.teamTypingChanged ?? []).isEmpty
        guard page.teamChanged == true || page.reset != nil || !chats.isEmpty || typing, teamAsked else { return }
        Task { await loadTeam() }
    }

    /// Reads the newest page again of each of these chats that the app has, one time each.
    private func reloadTeamChats(_ chats: Set<TeamChatKey>) {
        for chat in chats where teamChats[chat] != nil {
            Task { await loadTeamChat(chat) }
            // The alerts of a choice of the filter change with the chat.
            if teamAlertFilters[chat] != nil { Task { await loadTeamAlerts(chat) } }
        }
    }

    /// The chats that read their newest page again for one update, each one time: each chat of a reset; else each
    /// changed chat that the app has, and after a change of the documents each chat with a team bot that quotes a
    /// page (teamChatsQuotingPages). A chat whose first request runs reads again in loadTeamChat. A change of the
    /// messages of a room names the room only, so it reads the Team chat and each named chat of the room.
    static func teamChatsToRead(after page: StateUpdates, chats: [TeamChatKey: TeamChat]) -> Set<TeamChatKey> {
        if page.reset != nil { return Set(chats.keys) }
        let changed = page.changedTeamChats
        var read = Set(chats.keys.filter { key in
            changed.contains(key) || (key.chat != nil && changed.contains(TeamChatKey(room: key.room)))
        })
        if page.documentsChanged == true { read.formUnion(teamChatsQuotingPages(chats)) }
        return read
    }

    /// The Team screen asked for the list: it has one, or its first request runs. A change during that first request
    /// starts a later one, so the earlier answer, made before the change, does not stay on screen.
    private var teamAsked: Bool { team != nil || latestTeamLoad != nil }

    /// Numbers a write of a room. Only the answer of the latest write of the room goes on screen.
    private func startTeamWrite(_ room: String) -> Int {
        teamWriteCount += 1
        teamWrites[room] = teamWriteCount
        return teamWriteCount
    }

    /// Puts the room of a confirmed change into the list, or removes it. Writes of one room can answer in another
    /// order than they started, so the answer of an earlier write does not go on screen, and the list loads again:
    /// the load keeps only the newest list, which has each write that the Mac confirmed.
    private func applyTeamChange(room: TeamRoom?, removing id: String? = nil, write: Int? = nil) {
        teamChanges += 1
        defer { Task { await loadTeam() } }
        if let room, let write, teamWrites[room.room] != write { return }
        optimisticUpdates.receive {
            var rooms = team ?? TeamRooms(rooms: [], helper: true)
            if let room {
                if let index = rooms.rooms.firstIndex(where: { $0.room == room.room }) { rooms.rooms[index] = room }
                else { rooms.rooms.append(room) }
        }
        if let id { rooms.rooms.removeAll { $0.room == id } }
        team = rooms
        teamLoadError = nil
        }

    }

    /// Makes a team. This Mac is its admin. Return whether the Mac made it.
    func createTeam(name: String, memberName: String) async -> Bool {
        let room: TeamRoomAnswer? = await write("team/rooms", payload: TeamCreateRequest(name: name, memberName: memberName),
                                                as: TeamRoomAnswer.self, refreshInBackground: true)
        guard let room else { return false }
        TeamMemberName.remember(memberName)
        applyTeamChange(room: room.room)
        return true
    }

    /// Joins a team with an invite link. Return the team, or nil when the Mac did not join.
    func joinTeam(link: String, memberName: String) async -> TeamRoom? {
        let link = link.trimmingCharacters(in: .whitespacesAndNewlines)
        let room: TeamRoomAnswer? = await write("team/join", payload: TeamJoinBody(link: link, memberName: memberName),
                                                as: TeamRoomAnswer.self, refreshInBackground: true)
        guard let room else {
            // A join that did not finish leaves a room that waits. The list shows it.
            await loadTeam()
            return nil
        }
        TeamMemberName.remember(memberName)
        applyTeamChange(room: room.room)
        return room.room
    }

    /// Changes the name that the team sees for this person.
    func renameTeamMember(_ room: TeamRoom, to memberName: String) async -> Bool {
        guard let member = room.ownMember?.id else { return false }
        return optimistically("team/\(room.room)", change: { transaction in
            optimisticUpdates.set("team/\(room.room)/member/\(member)/name", to: memberName, in: transaction,
                read: { self.team?.rooms.first { $0.room == room.room }?.members?.first { $0.id == member }?.name ?? "" },
                write: { value in
                    if let i = self.team?.rooms.firstIndex(where: { $0.room == room.room }), let j = self.team?.rooms[i].members?.firstIndex(where: { $0.id == member }) { self.team?.rooms[i].members?[j].name = value }
                })
        }, save: {
            let answer: TeamRoomAnswer? = await self.write("team/rooms/\(room.room)", method: "PATCH", payload: TeamProfileRequest(memberName: memberName), as: TeamRoomAnswer.self)
            if let answer { TeamMemberName.remember(memberName); self.applyTeamChange(room: answer.room) }
        })
    }

    /// Turns the notifications of a team on or off for the phones of this Mac. Return whether the Mac did it. The list
    /// then loads again, so a later write of the same room keeps the order of the Mac.
    func setTeamNotifications(_ room: String, enabled: Bool) async -> Bool {
        return optimistically("team/\(room)", change: { optimisticTeam(room, \.notifications, Optional(enabled), in: $0) }, save: {
            let answer: TeamNotificationsAnswer? = await self.write("team/rooms/\(room)/notifications", method: "PUT", payload: TeamNotificationsRequest(enabled: enabled), as: TeamNotificationsAnswer.self)
            self.optimisticUpdates.receive {
                if let answer, let index = self.team?.rooms.firstIndex(where: { $0.room == room }) { self.team?.rooms[index].notifications = answer.notifications }
            }
            self.teamChanges += 1
        })
    }

    /// Mutes or unmutes one named chat of a team for this person (TeamChatMute). Return whether the Mac did it.
    ///
    /// The choice is local immediately. Saves of the same team run in order.
    /// The answer changes only the choice of its own chat in the list as it is now, so a request of another chat or
    /// of the switch of the team does not drop it. The choice that waits goes only when this request ends: after the
    /// answer is in the list, or after a failure, which shows the old state again.
    @discardableResult
    func setTeamChatNotifications(_ room: String, chat: String, enabled: Bool) async -> Bool {
        let path = "team/rooms/\(room)/chats/\(chat)/notifications"
        return optimistically(path, change: { transaction in
            optimisticUpdates.set(path, to: Optional(enabled), in: transaction,
                read: { self.team?.rooms.first { $0.room == room }?.chats?.first { $0.id == chat }?.notifications },
                write: { value in
                    if let value, let i = self.team?.rooms.firstIndex(where: { $0.room == room }),
                       let j = self.team?.rooms[i].chats?.firstIndex(where: { $0.id == chat }) { self.team?.rooms[i].chats?[j].notifications = value }
                })
        }, save: {
            let answer: TeamChatNotificationsAnswer? = await self.write(path, method: "PUT", payload: TeamNotificationsRequest(enabled: enabled), as: TeamChatNotificationsAnswer.self)
            self.optimisticUpdates.receive {
                if let answer, let i = self.team?.rooms.firstIndex(where: { $0.room == room }) { self.team?.rooms[i] = TeamChatMute.applying([answer.chat: answer.notifications], to: self.team!.rooms[i]) }
            }
            self.teamChanges += 1
        })
    }

    /// Pins or unpins a chat of a team for this person (TeamChatPin): the Team chat when `chat` is nil, else a named
    /// chat. Return whether the change was queued. Each answer changes only that chat
    /// in the list as it is now, as for a mute.
    @discardableResult
    func setTeamChatPinned(_ room: String, chat: String?, pinned: Bool) async -> Bool {
        let path = "team/rooms/\(room)" + (chat.map { "/chats/\($0)" } ?? "") + "/pin"
        return optimistically(path, change: { transaction in
            optimisticUpdates.set(path, to: Optional(pinned), in: transaction,
                read: {
                    guard let value = self.team?.rooms.first(where: { $0.room == room }) else { return nil }
                    return chat.flatMap { id in value.chats?.first { $0.id == id }?.pinned } ?? (chat == nil ? value.pinned : nil)
                }, write: { value in
                    guard let value, let i = self.team?.rooms.firstIndex(where: { $0.room == room }) else { return }
                    if let chat, let j = self.team?.rooms[i].chats?.firstIndex(where: { $0.id == chat }) { self.team?.rooms[i].chats?[j].pinned = value }
                    else if chat == nil { self.team?.rooms[i].pinned = value }
                })
        }, save: {
            let answer: TeamChatPinAnswer? = await self.write(path, method: "PUT", payload: TeamPinRequest(pinned: pinned), as: TeamChatPinAnswer.self)
            self.optimisticUpdates.receive {
                guard let answer, let i = self.team?.rooms.firstIndex(where: { $0.room == room }) else { return }
                if let chat = answer.chat, let j = self.team?.rooms[i].chats?.firstIndex(where: { $0.id == chat }) { self.team?.rooms[i].chats?[j].pinned = answer.pinned }
                else if answer.chat == nil { self.team?.rooms[i].pinned = answer.pinned }
            }
            self.teamChanges += 1
        })
    }

    /// Shares a page of this Mac with a team, or stops (backend/coconut/team_pages.py). Return whether the Mac did it.
    ///
    /// The person can tap again before the Mac answers, and the answers can come in another order. So each act on a page
    /// has a number, and only the answer of the latest act goes into the list. The list then loads again, and the load
    /// keeps only the newest list, which has each act that the Mac confirmed.
    @discardableResult
    func sharePage(_ document: BotDocument, with room: String, shared: Bool) async -> Bool {
        let name = team?.rooms.first(where: { $0.room == room })?.name ?? room
        let path = "team/rooms/\(room)/pages/\(document.id)"
        documentChanges += 1
        return optimistically("document/\(document.id)", change: { transaction in
            optimisticUpdates.set("document/\(document.id)/share/\(room)", to: shared, in: transaction,
                read: { self.documents?.first { $0.id == document.id }?.teams?.contains { $0.room == room } ?? false },
                write: { value in
                    guard let i = self.documents?.firstIndex(where: { $0.id == document.id }) else { return }
                    self.documents?[i].teams?.removeAll { $0.room == room }
                    if value { self.documents?[i].teams = (self.documents?[i].teams ?? []) + [DocumentTeam(room: room, name: name)] }
                    self.documents?[i].audience = self.documents?[i].teams?.isEmpty == false ? "team" : nil
                })
        }, save: {
            let answer: PageShareAnswer? = await self.write(path, method: shared ? "PUT" : "DELETE", payload: EmptyRequest(), as: PageShareAnswer.self)
            self.optimisticUpdates.receive {
                if let answer, let i = self.documents?.firstIndex(where: { $0.id == document.id }) { self.documents?[i].teams = answer.document.teams; self.documents?[i].audience = answer.document.audience }
            }
            self.documentChanges += 1
        })
    }

    /// Asks the Mac for a new invite link. The link is a secret: the caller gives it to the share sheet and keeps no
    /// copy. The person shares the web form, which a mail or a chat keeps and which opens the app.
    func teamInviteLink(_ room: TeamRoom) async -> URL? {
        // Each tap makes a new link, so a request never reuses the key of an earlier one.
        let invite: TeamInvite? = await write("team/rooms/\(room.room)/invites", payload: EmptyRequest(),
                                              as: TeamInvite.self, refreshInBackground: true,
                                              idempotencyKey: UUID().uuidString)
        return invite.flatMap { TeamLog.webLink(fromLink: $0.link) }.flatMap { URL(string: $0) }
    }

    /// The admin takes another member out of a team (backend/coconut/team.py, `team_remove`). Return whether the Mac
    /// did it. The answer is the room without the member.
    func removeTeamMember(_ member: String, from room: TeamRoom) async -> Bool {
        return optimistically("team/\(room.room)", change: { transaction in
            optimisticUpdates.set("0:team/\(room.room)/member/\(member)", to: Optional<TeamMember>.none, in: transaction,
                read: { self.team?.rooms.first { $0.room == room.room }?.members?.first { $0.id == member } },
                write: { value in
                    guard let i = self.team?.rooms.firstIndex(where: { $0.room == room.room }) else { return }
                    self.team?.rooms[i].members?.removeAll { $0.id == member }
                    if let value { self.team?.rooms[i].members?.append(value) }
                })
        }, save: {
            let answer: TeamRoomAnswer? = await self.write("team/rooms/\(room.room)/members/\(member)", method: "DELETE", payload: EmptyRequest(), as: TeamRoomAnswer.self)
            if let answer { self.applyTeamChange(room: answer.room) }
        })
    }

    /// Makes a named chat of a team (docs/team-log.md, "Chats of a team"). Return the ID of the chat, or nil when the Mac
    /// did not make it. The error banner of the sheet then shows the text of the Mac.
    func createTeamChat(_ room: TeamRoom, name: String) async -> String? {
        await changeTeamChat(room, path: "team/rooms/\(room.room)/chats", method: "POST", name: name)
    }

    /// Gives a named chat a new name. Return whether the Mac did it.
    func renameTeamChat(_ room: TeamRoom, chat: String, name: String) async -> Bool {
        return optimistically("team/\(room.room)", change: { transaction in
            optimisticUpdates.set("team/\(room.room)/chat/\(chat)/name", to: name, in: transaction,
                read: { self.team?.rooms.first { $0.room == room.room }?.chats?.first { $0.id == chat }?.name ?? "" },
                write: { value in
                    if let i = self.team?.rooms.firstIndex(where: { $0.room == room.room }), let j = self.team?.rooms[i].chats?.firstIndex(where: { $0.id == chat }) { self.team?.rooms[i].chats?[j].name = value }
                })
        }, save: { _ = await self.changeTeamChat(room, path: "team/rooms/\(room.room)/chats/\(chat)", method: "PATCH", name: name) })
    }

    /// Deletes a named chat for each member. Return whether the Mac did it. The chat and its messages leave the app.
    func deleteTeamChat(_ room: TeamRoom, chat: String) async -> Bool {
        return optimistically("team/\(room.room)", change: { transaction in
            optimisticUpdates.set("0:team/\(room.room)/chat/\(chat)", to: Optional<TeamNamedChat>.none, in: transaction,
                read: { self.team?.rooms.first { $0.room == room.room }?.chats?.first { $0.id == chat } },
                write: { value in
                    guard let i = self.team?.rooms.firstIndex(where: { $0.room == room.room }) else { return }
                    self.team?.rooms[i].chats?.removeAll { $0.id == chat }
                    if let value { self.team?.rooms[i].chats?.append(value) }
                })
        }, save: { _ = await self.changeTeamChat(room, path: "team/rooms/\(room.room)/chats/\(chat)", method: "DELETE", name: nil) })
    }

    /// One write of the named chats of a room. The answer is the room with its chats, and the list loads again.
    private func changeTeamChat(_ room: TeamRoom, path: String, method: String, name: String?) async -> String? {
        let ticket = startTeamWrite(room.room)
        let answer: TeamChatAnswer? = await write(path, method: method, payload: TeamChatRequest(name: name),
                                                  as: TeamChatAnswer.self, refreshInBackground: true)
        guard let answer else { return nil }
        applyTeamChange(room: answer.room, write: ticket)
        // A deleted chat leaves the list of the answer, so its messages and its unsent messages go.
        dropTeamChats(teamChats.keys.filter { !teamListHas($0) })
        return answer.chat
    }

    /// Posts a notice on the board of a team (docs/team-log.md, "Notices"). Return whether the Mac posted it.
    func postTeamNotice(_ room: TeamRoom, kind: NoticeKind, text: String, endsAt: Date?) async -> Bool {
        await changeTeamNotices(room, path: "team/rooms/\(room.room)/notices", method: "POST",
                                payload: TeamNoticeRequest(type: kind.rawValue, text: text,
                                                           endsAt: endsAt.map(Self.noticeTime)),
                                key: UUID().uuidString)
    }

    /// Closes an own notice for each member. It stays on the board as ended.
    func closeTeamNotice(_ room: TeamRoom, notice: WorkspaceNotice) async -> Bool {
        optimistically("team/\(room.room)", change: { transaction in
            optimisticNotice(room.room, notice.id, \.closed, true, in: transaction)
            optimisticNotice(room.room, notice.id, \.active, false, in: transaction)
        }, save: { _ = await self.changeTeamNotices(room, path: "team/rooms/\(room.room)/notices/\(notice.id)", method: "PATCH", payload: TeamNoticeChange(closed: true)) })
    }

    /// Removes an own notice from the board of each member.
    func deleteTeamNotice(_ room: TeamRoom, notice: WorkspaceNotice) async -> Bool {
        optimistically("team/\(room.room)", change: { transaction in
            optimisticUpdates.set("0:team/\(room.room)/notice/\(notice.id)", to: Optional<WorkspaceNotice>.none, in: transaction,
                read: { self.team?.rooms.first { $0.room == room.room }?.notices?.first { $0.id == notice.id } },
                write: { value in
                    guard let i = self.team?.rooms.firstIndex(where: { $0.room == room.room }) else { return }
                    self.team?.rooms[i].notices?.removeAll { $0.id == notice.id }
                    if let value { self.team?.rooms[i].notices?.append(value) }
                })
        }, save: { _ = await self.changeTeamNotices(room, path: "team/rooms/\(room.room)/notices/\(notice.id)", method: "DELETE", payload: EmptyRequest()) })
    }

    /// Tells the team that this person has seen these notices. A notice that the person has seen writes nothing.
    func markTeamNoticesSeen(_ room: TeamRoom, notices: [WorkspaceNotice]) async {
        let ids = notices.filter { !$0.seen }.map(\.id)
        guard !ids.isEmpty, !isDemo else { return }
        optimistically("team/\(room.room)", change: { transaction in
            for id in ids { optimisticNotice(room.room, id, \.seen, true, in: transaction) }
        }, save: { _ = await self.changeTeamNotices(room, path: "team/rooms/\(room.room)/notices/seen", method: "POST", payload: TeamNoticesSeen(notices: ids), quiet: true) })
    }

    private func changeTeamNotices<Payload: Encodable>(_ room: TeamRoom, path: String, method: String,
                                                       payload: Payload, key: String? = nil,
                                                       quiet: Bool = false) async -> Bool {
        let ticket = startTeamWrite(room.room)
        let answer: TeamRoomAnswer? = await write(path, method: method, payload: payload, as: TeamRoomAnswer.self,
                                                  quiet: quiet, refreshInBackground: true, idempotencyKey: key)
        guard let answer else { return false }
        applyTeamChange(room: answer.room, write: ticket)
        return true
    }

    nonisolated static func noticeTime(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    /// Leaves a team, or removes a team that this Mac is no longer in. The Mac deletes its copy and its keys.
    func leaveTeam(_ room: TeamRoom) async -> Bool {
        // An answer of an earlier write of the room must not bring the room back.
        _ = startTeamWrite(room.room)
        let answer: TeamLeaveAnswer? = await write("team/rooms/\(room.room)", method: "DELETE", payload: EmptyRequest(),
                                                   as: TeamLeaveAnswer.self, refreshInBackground: true)
        guard answer != nil else { return false }
        applyTeamChange(room: nil, removing: room.room)
        // The Mac deleted its copy, so the chats have no history to show.
        dropTeamChats(teamChats.keys.filter { $0.room == room.room })
        return true
    }

    /// Removes the chats of rooms that the app no longer has: their messages, their unsent messages, and each request
    /// that is on its way, so a late answer finds no place to go.
    private func dropTeamChats(_ chats: [TeamChatKey]) {
        let pending = chats.contains { teamChats[$0]?.pending.isEmpty == false }
        for chat in chats {
            teamChats[chat] = nil
            teamChatRequests[chat] = nil
            teamOlderRequests[chat] = nil
            teamMessageChanges[chat] = nil
            teamReactionWriters[chat] = nil
            teamReadPosted[chat] = nil
            teamReadPosting[chat] = nil
            teamChatsOnScreen.remove(chat)
            dropTeamAlertFilter(chat)
        }
        if pending { try? saveOutgoing() }
    }

    /// The newest message in the app of each direct chat that has one: an unsent message, or a saved message. The
    /// Chats list shows a direct chat that the list of the Mac does not have yet with it (TeamRoom.listedDirects).
    var newestLocalMessages: [TeamChatKey: TeamLastMessage] {
        var newest: [TeamChatKey: TeamLastMessage] = [:]
        for (chat, value) in teamChats where chat.isDirect {
            if let last = value.newestLocal { newest[chat] = last }
        }
        return newest
    }

    /// True while the list of the teams has the chat: its room, and for a named chat that chat. A list that the app has
    /// not loaded yet keeps every chat. A direct chat stays while its room stays, also after the other member left.
    private func teamListHas(_ chat: TeamChatKey) -> Bool {
        team.map { $0.has(chat) } ?? true
    }

    // MARK: Team chat

    /// Reads the newest page of the chat of a team and merges it by message ID. True when the page is fresh from the
    /// Mac. Only the answer of the latest request goes into the chat, and a page of an earlier pairing goes nowhere.
    @discardableResult
    func loadTeamChat(_ chat: TeamChatKey) async -> Bool {
        if isDemo {
            if teamChats[chat] == nil { teamChats[chat] = TeamChat(loaded: true) }
            return true
        }
        guard let client else { return false }
        for attempt in 0..<3 {
            teamChatRequestCount += 1
            let request = teamChatRequestCount
            teamChatRequests[chat] = request
            let changes = teamChatChanges[chat, default: 0]
            let documents = teamDocumentChanges
            let page: TeamMessagePage
            do { page = try await client.request("\(chat.path)/messages?limit=\(Self.teamPage)\(chat.query)") } catch {
                guard self.client === client, teamChatRequests[chat] == request, teamListHas(chat) else { return false }
                if !(error is CancellationError) { teamChats[chat, default: TeamChat()].error = Self.teamMessage(error) }
                return false
            }
            // A removal of the room drops its request, so a late page cannot make its history again.
            guard self.client === client, teamChatRequests[chat] == request, teamListHas(chat) else { return false }
            // The Mac confirmed a message, an edit or a removal during this request, so the page can be older. Ask again.
            guard changes == teamChatChanges[chat, default: 0] else { continue }
            // The update reads only the chats that the app has, so it does not read a chat whose first request runs.
            guard attempt == 2 || !isOlderThanDocumentChange(page, since: documents) else { continue }
            var merged = teamChats[chat] ?? TeamChat()
            // A page that starts the loaded range again makes each read of the old range out of date.
            if merged.merge(page, requested: nil) { teamOlderRequests[chat] = nil }
            merged.error = nil
            // A page that changes nothing does not draw the chat again.
            if teamChats[chat] != merged { teamChats[chat] = merged }
            // An edit or a removal can be in an older page that the person loaded. A chat on screen reads them again.
            if teamChatsOnScreen.contains(chat), merged.hasOlderPages { await reloadOlderTeamMessages(chat, using: client) }
            return true
        }
        return false
    }

    /// True when a document changed after the request of this page started (`documents` is teamDocumentChanges at
    /// the start), and a message of the page quotes a page. That page can be gone, so the caller asks again. In a loop
    /// of 3 tries, the last try takes its answer: the loaded chat then quotes a page, so the next update with a
    /// change of the documents reads it again (teamChatsQuotingPages), and the chat does not stay without messages.
    private func isOlderThanDocumentChange(_ page: TeamMessagePage, since documents: Int) -> Bool {
        documents != teamDocumentChanges && page.messages.contains { $0.page != nil }
    }

    /// The most messages of one request that reads loaded older pages again.
    static let teamRereadPage = 100

    /// Reads the older pages that a chat on screen has again, from the newest page down to the oldest loaded message,
    /// in requests of up to 100. Only the latest pass merges, and nothing under the loaded range comes in.
    private func reloadOlderTeamMessages(_ chat: TeamChatKey, using client: APIClient) async {
        teamChatRequestCount += 1
        let pass = teamChatRequestCount
        teamOlderRequests[chat] = pass
        guard var upper = teamChats[chat]?.newestFloor else { return }
        // The oldest loaded message. A chat that has its oldest message has no `before`, and each `first` is over 0.
        let floor = teamChats[chat]?.before ?? 0
        while true {
            let changes = teamChatChanges[chat, default: 0]
            let documents = teamDocumentChanges
            let page: TeamMessagePage
            do {
                page = try await client.request(
                    "\(chat.path)/messages?before=\(upper)&limit=\(Self.teamRereadPage)\(chat.query)")
            } catch { return }
            guard self.client === client, teamOlderRequests[chat] == pass, teamListHas(chat),
                  teamChats[chat] != nil else { return }
            // A confirmed write can make this page older. The next change reads it again.
            guard changes == teamChatChanges[chat, default: 0] else { return }
            // After a change of the documents, no next read comes for a chat whose loaded messages quote no page. So
            // ask for the same range again. Only an update changes the count, and a read gives no update: each new
            // request needs a new update, so the reads cannot loop.
            guard !isOlderThanDocumentChange(page, since: documents) else { continue }
            teamChats[chat]?.merge(page, requested: upper, floor: floor)
            guard let next = page.before, next > floor else { return }
            upper = next
        }
    }

    /// Numbers a local change of a message. The answer of an earlier change of the same message is then out of date.
    private func startMessageChange(_ chat: TeamChatKey, _ id: String) -> Int {
        teamMessageChangeCount += 1
        teamMessageChanges[chat, default: [:]][id] = teamMessageChangeCount
        return teamMessageChangeCount
    }

    /// The change is still the newest local change of the message, and the room is still in the list.
    private func isNewestMessageChange(_ chat: TeamChatKey, _ id: String, _ change: Int) -> Bool {
        teamMessageChanges[chat]?[id] == change && teamListHas(chat)
    }

    /// "Chat about this page": the page waits for the chat of the target, which takes it when it opens.
    func ask(about document: BotDocument, in target: DocumentChatTarget) {
        switch target {
        case .bot(let id): pageQuotes[id] = .page(document)
        case .team(let chat): teamPageQuotes[chat] = TeamAboutPage(document)
        }
    }

    /// Takes the page that the chat of `chat` quotes when it opens ("Chat about this page"). Only the chat of that key
    /// gets it, only one time, and only while the chat is on the screen: a chat that the person does not see leaves it
    /// for the chat that the person sees. A chat where the person cannot write takes the page and drops it.
    func takeTeamPageQuote(_ chat: TeamChatKey, onScreen: Bool, canWrite: Bool) -> TeamAboutPage? {
        guard onScreen, let page = teamPageQuotes.removeValue(forKey: chat) else { return nil }
        return canWrite ? page : nil
    }

    /// Sends the draft of a composer with its reply or its page and its files (sendTeamMessage). False when the store
    /// did not save it, so the composer keeps the draft.
    @discardableResult
    func sendTeamMessage(_ chat: TeamChatKey, from composer: TeamComposerState, mentions: [String] = []) -> Bool {
        sendTeamMessage(chat, text: composer.draft, replyingTo: composer.quote?.reply, page: composer.quote?.page,
                        mentions: mentions, preview: composer.editing == nil ? composer.linkPreview : nil,
                        files: composer.uploading == 0 ? composer.files : [])
    }

    /// The loaded direct chats with a team bot that quote a page, each one time. After an update that says that a
    /// document changed, each reads its messages again, so the quote of a page that went says so and opens nothing.
    /// A page that went can come back with the same ID, so a chat whose pages all went also reads again. Only an
    /// update starts these reads, and a read of a chat gives no update: the reads cannot loop.
    nonisolated static func teamChatsQuotingPages(_ chats: [TeamChatKey: TeamChat]) -> [TeamChatKey] {
        chats.compactMap { key, chat in
            key.bot != nil && chat.loaded && chat.messages.contains { $0.page != nil } ? key : nil
        }
        .sorted { ($0.room, $0.bot ?? "") < ($1.room, $1.bot ?? "") }
    }

    /// The chat of a room is on screen. It loads its newest page when it opens.
    func openTeamChat(_ chat: TeamChatKey) {
        teamChatsOnScreen.insert(chat)
    }

    /// The chat is on the screen: it appeared and did not disappear since.
    func isTeamChatOnScreen(_ chat: TeamChatKey) -> Bool { teamChatsOnScreen.contains(chat) }

    /// The chat of a room leaves the screen. It keeps its newest page only, so it opens again without stale history.
    func closeTeamChat(_ chat: TeamChatKey) {
        teamChatsOnScreen.remove(chat)
        teamOlderRequests[chat] = nil
        teamChats[chat]?.keepNewestPage()
        // A chat opens with each message, so a message of a notification is never behind a filter.
        dropTeamAlertFilter(chat)
    }

    // MARK: Alerts of a team chat

    /// The filter that a chat uses: the choice of the person while the chat offers it, else "All".
    func teamAlertFilter(_ chat: TeamChatKey) -> TeamAlertFilter {
        TeamAlertFilters.effective(teamAlertFilters[chat] ?? .all, summary: teamChats[chat]?.alerts)
    }

    /// The person chose a filter of the alerts of a chat. The chat shows the alerts of the choice that the app has at
    /// once, and reads the newest page of the choice from the Mac. The choice changes no read position and goes to no
    /// Mac.
    func setTeamAlertFilter(_ chat: TeamChatKey, _ filter: TeamAlertFilter) {
        guard filter != teamAlertFilters[chat] ?? .all else { return }
        guard !filter.isAll else { return dropTeamAlertFilter(chat) }
        teamAlertRequests[chat] = nil
        teamAlertFilters[chat] = filter
        var start = TeamAlertFilters.start(from: teamChats[chat], filter: filter)
        // The preview has no Mac to ask.
        if isDemo { start.loaded = true }
        teamAlertChats[chat] = start
        Task { await loadTeamAlerts(chat) }
    }

    private func dropTeamAlertFilter(_ chat: TeamChatKey) {
        teamAlertRequests[chat] = nil
        if teamAlertFilters[chat] != nil { teamAlertFilters[chat] = nil }
        if teamAlertChats[chat] != nil { teamAlertChats[chat] = nil }
    }

    /// Reads the newest page of the alerts of the choice of a chat. Only the answer of the latest request of the
    /// same choice goes into the chat.
    @discardableResult
    func loadTeamAlerts(_ chat: TeamChatKey) async -> Bool {
        guard !isDemo, let client, let filter = teamAlertFilters[chat] else { return false }
        teamChatRequestCount += 1
        let request = teamChatRequestCount
        teamAlertRequests[chat] = request
        let page: TeamMessagePage
        do {
            page = try await client.request("\(chat.path)/messages?limit=\(Self.teamPage)\(chat.query)\(filter.query)")
        } catch {
            guard self.client === client, teamAlertRequests[chat] == request else { return false }
            if !(error is CancellationError) { teamAlertChats[chat]?.error = Self.teamMessage(error) }
            return false
        }
        guard self.client === client, teamAlertRequests[chat] == request, teamAlertFilters[chat] == filter,
              teamListHas(chat) else { return false }
        var merged = teamAlertChats[chat] ?? TeamChat()
        merged.merge(page, requested: nil)
        merged.error = nil
        if teamAlertChats[chat] != merged { teamAlertChats[chat] = merged }
        return true
    }

    /// Reads the next older page of the alerts of the choice of a chat.
    @discardableResult
    func loadEarlierTeamAlerts(_ chat: TeamChatKey) async -> Bool {
        guard !isDemo, let client, let filter = teamAlertFilters[chat], let before = teamAlertChats[chat]?.before,
              teamAlertChats[chat]?.loadingEarlier == false else { return false }
        teamAlertChats[chat]?.loadingEarlier = true
        defer { if self.client === client { teamAlertChats[chat]?.loadingEarlier = false } }
        let page: TeamMessagePage
        do {
            page = try await client.request(
                "\(chat.path)/messages?before=\(before)&limit=\(Self.teamPage)\(chat.query)\(filter.query)")
        } catch {
            guard self.client === client else { return false }
            if !(error is CancellationError) { report(error) }
            return false
        }
        // A new choice, or a newer page that started the list again, leaves this page without a place.
        guard self.client === client, teamAlertFilters[chat] == filter, teamAlertChats[chat]?.before == before else {
            return false
        }
        teamAlertChats[chat]?.merge(page, requested: before)
        return true
    }

    /// The messages of one page. The chat reads the newest page first, and one more for each "Load earlier messages".
    static let teamPage = 50

    /// Reads the next older page of a chat. The chat shows a failure in the error banner and keeps its messages.
    @discardableResult
    func loadEarlierTeamMessages(_ chat: TeamChatKey) async -> Bool {
        guard !isDemo, let client, let before = teamChats[chat]?.before, teamChats[chat]?.loadingEarlier == false else {
            return false
        }
        teamChats[chat]?.loadingEarlier = true
        defer { if self.client === client { teamChats[chat]?.loadingEarlier = false } }
        for attempt in 0..<3 {
            let changes = teamChatChanges[chat, default: 0]
            let documents = teamDocumentChanges
            let page: TeamMessagePage
            do {
                page = try await client.request("\(chat.path)/messages?before=\(before)&limit=\(Self.teamPage)\(chat.query)")
            }
            catch {
                guard self.client === client else { return false }
                if !(error is CancellationError) { report(error) }
                return false
            }
            // A newer page can have started the chat again, or the room went. This page then has no place in it.
            guard self.client === client, teamChats[chat]?.before == before, teamListHas(chat),
                  teamChatsOnScreen.contains(chat) else { return false }
            guard changes == teamChatChanges[chat, default: 0] else { continue }
            // An update reads only the chats whose loaded messages quote a page, so it does not read this page.
            guard attempt == 2 || !isOlderThanDocumentChange(page, since: documents) else { continue }
            teamChats[chat]?.merge(page, requested: before)
            return true
        }
        return false
    }

    /// Shows a message of this person at once, and sends it. A failure keeps it in the chat with a way to try again.
    /// The message is saved before the composer clears, so a restart keeps it. False when the save failed.
    /// `mentions` are the IDs of the team bots and the people that the text names (TeamMentions.ids). More than the
    /// Mac takes is false too: the app saves nothing, the composer keeps the draft, and the error banner says why.
    /// `page` is the Library page that the message is about. Only a direct chat with a bot takes it, and then the
    /// message answers no other message.
    /// `preview` is the link preview of the composer. It goes only to a Mac with previews (TeamChat.offersPreviews),
    /// and only when it fits the text (TeamLinkPreview.fitting). Else the message goes without it.
    /// `files` are the drafts of the composer on the Mac. With them the text can be empty. A direct chat takes none.
    @discardableResult
    func sendTeamMessage(_ chat: TeamChatKey, text: String, replyingTo answered: TeamMessage?, page: TeamAboutPage? = nil,
                         mentions: [String] = [], preview: TeamLinkPreview? = nil, files: [TeamFile] = []) -> Bool {
        let page = chat.bot == nil ? nil : page
        let answered = page == nil ? answered : nil
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let preview = teamChats[chat]?.offersPreviews == true ? preview?.fitting(in: text) : nil
        let files = chat.isDirect ? [] : Array(files.prefix(TeamFileRules.limit))
        guard !text.isEmpty || !files.isEmpty, teamListHas(chat) else { return false }
        guard TeamMentions.fits(mentions) else {
            error = TeamMentions.overLimit
            return false
        }
        let quote = answered.map {
            TeamMessageQuote(id: $0.id, thisMac: $0.thisMac, member: $0.member, memberName: $0.memberName, bot: $0.bot,
                             botName: $0.botName, text: String($0.shortText.prefix(200)))
        }
        let pending = TeamPendingMessage(id: UUID().uuidString, text: text, replyTo: answered?.id, reply: quote,
                                         createdAt: Self.now, mentions: mentions.isEmpty ? nil : mentions, page: page,
                                         preview: preview, files: files.isEmpty ? nil : files)
        teamChats[chat, default: TeamChat()].pending.append(pending)
        do { try saveOutgoing() } catch {
            teamChats[chat]?.pending.removeAll { $0.id == pending.id }
            report(error)
            return false
        }
        Task { await deliverTeamMessage(chat, id: pending.id) }
        return true
    }

    /// The Mac confirmed a message with files: the bytes of each draft on the phone become the bytes of the file of
    /// the message, so the chat shows them without a download.
    private func adoptTeamDrafts(_ pending: TeamPendingMessage, as message: TeamMessage, in chat: TeamChatKey) {
        for file in pending.files ?? [] where message.files.contains(where: { $0.id == file.id }) {
            if let address = TeamFileAddress(room: chat.room, message: message.id, file: file.id) {
                PhotoLoader.shared.adoptDraft(file.id, as: address)
            }
        }
    }

    /// The answer of the Mac to a link preview that does not fit its rules. The Mac then saves nothing.
    static let previewRefused = "The link preview does not fit the rules of a preview, so send the message without it."

    /// Sends a message that did not send again, with the same key.
    func retryTeamMessage(_ chat: TeamChatKey, id: String) {
        guard let index = teamChats[chat]?.pending.firstIndex(where: { $0.id == id && $0.failed }),
              let pending = teamChats[chat]?.pending[index] else { return }
        teamChats[chat]?.pending[index] = pending.retried
        try? saveOutgoing()
        Task { await deliverTeamMessage(chat, id: id) }
    }

    /// Removes a message that did not send. The Mac never confirmed it, so nothing else changes. The saved set loses it
    /// first, so a restart does not bring it back.
    func removeUnsentTeamMessage(_ chat: TeamChatKey, id: String) {
        guard let previous = teamChats[chat]?.pending else { return }
        let removed = previous.first { $0.id == id && $0.failed }
        teamChats[chat]?.pending.removeAll { $0.id == id && $0.failed }
        do { try saveOutgoing() } catch {
            teamChats[chat]?.pending = previous
            report(error)
            return
        }
        // The drafts of the message go too, on the Mac and on the phone.
        for file in removed?.files ?? [] { dropTeamFile(chat, file) }
    }

    private func deliverTeamMessage(_ chat: TeamChatKey, id: String) async {
        guard let pending = teamChats[chat]?.pending.first(where: { $0.id == id }) else { return }
        if isDemo {
            let first = (teamChats[chat]?.newestFirst ?? 0) + 1
            let files = (pending.files ?? []).map { file in var file = file; file.complete = true; return file }
            let message = TeamMessage(id: id, first: first, thisMac: true, text: pending.text, replyTo: pending.replyTo,
                                      reply: pending.reply, mentions: pending.mentions,
                                      createdAt: pending.createdAt, page: pending.page,
                                      preview: pending.preview, files: files, filesKey: true)
            adoptTeamDrafts(pending, as: message, in: chat)
            teamChats[chat]?.confirm(id, as: message)
            return
        }
        let change = startMessageChange(chat, id)
        var refusedPreview = false
        let answer: TeamMessageAnswer? = await write("\(chat.path)/messages",
                                                     payload: TeamSendRequest(text: pending.text, replyTo: pending.replyTo,
                                                                              mentions: pending.mentions, chat: chat.chat,
                                                                              aboutDocumentId: pending.page?.id,
                                                                              preview: pending.preview,
                                                                              files: pending.files?.map(\.id)),
                                                     as: TeamMessageAnswer.self, refreshInBackground: true,
                                                     idempotencyKey: pending.idempotencyKey,
                                                     onFailure: { error in
                                                         if let error = error as? ServiceError,
                                                            case .response(400, AppStore.previewRefused) = error {
                                                             refusedPreview = true
                                                         }
                                                     })
        // A new pairing or a removal of the room removed the message, or a newer try of it runs.
        guard isNewestMessageChange(chat, id, change),
              let index = teamChats[chat]?.pending.firstIndex(where: { $0.id == id }) else { return }
        if answer == nil, refusedPreview, pending.preview != nil {
            // The Mac refused the preview and saved nothing. The message goes one time again without it, with the
            // same key, so a person never loses a message because of a preview.
            teamChats[chat]?.pending[index].preview = nil
            try? saveOutgoing()
            if error == AppStore.previewRefused { error = nil }
            await deliverTeamMessage(chat, id: id)
            return
        }
        guard let answer else {
            teamChats[chat]?.pending[index].failed = true
            try? saveOutgoing()
            return
        }
        teamChatChanges[chat, default: 0] += 1
        adoptTeamDrafts(pending, as: answer.message, in: chat)
        // A page that brought the message first is as new as this answer or newer, so its copy stays: a late answer
        // does not take away a reaction or bring back a page that went.
        teamChats[chat]?.confirm(id, as: answer.message)
        try? saveOutgoing()
    }

    /// Changes the text of a message of this person. The chat shows the new text while the Mac writes it. True when the
    /// Mac confirmed it.
    func editTeamMessage(_ chat: TeamChatKey, _ message: TeamMessage, text: String) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // A message with files takes an empty text (docs/team-log.md, "Files").
        guard !text.isEmpty || !message.files.isEmpty else { return false }
        guard text != message.text else { return true }
        if isDemo {
            var edited = message
            edited.text = text
            edited.editedAt = Self.now
            teamChats[chat]?.put(edited)
            return true
        }
        let change = startMessageChange(chat, message.id)
        teamChats[chat]?.editing[message.id] = text
        let answer: TeamMessageAnswer? = await write("\(chat.path)/messages/\(message.id)", method: "PATCH",
                                                     payload: TeamEditRequest(text: text), as: TeamMessageAnswer.self,
                                                     refreshInBackground: true)
        // A newer edit or a removal of the same message wins over this answer: it does not put back the message or
        // an older text.
        guard isNewestMessageChange(chat, message.id, change) else {
            // The Mac made the change, so a page that the app asked for before it is out of date.
            if answer != nil { teamChatChanges[chat, default: 0] += 1 }
            return answer != nil
        }
        teamChats[chat]?.editing[message.id] = nil
        guard let answer else { return false }
        teamChatChanges[chat, default: 0] += 1
        teamChats[chat]?.putEdit(answer.message)
        return true
    }

    /// Removes a message of this person for each member. The message hides while the Mac writes the removal, and comes
    /// back when the Mac does not confirm it.
    func deleteTeamMessage(_ chat: TeamChatKey, _ message: TeamMessage) async -> Bool {
        if isDemo {
            teamChats[chat]?.remove(message.id)
            return true
        }
        let change = startMessageChange(chat, message.id)
        // An edit of the message that is on its way no longer shows.
        teamChats[chat]?.editing[message.id] = nil
        teamChats[chat]?.deleting.insert(message.id)
        let answer: TeamDeleteAnswer? = await write("\(chat.path)/messages/\(message.id)", method: "DELETE",
                                                    payload: EmptyRequest(), as: TeamDeleteAnswer.self,
                                                    refreshInBackground: true)
        guard isNewestMessageChange(chat, message.id, change) else {
            // The Mac made the change, so a page that the app asked for before it is out of date.
            if answer != nil { teamChatChanges[chat, default: 0] += 1 }
            return answer != nil
        }
        teamChats[chat]?.deleting.remove(message.id)
        guard answer != nil else { return false }
        teamChatChanges[chat, default: 0] += 1
        teamChats[chat]?.remove(message.id)
        return true
    }

    /// Sets the reaction that this person wants on a message to `emoji`, or takes it back when the person tapped the
    /// emoji that the person has (TeamReactions.value). `message` is the message as the chat shows it. The chat shows
    /// the wanted reaction at once, and the call returns at once. One writer for each message sends it to the Mac
    /// (writeTeamReaction). False when the chat offers no reaction on the message.
    @discardableResult
    func reactToTeamMessage(_ chat: TeamChatKey, _ message: TeamMessage, emoji: String) -> Bool {
        guard TeamReactions.all.contains(emoji), message.reactions != nil, chat.bot == nil,
              teamChats[chat]?.deleting.contains(message.id) == false,
              let member = team?.rooms.first(where: { $0.room == chat.room })?.ownMember?.member else { return false }
        let value = TeamReactions.value(tapping: emoji, on: message)
        if isDemo {
            teamChats[chat]?.put(message.reacting(value, as: member))
            return true
        }
        teamChats[chat]?.reacting[message.id] = TeamOwnReaction(emoji: value, member: member)
        // A running writer sends the new value when its write ends.
        guard teamReactionWriters[chat]?[message.id] == nil else { return true }
        teamReactionWriterCount += 1
        let writer = teamReactionWriterCount
        teamReactionWriters[chat, default: [:]][message.id] = writer
        Task { await writeTeamReaction(chat, message.id, writer: writer) }
        return true
    }

    /// The one writer of the reaction of this person on a message. It sends the wanted value, and after each answer
    /// the value that the person wants now, until the Mac has it. So the Mac gets the choices in their order, and the
    /// last choice is the last write. Each write has its own key, so a write with the body of an earlier write does
    /// not get the stored answer of that write.
    ///
    /// After each confirmed write, the writer puts the confirmed value into the message that the chat has, and the
    /// overlay keeps only a value that still waits. It does not take the reactions of the answer: the reactions of the
    /// other people come only from a page. A write fails: the writer clears the overlay and stops, so the chat shows
    /// the last confirmed value, and the error shows. A new pairing or the removal of the chat ends the writer: it
    /// changes nothing after that.
    private func writeTeamReaction(_ chat: TeamChatKey, _ id: String, writer: Int) async {
        while let wanted = teamChats[chat]?.reacting[id] {
            let answer: TeamMessageAnswer? = await write("\(chat.path)/messages/\(id)/reaction", method: "PUT",
                                                         payload: TeamReactionRequest(emoji: wanted.emoji),
                                                         as: TeamMessageAnswer.self, refreshInBackground: true,
                                                         idempotencyKey: UUID().uuidString)
            guard teamReactionWriters[chat]?[id] == writer, teamListHas(chat) else { return }
            guard answer != nil else {
                teamChats[chat]?.reacting[id] = nil
                teamReactionWriters[chat]?[id] = nil
                return
            }
            // The Mac made a change, so a page that the app asked for before it can be older.
            teamChatChanges[chat, default: 0] += 1
            // The chat takes each confirmed value at once, so a later failure shows the value that the Mac has.
            teamChats[chat]?.confirmReaction(id, wanted)
        }
        teamReactionWriters[chat]?[id] = nil
    }

    /// Tells the Mac that the person read the chat up to the message with this `first`. The app sends each position
    /// one time, and marks it as sent only after the Mac confirmed it.
    func markTeamRead(_ chat: TeamChatKey, first: Int) async {
        guard !isDemo, first > teamReadMark(chat) ?? 0 else { return }
        teamReadPosting[chat] = first
        let list = teamListVersion
        let answer: TeamReadAnswer? = await write("\(chat.path)/read", payload: TeamReadRequest(first: first, chat: chat.chat),
                                                  as: TeamReadAnswer.self, quiet: true, refreshInBackground: true)
        let newestRead = teamReadPosting[chat] == first
        if newestRead { teamReadPosting[chat] = nil }
        guard let answer, teamListHas(chat) else { return }
        teamReadPosted[chat] = max(teamReadPosted[chat] ?? 0, first)
        // The row of the chat shows the new count at once. A newer read or a newer list has a newer count.
        guard newestRead, list == teamListVersion, let unread = answer.unread,
              let index = team?.rooms.firstIndex(where: { $0.room == chat.room }) else { return }
        if let member = chat.member {
            if let direct = team?.rooms[index].directs?.firstIndex(where: { $0.member == member }),
               team?.rooms[index].directs?[direct].unread != unread {
                team?.rooms[index].directs?[direct].unread = unread
            }
        } else if let bot = chat.bot {
            if let direct = team?.rooms[index].botDirects?.firstIndex(where: { $0.bot == bot }),
               team?.rooms[index].botDirects?[direct].unread != unread {
                team?.rooms[index].botDirects?[direct].unread = unread
            }
        } else if let id = chat.chat {
            if let named = team?.rooms[index].chats?.firstIndex(where: { $0.id == id }),
               team?.rooms[index].chats?[named].unread != unread {
                team?.rooms[index].chats?[named].unread = unread
            }
        } else if team?.rooms[index].unread != unread {
            team?.rooms[index].unread = unread
        }
    }

    /// The person changed the draft of the Team chat or of a named chat: the Mac tells the team that the person types,
    /// at the pace of TeamTypingPace (docs/team-log.md, "Typing"). A direct chat tells nobody. A failure shows nothing.
    func teamDraftChanged(_ chat: TeamChatKey, draft: String) {
        guard !isDemo, !chat.isDirect, connection == .connected, let client,
              teamTypingPace[chat, default: TeamTypingPace()].take(draft: draft, at: .now) else { return }
        let body = (try? JSONEncoder().encode(TeamTypingRequest(chat: chat.chat))) ?? Data("{}".utf8)
        Task {
            let _: TeamTypingAnswer? = try? await client.request("\(chat.path)/typing", method: "POST", body: body,
                                                                 retryTransient: false)
        }
    }

    /// The read position that the app sent or is sending for a chat.
    func teamReadMark(_ chat: TeamChatKey) -> Int? {
        [teamReadPosted[chat], teamReadPosting[chat]].compactMap { $0 }.max()
    }

    /// One document with the content of its current version. Nil when the Mac cannot send it.
    func loadDocument(id: String) async -> BotDocument? {
        if isDemo { return BotDocument.previews(bots: bots).first { $0.id == id } }
        guard let client else { return documentContents[id] }
        let generation = connectionGeneration
        do {
            let value: BotDocument = try await client.request("documents/\(id)")
            guard generation == connectionGeneration, self.client === client else { return nil }
            if documentContents.count >= 30, documentContents[id] == nil { documentContents.removeValue(forKey: documentContents.keys.sorted().first!) }
            if let current = documentContents[id], current.version > value.version { return current }
            documentContents[id] = value
            return value
        } catch {
            guard generation == connectionGeneration else { return nil }
            if let failure = error as? ServiceError, case .response(let status, _) = failure, [403, 404, 410].contains(status) {
                documentContents[id] = nil
                report(error)
                return nil
            }
            if documentContents[id] == nil { report(error) }
            return documentContents[id]
        }
    }

    /// Local list actions finish immediately. A failed save restores the row and reports the error.
    @discardableResult
    func deleteDocument(_ document: BotDocument) async -> Bool {
        if isDemo { documents?.removeAll { $0.id == document.id }; return true }
        documentChanges += 1
        return optimistically("document/\(document.id)", change: { transaction in
            optimisticUpdates.set("0:document/\(document.id)", to: Optional<BotDocument>.none, in: transaction,
                read: { self.documents?.first { $0.id == document.id } }, write: { value in
                    if let value { self.documents = StateUpdates.merge([value], into: self.documents ?? []) }
                    else { self.documents?.removeAll { $0.id == document.id } }
                })
        }, save: {
            let result: Acknowledgement? = await self.write("documents/\(document.id)", method: "DELETE", payload: EmptyRequest(), as: Acknowledgement.self)
            self.optimisticUpdates.receive {
                if result?.ok == true { self.documents?.removeAll { $0.id == document.id }; self.documentContents[document.id] = nil }
            }
            self.documentChanges += 1
        })
    }

    func pinDocument(_ document: BotDocument, pinned: Bool) async {
        if isDemo { if let i = documents?.firstIndex(where: { $0.id == document.id }) { documents?[i].pinned = pinned }; return }
        documentChanges += 1
        optimistically("document/\(document.id)", change: { optimisticDocument(document.id, \.pinned, Optional(pinned), in: $0) }, save: {
            let result: BotDocument? = await self.write("documents/\(document.id)", method: "PATCH", payload: PinRequest(pinned: pinned), as: BotDocument.self)
            self.optimisticUpdates.receive {
                if let result, let i = self.documents?.firstIndex(where: { $0.id == document.id }) { self.documents?[i].pinned = result.pinned }
            }
            self.documentChanges += 1
        })
    }

    private func write<Response: Decodable & Sendable, Payload: Encodable>(_ path: String, method: String = "POST", payload: Payload, as type: Response.Type, quiet: Bool = false, refreshInBackground: Bool = true, idempotencyKey: String? = nil, onFailure: ((Error) -> Void)? = nil, onSuccess: ((Response) -> Void)? = nil) async -> Response? {
        let origin = pairing
        var confirmed = false
        defer { if !confirmed { optimisticUpdates.markFailed() } }
        if method == "POST", ["/messages", "/replies", "/run", "/context"].contains(where: { path.hasSuffix($0) }),
           !(await optimisticUpdates.wait(for: "native-settings")) {
            guard pairing == origin else { return nil }
            let message = "Your agent settings did not save. Review them before retrying this message or task."
            error = message
            onFailure?(ServiceError.configuration(message))
            return nil
        }
        guard pairing == origin else { return nil }
        let storedToken: String?
        do { storedToken = try PairingToken.lookup() } catch {
            if !quiet { self.error = error.localizedDescription }
            return nil
        }
        guard let pairing, let token = storedToken else {
            if !quiet {
                error = savedPairingLocked ? "Coconut cannot read its pairing right now. Try again in a moment."
                    : "Pair with your Mac in Settings to continue."
            }
            return nil
        }
        func belongsToPairing() -> Bool {
            self.pairing == pairing
        }
        writeCount += 1
        defer { writeCount -= 1 }
        var signature: String?
        let generation = connectionGeneration
        var requestClient: APIClient?
        do {
            if client == nil { client = try makeClient(pairing, token: token) }
            guard let client else { return nil }
            requestClient = client
            let body = try APIJSON.encode(payload)
            let requestSignature = method + " " + path + " " + SHA256.hash(data: body).description
            signature = requestSignature
            let explicitKey = idempotencyKey ?? OptimisticRequest.id
            let key = explicitKey ?? uncertainWriteKeys[requestSignature] ?? UUID().uuidString
            if explicitKey == nil { uncertainWriteKeys[requestSignature] = key }
            let (receiptModel, receiptBaseURL, receiptCatalog, receiptApproval, receiptMCP, receiptCapacityLimit, receiptParallelLimit, receiptFallback, receiptMessages, receiptDecisions, receiptBots, receiptRoutines, receiptNativeReadAt, receiptCursor, receiptEpoch) = optimisticUpdates.receive {
                (model, modelBaseURL, modelCatalog, approvals?.defaults, mcpServers, workCapacity?.limit, parallelWork?.limit, fallback, messages, decisions, bots, routines, nativeSettingsReadAt, cachedState?.cursor, snapshotEpoch)
            }
            let result: Response = try await client.request(path, method: method, body: body, idempotencyKey: key)
            guard belongsToPairing() else { return nil }
            confirmed = true
            uncertainWriteKeys.removeValue(forKey: requestSignature)
            if !quiet { error = nil }
            let updatesNativeSettings = path.hasPrefix("settings/") || path.contains("routines") || (path.hasPrefix("bots/") && method == "PATCH")
            let receiptIsCurrent = cachedState?.cursor == receiptCursor && snapshotEpoch == receiptEpoch
            snapshotEpoch = UUID()
            if updatesNativeSettings {
                nativeSettingsTask?.cancel()
            }
            optimisticUpdates.receive {
                if let bot = result as? Bot {
                    if let previous = receiptBots.first(where: { $0.id == bot.id }), let current = bots.first(where: { $0.id == bot.id }) {
                        bots = StateUpdates.merge([StateUpdates.mergeBotReceipt(bot, original: previous, current: current)], into: bots)
                    } else if receiptIsCurrent || path == "bots" { bots = StateUpdates.merge([bot], into: bots) }
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
                else if let settings = result as? FallbackSettings, fallback == receiptFallback { fallback = settings }
                else if let settings = result as? MCPResponse, mcpServers == receiptMCP { mcpServers = settings.servers }
                else if let settings = result as? ParallelWorkSettings {
                    if path == "settings/work-capacity", workCapacity?.limit == receiptCapacityLimit {
                        workCapacity = settings; cachedState?.workCapacity = settings
                    } else if path == "settings/parallel-work", parallelWork?.limit == receiptParallelLimit { parallelWork = settings }
                }
                if method == "DELETE", path.hasPrefix("routines/") {
                    routines.removeAll { "routines/" + $0.id == path }
                    cachedState?.routines = routines
                }
                if method == "DELETE", path.hasPrefix("bots/"), path.split(separator: "/").count == 2 {
                    bots.removeAll { "bots/" + $0.id == path }
                    cachedState?.bots = bots
                }
                var callbackIsCurrent = receiptIsCurrent
                if let receipt = result as? Message {
                    callbackIsCurrent = receiptMessages.first(where: { $0.id == receipt.id })?.reactions == savedMessage(id: receipt.id)?.reactions
                } else if let receipt = result as? Decision {
                    callbackIsCurrent = receiptDecisions.first(where: { $0.id == receipt.id }) == decisions.first(where: { $0.id == receipt.id })
                }
                if callbackIsCurrent { onSuccess?(result) }
            }

            if refreshInBackground {
                scheduleWriteRefresh(using: client)
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
                if Self.isTransportFailure(error) && transportIsCurrent { recordSyncFailure(error) }
            }
            return nil
        }
    }

    private func scheduleWriteRefresh(using client: APIClient) {
        writeRefreshNeeded = true
        guard writeRefreshTask == nil else { return }
        writeRefreshTask = Task {
            while writeRefreshNeeded {
                writeRefreshNeeded = false
                guard self.client === client else { break }
                await refreshAfterWrite(using: client)
            }
            writeRefreshTask = nil
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
        guard foreground, !isDemo, pairing != nil, pollTask == nil, !repairRequired else { return }
        let generation = connectionGeneration
        pollTask = Task { [weak self] in
            guard let self else { return }
            var failures = 0
            var needsSnapshot = refreshImmediately
            while !Task.isCancelled, self.foreground, self.connectionGeneration == generation {
                do {
                    if self.client == nil {
                        // A Keychain that cannot read the token now throws, and the loop tries again after its pause.
                        guard let pairing = self.pairing, let token = try PairingToken.lookup() else { self.connection = .unconfigured; break }
                        self.client = try makeClient(pairing, token: token)
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
                            self.markConnected()
                            failures = 0
                            self.refreshNativeSettings(using: client, force: page.settingsChanged == true || page.reset != nil)
                            self.reloadChangedDocuments(page)
                            self.reloadChangedTeam(page)
                            self.reloadChangedActivity(page)
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
                    self.markConnected()
                    self.scheduleDelivery()
                    Task { await self.syncPushRegistration() }
                    failures = 0
                } catch is CancellationError { break }
                catch let error as URLError where error.code == .cancelled && Task.isCancelled { break }
                catch {
                    guard self.connectionGeneration == generation else { break }
                    self.recordSyncFailure(error)
                    // Only a new pairing link helps, so the phone stops asking the relay.
                    if error as? TunnelError == .needsRepair { break }
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
                    markConnected()
                    refreshNativeSettings(using: client, force: page.settingsChanged == true || page.reset != nil)
                    reloadChangedDocuments(page)
                    reloadChangedTeam(page)
                    reloadChangedActivity(page)
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
        let replaced = snapshot.cursor >= cursor
        if replaced { apply(snapshot, persist: true) }
        // A full state skips the events between the cursors, also the team changes, so a loaded Team screen loads again.
        if replaced, teamAsked { Task { await loadTeam() } }
        if replaced { reloadTeamChats(Set(teamChats.keys)) }
        markConnected()
        Task { await syncPushRegistration() }
        refreshNativeSettings(using: client)
    }

    nonisolated static func newReplies(in incoming: StateSnapshot, after previous: StateSnapshot?) -> [Message] {
        guard let previous, incoming.cursor > previous.cursor else { return [] }
        // Most messages keep their place from one state to the next. A set of the IDs is made only for a reply
        // that moved or that is new.
        var finished: Set<String>?
        return incoming.messages.enumerated().filter { index, message in
            guard message.role == "assistant", message.kind == "text", !message.streaming else { return false }
            if index < previous.messages.count, previous.messages[index].id == message.id,
               !previous.messages[index].streaming { return false }
            let known = finished ?? Set(previous.messages.filter { !$0.streaming }.map(\.id))
            finished = known
            return !known.contains(message.id)
        }.map(\.element)
    }

    private func apply(_ incoming: StateSnapshot, persist: Bool) {
        optimisticUpdates.receive {
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
                snapshot.fallback = fallback
                snapshot.mcpServers = mcpServers
                snapshot.routines = routines
                snapshot.bots = preservingNativeSettings(in: snapshot.bots)
            }
            // The set holds each sent message of the state. Only a message that waits needs it.
            if !outgoingMessages.isEmpty {
                let confirmed = Set(snapshot.messages.compactMap(\.clientRequestId) + (snapshot.confirmedRequestIds ?? []))
                if outgoingMessages.contains(where: { confirmed.contains($0.id) }) {
                    outgoingMessages.removeAll { confirmed.contains($0.id) }
                    try? saveOutgoing()
                }
            }
            assign(\.bots, snapshot.bots)
            assign(\.tasks, snapshot.tasks)
            assign(\.messages, snapshot.messages)
            assign(\.decisions, snapshot.decisions)
            assign(\.artifacts, snapshot.artifacts)
            assign(\.approvals, snapshot.approvals)
            let previewLimit = ParallelWorkSettings(limit: nil, effectiveLimit: 4, automaticLimit: 4)
            assign(\.workCapacity, snapshot.workCapacity ?? (isDemo ? previewLimit : nil))
            assign(\.parallelWork, snapshot.parallelWork ?? (isDemo ? previewLimit : nil))
            assign(\.fallback, snapshot.fallback ?? (isDemo ? FallbackSettings(models: []) : nil))
            assign(\.mcpServers, snapshot.mcpServers ?? [])
            assign(\.routines, snapshot.routines ?? [])
            if foreground { scheduleDelivery() }
            cursor = snapshot.cursor
            assign(\.provider, snapshot.provider)
            assign(\.model, snapshot.model)
            assign(\.modelBaseURL, snapshot.baseUrl ?? "")
            assign(\.modelConfigured, snapshot.modelConfigured)
            assign(\.modelCatalog, snapshot.modelCatalog)
            assign(\.pushConfigured, snapshot.pushConfigured ?? false)
            lastSync = Date()
            cachedState = snapshot
            if persist { saveCache(snapshot) }
        }
    }

    /// Observation notifies the readers of a property on every assignment, also of an equal value.
    /// Assign only a changed value, so a routine update does not redraw every screen.
    private func assign<Value: Equatable>(_ property: ReferenceWritableKeyPath<AppStore, Value>, _ value: Value) {
        if self[keyPath: property] != value { self[keyPath: property] = value }
    }

    private func markConnected() {
        let connecting = connection != .connected
        assign(\.connection, .connected)
        assign(\.syncIssue, nil)
        // The Chats list shows the teams, so the app has them as soon as it reaches the Mac.
        if connecting { Task { await loadTeam() } }
    }

    /// Encoding the saved state costs about as much as decoding an update. While updates stream, write it at most
    /// every 10 seconds. Leaving the foreground writes it at once, so a relaunch replays at most a few updates.
    private func saveCache(_ snapshot: StateSnapshot, immediately: Bool = false) {
        pendingCache = SnapshotCache(snapshot: snapshot, syncedAt: lastSync ?? Date())
        if immediately {
            cacheTask?.cancel()
            cacheTask = nil
            writeCache()
            return
        }
        guard cacheTask == nil else { return }
        let delay = max(0.3, 10 - Date().timeIntervalSince(cacheWrittenAt))
        cacheTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, !Task.isCancelled else { return }
            self.cacheTask = nil
            self.writeCache()
        }
    }

    private func writeCache() {
        guard let cache = pendingCache else { return }
        pendingCache = nil
        cacheWrittenAt = Date()
        cacheQueue.async { try? ProtectedCache.write(cache, name: "snapshot.json") }
    }

    func recordSyncFailure(_ failure: Error) {
        if failure is CancellationError || ((failure as? URLError)?.code == .cancelled && Task.isCancelled) { return }
        // A pairing that needs repair stops polling until Reconnect or a new pairing. A later failure, for example of
        // a write, must not hide the repair: the banner would lose its Pair button while nothing tries again.
        if repairRequired { return }
        if let failure = failure as? TunnelError {
            // The poll repeats the same failure, so a view updates only when the state changes.
            if failure == .needsRepair { repairRequired = true }
            assign(\.connection, ConnectionState(failure))
            assign(\.syncIssue, failure.message)
            return
        }
        connection = .disconnected
        syncIssue = failure is URLError ? Self.requestFailed : failure.localizedDescription
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
        await registerPush(client: client, pairing: pairing)
        await sendPreviewReports(client: client)
    }

    private func registerPush(client: APIClient, pairing: PairingConfiguration) async {
        let push = PushNotifications.shared
        await push.refresh(requestPermission: pushConfigured)
        guard self.client === client else { return }
        let preferences = push.preferences
        // An invalid enabled edit is not sent; the Mac keeps the last valid schedule. Turning it off always is.
        let schedule = push.schedule.registrable
        let signature = PushRegistration.signature(deviceId: pairing.deviceId, environment: push.environment, token: push.deviceToken,
                                                   allowed: push.allowed, preferences: preferences, schedule: schedule,
                                                   outside: push.outsideWorkHours)
        // The Mac removes a registration that is not refreshed for 30 days, so a running app refreshes it twice a day.
        guard registeredPushSignature != signature || Date().timeIntervalSince(registeredPushAt) > 12 * 3600 else { return }
        do {
            let response: PushRegistrationResponse
            if push.allowed {
                guard let token = push.deviceToken else { return }
                let configuration: PushRelayConfiguration = try await client.request("push/config")
                let relay: PushRelayGrant
                do {
                    relay = try await PushRelayClient.shared.register(deviceId: pairing.deviceId, token: token,
                        environment: push.environment, configuration: configuration)
                    PreviewDiagnostics.record(nil, source: .app)
                } catch let failure as NotificationKeys.Failure {
                    // The keys of the previews are not usable. The Mac shows this reason in coconut status.
                    PreviewDiagnostics.record(failure.reason, source: .app)
                    throw failure
                }
                guard self.client === client, push.allowed else {
                    PushRelayClient.shared.disable(deviceId: pairing.deviceId)
                    return
                }
                response = try await client.request("device/push", method: "PUT",
                    body: APIJSON.encode(PushRegistration(token: token, environment: push.environment, enabled: true, relay: relay, preferences: preferences, schedule: schedule,
                                                      outsideWorkHours: push.outsideWorkHours.registrable)),
                    idempotencyKey: UUID().uuidString)
            } else {
                PushRelayClient.shared.disable(deviceId: pairing.deviceId)
                response = try await client.request("device/push", method: "DELETE", body: Data("{}".utf8), idempotencyKey: UUID().uuidString)
            }
            guard self.client === client else { return }
            registeredPushSignature = signature
            registeredPushAt = Date()
            pushConfigured = response.configured
            pushRegistrationError = nil
        } catch {
            pushRegistrationError = "Notification setup has not reached your Mac. Coconut will retry when connected."
        }
    }

    /// Send the last preview results to the Mac when they change. A Mac before this report answers 404, and the
    /// next change tries again.
    private func sendPreviewReports(client: APIClient) async {
        let reports = PreviewReports(extension: PreviewDiagnostics.read(.extension), app: PreviewDiagnostics.read(.app))
        guard reports != sentPreviewReports, reports.extension != nil || reports.app != nil else { return }
        let encoder = APIJSON.encoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let body = try? encoder.encode(reports),
              let _: PreviewReportsResponse = try? await client.request("device/push/previews", method: "PUT", body: body,
                                                                        idempotencyKey: UUID().uuidString),
              self.client === client else { return }
        sentPreviewReports = reports
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
                self.optimisticUpdates.receive {
                    let values = Dictionary(uniqueKeysWithValues: native.bots.map { ($0.id, $0) })
                    var bots = self.bots
                    for index in bots.indices {
                        guard let bot = values[bots[index].id] else { continue }
                        bots[index].name = bot.name
                        bots[index].role = bot.role
                        bots[index].instructions = bot.instructions
                        bots[index].modelOverride = bot.modelOverride
                        bots[index].effectiveModel = bot.effectiveModel
                        bots[index].approvalOverride = bot.approvalOverride
                        bots[index].effectiveApproval = bot.effectiveApproval
                        bots[index].workerLimit = bot.workerLimit
                        bots[index].modelError = bot.modelError
                    }
                    self.assign(\.bots, bots)
                    self.assign(\.modelCatalog, native.modelCatalog)
                    self.assign(\.model, native.modelCatalog?.defaults.model ?? "")
                    self.assign(\.modelBaseURL, native.modelCatalog?.defaults.baseUrl ?? "")
                    self.assign(\.modelConfigured, native.modelConfigured)
                    self.assign(\.approvals, native.approvals)
                    self.assign(\.parallelWork, native.parallelWork)
                    self.assign(\.fallback, native.fallback)
                    self.assign(\.mcpServers, native.mcpServers ?? [])
                    self.assign(\.routines, native.routines ?? [])
                    if let configured = native.pushConfigured { self.assign(\.pushConfigured, configured) }
                    self.nativeSettingsReadAt = Date()
                    if var state = self.cachedState {
                        state.bots = self.bots
                        state.pushConfigured = self.pushConfigured
                        self.apply(state, persist: true)
                        Task { await self.syncPushRegistration() }
                }
                }
            } catch { /* Content remains usable while native settings are unavailable. */ }
        }
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
            state.artifacts = StateUpdates.merge(state.artifacts, into: page.artifacts ?? [])
            state.historyBefore?[botId] = page.before
            apply(state, persist: true)
            return page.before != before
        } catch { report(error); return false }
    }

    /// Read what the Mac last delivered to this iPhone, for the notification settings. A failed read leaves no status,
    /// so the screen says that it is not available instead of showing an empty history.
    func refreshPushStatus() async {
        guard !isDemo, let client, connection == .connected else {
            pushStatus = nil
            return
        }
        let status: PushStatus? = try? await client.request("device/push")
        guard self.client === client else { return }
        pushStatus = status
    }

    func notificationDestination(_ target: NotificationTarget) async -> NotificationDestination? {
        guard acceptsNotification(target) else { return nil }
        if let room = target.noticeRoom { return .notices(room) }
        if target.team != nil { return await teamNotificationDestination(target) }
        if target.group { return .inboxList }
        if target.decisionId == nil { return .conversation(target.conversationRoute) }
        // Without the Mac, the phone opens the card that it has, or the chat. Waiting would keep the Inbox busy
        // until the Mac answered again.
        // It reads the cards when it is used: a poll during the request can resolve the card.
        func offline() -> NotificationDestination? {
            target.destination(pendingDecisionIds: Set(decisions.filter(\.isPending).map(\.id)), snapshotReady: true)
        }
        guard let client else { return offline() }
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
        } catch {
            guard self.client === client, acceptsNotification(target) else { return nil }
            return offline()
        }
    }

    /// The place that a tapped notification shows at once, before the Mac confirms it: the chat of a team, the card,
    /// or the conversation.
    func notificationPreview(_ target: NotificationTarget) -> NotificationDestination {
        if let room = target.noticeRoom { return .notices(room) }
        return target.chat.map(teamNotificationPlace)
            ?? target.decisionId.map(NotificationDestination.inbox) ?? .conversation(target.conversationRoute)
    }

    /// The chat of the team of a notification, or the Chats list when the app no longer has the team, for example
    /// after a leave. The app reads the list of the teams first when the list does not have the chat. Nil when the
    /// pairing changed during that read, as for a card.
    func teamNotificationDestination(_ target: NotificationTarget) async -> NotificationDestination? {
        guard let chat = target.chat else { return nil }
        // The list can lack the team, or a named chat that the admin made while the app was in the background.
        if team?.has(chat) != true { await loadTeam() }
        guard acceptsNotification(target) else { return nil }
        return teamNotificationPlace(chat)
    }

    /// The place of a team notification with the list that the app has now: the chat of the key. It takes the whole
    /// key, so no caller can drop a part of it. A list that the app has not loaded, or cannot read, keeps each chat
    /// (`teamListHas`), so a tap in a terminated app opens the chat at once.
    func teamNotificationPlace(_ chat: TeamChatKey) -> NotificationDestination {
        return teamListHas(chat) ? NotificationDestination(chat: chat) : .chatList
    }

    func acceptsNotification(_ target: NotificationTarget) -> Bool {
        target.deviceId == pairing?.deviceId
    }

    private func report(_ error: Error, updateConnection: Bool = true) {
        // A request cut by a cancel or by the app going to the background has no message for the person.
        if error is CancellationError { return }
        if let error = error as? URLError {
            self.error = "Couldn't complete this request. Try again."
            if updateConnection { syncIssue = Self.requestFailed }
            #if DEBUG
            NSLog("Coconut connection: URL error %d", error.code.rawValue)
            #endif
        } else if let error = error as? TunnelError {
            self.error = error == .needsRepair ? error.message : "Couldn't complete this request. Try again."
            if updateConnection { syncIssue = error.message }
        } else { self.error = error.localizedDescription }
    }

    // Only the loopback transport of a simulator debug build fails with a URLError.
    private static let requestFailed = "The request to your Mac failed. Reconnect to check the current state."

    #if DEBUG && targetEnvironment(simulator)
    // The test servers and scripts/desktop.py start the simulator with a loopback address, and a pairing code or a token.
    private func configureFromLaunchEnvironment() {
        let environment = ProcessInfo.processInfo.environment
        guard let address = environment["COCONUT_SERVER_URL"] else { return }
        if let code = environment["COCONUT_PAIR_CODE"], !code.isEmpty {
            loopbackBootstrap = (address, code)
        } else if let token = environment["COCONUT_DEVICE_TOKEN"], !token.isEmpty {
            do {
                _ = try APIClient(loopback: address, token: token)
                try PairingToken.save(token)
                let configuration = PairingConfiguration(loopback: address, deviceId: "simulator-bootstrap")
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

// pairing.json. Version 2 pairs through the relay tunnel. Only a simulator debug build uses version 1, with an http://
// loopback address in `serverURL`, for the test servers. Any other version 1 pairing cannot connect.
struct PairingConfiguration: Codable, Equatable, Sendable {
    var version = 2
    let deviceId: String
    var relay: String?
    var route: String?
    var macKey: String?
    // The name of the Mac from the pairing link, for display only.
    var macName: String?
    var serverURL: String?

    init(setup: PairingSetup, deviceId: String) {
        self.deviceId = deviceId
        relay = setup.relay.absoluteString
        route = setup.route
        macKey = setup.macKey.base64EncodedString()
        macName = setup.name
    }

    #if DEBUG && targetEnvironment(simulator)
    init(loopback serverURL: String, deviceId: String) {
        version = 1
        self.deviceId = deviceId
        self.serverURL = serverURL
    }
    #endif

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = try values.decodeIfPresent(Int.self, forKey: .version) ?? 1
        deviceId = try values.decode(String.self, forKey: .deviceId)
        relay = try values.decodeIfPresent(String.self, forKey: .relay)
        route = try values.decodeIfPresent(String.self, forKey: .route)
        macKey = try values.decodeIfPresent(String.self, forKey: .macKey)
        macName = try values.decodeIfPresent(String.self, forKey: .macName)
        serverURL = try values.decodeIfPresent(String.self, forKey: .serverURL)
        guard version == 2 ? tunnelEndpoint != nil : serverURL != nil else {
            throw DecodingError.dataCorruptedError(forKey: .version, in: values, debugDescription: "Incomplete pairing")
        }
    }

    var usesTunnel: Bool { version == 2 }

    var tunnelEndpoint: TunnelEndpoint? {
        guard version == 2, let relay, let url = URL(string: relay), let route,
              let macKey, let key = Data(base64Encoded: macKey), key.count == 32 else { return nil }
        return TunnelEndpoint(relay: url, route: route, macKey: key)
    }
}
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
private struct ArchiveRequest: Encodable { let archived: Bool }
private struct TeamCreateRequest: Encodable { let name: String; let memberName: String }
private struct TeamJoinBody: Encodable { let link: String; let memberName: String }
private struct TeamProfileRequest: Encodable { let memberName: String }
private struct TeamNotificationsRequest: Encodable { let enabled: Bool }
private struct TeamNotificationsAnswer: Decodable, Sendable { let room: String; let notifications: Bool }
private struct TeamPinRequest: Encodable { let pinned: Bool }
private struct TeamChatPinAnswer: Decodable, Sendable { let room: String; let chat: String?; let pinned: Bool }
private struct TeamChatNotificationsAnswer: Decodable, Sendable {
    let room: String; let chat: String; let notifications: Bool
}
/// The answer of a share or a stop of a page: the document with its teams.
private struct PageShareAnswer: Decodable, Sendable { let document: BotDocument }
/// A nil `mentions` or `aboutDocumentId` is not in the JSON: a message without a mention or a page sends none.
private struct TeamSendRequest: Encodable {
    let text: String; let replyTo: String?; let mentions: [String]?; let chat: String?; let aboutDocumentId: String?
    let preview: TeamLinkPreview?
    /// The draft IDs of the files, or nil for a message without files.
    var files: [String]? = nil
}
/// A nil `type`, `width` or `height` is not in the JSON.
private struct TeamFileRequest: Encodable {
    let name: String; let content: String; let type: String?; let width: Int?; let height: Int?
}
private struct TeamFileAnswer: Decodable, Sendable { let file: TeamFile }
private struct TeamFileDropAnswer: Decodable, Sendable { let file: String }
private struct TeamEditRequest: Encodable { let text: String }
private struct TeamReactionRequest: Encodable { let emoji: String }
private struct TeamReadRequest: Encodable { let first: Int; let chat: String? }
private struct TeamTypingRequest: Encodable { let chat: String? }
/// The body of a new chat and of a new name. A delete sends no name.
private struct TeamChatRequest: Encodable { let name: String? }
private struct TeamNoticeRequest: Encodable { let type: String; let text: String; let endsAt: String? }
private struct TeamNoticeChange: Encodable { let closed: Bool }
private struct TeamNoticesSeen: Encodable { let notices: [String] }
/// The answer of a change of the named chats: the room, and the ID of the chat.
private struct TeamChatAnswer: Decodable, Sendable { let room: TeamRoom; let chat: String }
private struct InstructionsRequest: Encodable { let instructions: String }
private struct PreferenceRequest: Encodable { let text: String }
private struct RoutineRequest: Encodable { let name: String; let prompt: String; let cron: String; let timezone: String; let enabled: Bool }
struct MessageRequest: Codable, Equatable { let text: String; let mode: String; let taskId: String?; let scheduledAt: String?; let attachmentIds: [String]; let recipientBotIds: [String]; var replyToMessageId: String? = nil; var mentionedBotIds: [String]? = nil; var workArtifactId: String? = nil; var workStatusOnly: Bool? = nil; var aboutDocumentId: String? = nil }
private struct ReactionRequest: Encodable { let emoji: String; let active: Bool }
private struct QueueTextRequest: Encodable { let text: String }
private struct QueueOrderRequest: Encodable { let messageIds: [String] }
struct OutgoingMessage: Codable, Identifiable, Equatable {
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
struct OutgoingCache: Codable {
    let deviceId: String
    let messages: [OutgoingMessage]
    var answers: [OutgoingAnswer]? = nil
    /// The messages of the Team chat that the Mac did not confirm, by room.
    var teamMessages: [String: [TeamPendingMessage]]? = nil
    /// The messages of the direct chats that the Mac did not confirm, by room and other member. An older app has none,
    /// and a cache of an older app loads without it.
    var directMessages: [String: [String: [TeamPendingMessage]]]? = nil
    /// The messages of the named chats that the Mac did not confirm, by room and chat. An older app has none.
    var namedMessages: [String: [String: [TeamPendingMessage]]]? = nil
    /// The messages of the direct chats with a bot that the Mac did not confirm, by room and team bot ID. An older app
    /// has none.
    var botDirectMessages: [String: [String: [TeamPendingMessage]]]? = nil

    /// The unsent messages of each chat of a team. Each part is nil when it has no message.
    var pendingTeamMessages: [TeamChatKey: [TeamPendingMessage]] {
        get {
            var chats: [TeamChatKey: [TeamPendingMessage]] = [:]
            for (room, pending) in teamMessages ?? [:] { chats[TeamChatKey(room: room)] = pending }
            for (room, members) in directMessages ?? [:] {
                for (member, pending) in members { chats[TeamChatKey(room: room, member: member)] = pending }
            }
            for (room, named) in namedMessages ?? [:] {
                for (chat, pending) in named { chats[TeamChatKey(room: room, chat: chat)] = pending }
            }
            for (room, bots) in botDirectMessages ?? [:] {
                for (bot, pending) in bots { chats[TeamChatKey(room: room, bot: bot)] = pending }
            }
            return chats
        }
        set {
            var team: [String: [TeamPendingMessage]] = [:]
            var direct: [String: [String: [TeamPendingMessage]]] = [:]
            var named: [String: [String: [TeamPendingMessage]]] = [:]
            var bots: [String: [String: [TeamPendingMessage]]] = [:]
            for (chat, pending) in newValue {
                if let member = chat.member { direct[chat.room, default: [:]][member] = pending }
                else if let bot = chat.bot { bots[chat.room, default: [:]][bot] = pending }
                else if let id = chat.chat { named[chat.room, default: [:]][id] = pending }
                else { team[chat.room] = pending }
            }
            teamMessages = team.isEmpty ? nil : team
            directMessages = direct.isEmpty ? nil : direct
            namedMessages = named.isEmpty ? nil : named
            botDirectMessages = bots.isEmpty ? nil : bots
        }
    }
}
struct OutgoingAnswer: Codable, Identifiable, Equatable {
    let id: String
    let decisionId: String
    let botId: String
    let createdAt: String
    let sequence: Int
    let text: String
    let request: AnswerRequest
    var replyTo: MessageReference? = nil
    /// The app holds the answer until this time, so the person can undo it. An answer saved before the Undo window
    /// has none and sends at once. `id` is the idempotency key of the request, the same as before the window.
    var sendAt: Date? = nil

    func isWaiting(at now: Date = Date()) -> Bool { sendAt.map { $0 > now } ?? false }
    var message: Message {
        Message(id: "outgoing-" + id, botId: botId, taskId: nil, role: "user", kind: "text", text: text,
                decisionId: request.choice == "reply" ? decisionId : nil, artifactId: nil, createdAt: createdAt,
                streaming: false, clientRequestId: id, replyTo: replyTo)
    }
}
struct AnswerRequest: Codable, Equatable { let choice: String; let revision: Int; let correction: String?; let optionId: String?; let draftHash: String?; let previewsHash: String? }
private struct DraftEditRequest: Encodable { let revision: Int; let draft: MessageDraft }
private struct InboxRequest: Encodable { let action: String; let revision: Int; let swipeId: String? }
private struct ReplyRequest: Encodable { let text: String; let attachmentIds: [String]; let decisionId: String?; let mentionedBotIds: [String] }
private struct ContextRequest: Encodable { let question: String }

private struct ModelResponse: Decodable, Sendable { let model: String; let modelConfigured: Bool; let baseUrl: String?; let modelCatalog: ModelCatalog? }
private struct AttachmentRequest: Encodable { let botId: String; let filename: String; let contentBase64: String }
private struct LearningResponse: Decodable, Sendable { let groups: [LearningGroup] }

enum ProtectedCache {
    private static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent(TestIsolation.name("Coconut"), isDirectory: true)
    }

    static func read<T: Decodable>(_ name: String, in location: URL? = nil) -> T? { try? lookup(name, in: location) }

    /// The saved value, or nil when there is no file or the file holds another format. Throws when the file exists
    /// but the device cannot read it now. The files have complete protection, so a Mac with a locked screen refuses
    /// the read, and a locked iPhone too. That is not a missing file.
    static func lookup<T: Decodable>(_ name: String, in location: URL? = nil) throws -> T? {
        let data: Data
        do { data = try Data(contentsOf: (location ?? directory).appendingPathComponent(name)) }
        catch let error as CocoaError where error.code == .fileReadNoSuchFile { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    static func write<T: Encodable>(_ value: T, name: String, in location: URL? = nil) throws {
        var directory = location ?? directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.complete])
        var resourceValues = URLResourceValues()
        resourceValues.isExcludedFromBackup = true
        try directory.setResourceValues(resourceValues)
        try JSONEncoder().encode(value).write(to: directory.appendingPathComponent(name), options: [.atomic, .completeFileProtection])
    }

    static func remove(_ name: String, in location: URL? = nil) {
        try? FileManager.default.removeItem(at: (location ?? directory).appendingPathComponent(name))
    }
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


