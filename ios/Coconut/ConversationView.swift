import SwiftUI
import UniformTypeIdentifiers

struct ConversationView: View {
    @Environment(AppStore.self) private var store
    let botId: String
    var focusDecisionId: String? = nil
    var focusMessageId: String? = nil
    private var focusTarget: String? { focusDecisionId ?? focusMessageId.map { store.displayedMessageId(for: $0) } }
    private func focusTargetExists(in messages: [Message]) -> Bool {
        if let focusDecisionId { return store.decisions.contains { $0.id == focusDecisionId } }
        if focusMessageId != nil, let focusTarget { return messages.contains { $0.id == focusTarget } }
        return false
    }
    @State private var messageDraft = ""
    @State private var workContext: WorkReference?
    @State private var replyTo: MessageReference?
    /// A focus of the composer that waits for the push to end. A tap on the chat or leaving it cancels it.
    @State private var focusAfterPush: UUID?
    @State private var messageAttachments: [Artifact] = []
    @State private var messageScheduling = false
    @State private var messageScheduledAt = Date().addingTimeInterval(3600)
    @State private var messageMentions: [SelectedBotMention] = []
    @State private var stop = StopConfirmation<AgentTask>()
    @State private var didFocusDecision = false
    @State private var nearBottom = true
    @State private var userScrolling = false
    @State private var latestHidden = false
    @State private var rowHold = ConversationRowHold()
    @State private var footerScroll = ConversationFooterScroll()
    @State private var isVisible = false
    @State private var transcript = TranscriptCache()
    /// The moment that the day labels count from. A new day or a new time zone replaces it, so an open chat groups
    /// its rows and writes its labels again at once.
    @State private var dayReference = Date()
    @State private var pinnedDay = PinnedDay()
    #if os(macOS)
    /// The chat runs up under the toolbar of the window, where the title of the bot floats. The first message and the
    /// pinned day pill start below the title.
    private static let titleHeight: CGFloat = 52
    #else
    private static let titleHeight: CGFloat = 0
    #endif
    @FocusState private var composerFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.toggleBotDetails) private var toggleBotDetails
    @ScaledMetric(relativeTo: .headline) private var nameWidth = 150

    init(botId: String, focusDecisionId: String? = nil, focusMessageId: String? = nil, attachedWork: WorkReference? = nil) {
        self.botId = botId
        self.focusDecisionId = focusDecisionId
        self.focusMessageId = focusMessageId
        _workContext = State(initialValue: attachedWork)
    }

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var current: (messages: [Message], entries: [TranscriptEntry]) {
        // A snapshot of the calendar, not the auto-updating one: two auto-updating calendars are always equal, so the
        // cache could not see a new time zone.
        transcript.current(in: store, botId: botId, focusDecisionId: focusDecisionId, calendar: .current)
    }
    private var activity: ConversationActivity? { store.conversationActivity(botId) }
    private var activeTask: AgentTask? {
        store.tasks.last { $0.chatBotId == botId && $0.isRunning }
            ?? store.tasks.last { $0.chatBotId == botId && ["needs_you", "waiting"].contains($0.status) }
    }


    @MainActor
    private func loadAndScrollToMessage(_ id: String, proxy: ScrollViewProxy) async {
        nearBottom = false
        while !store.messages.contains(where: { $0.id == id || ($0.kind == "decision" && $0.decisionId == id) }) && store.hasOlderMessages(botId) && !Task.isCancelled {
            if !(await store.loadOlderMessages(botId)) { break }
        }
        await Task.yield()
        guard !Task.isCancelled else { return }
        show(TranscriptEntry.rowId(for: store.displayedMessageId(for: id), in: current.entries), proxy: proxy)
    }

    private func show(_ row: String, proxy: ScrollViewProxy) {
        proxy.scrollTo(row, anchor: .top)
        rowHold.hold(row)
    }

    var body: some View {
        let (messages, entries) = current
        Group {
            if let bot {
                ScrollViewReader { proxy in
                    // A lazy stack froze here: rows of very different heights switched between their estimated and
                    // real height on every frame. A list sizes its rows without that loop and stays as fast.
                    List {
                        Group {
                            conversationHeader(bot: bot, messages: messages)
                            if store.hasOlderMessages(botId) {
                                Button {
                                    let anchor = entries.first?.message.id
                                    nearBottom = false
                                    Task {
                                        await store.loadOlderMessages(botId)
                                        await Task.yield()
                                        if let anchor, let entry = current.entries.first(where: { $0.messages.contains { $0.id == anchor } }) {
                                            let target = entry.message.kind == "decision" ? (entry.message.decisionId ?? entry.id) : entry.id
                                            show(target, proxy: proxy)
                                        }
                                    }
                                } label: {
                                    if store.historyLoading.contains(botId) { ProgressView() }
                                    else { Text("Load earlier messages").font(.caption) }
                                }
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .disabled(store.historyLoading.contains(botId))
                                .accessibilityIdentifier("load-earlier-messages")
                            }
                            ForEach(store.tasks.filter {
                                $0.botId == botId && $0.sourceBotId == nil && ($0.conversationBotId ?? botId) != botId
                            }) { task in
                                if let origin = store.bots.first(where: { $0.id == task.conversationBotId }) {
                                    NavigationLink(value: ConversationRoute(botId: origin.id)) {
                                        Label("In \(origin.name)", systemImage: "arrow.up.left")
                                            .font(.caption).foregroundStyle(.secondary)
                                            .frame(maxWidth: .infinity, minHeight: 44)
                                    }.buttonStyle(.plain)
                                }
                            }
                            ForEach(entries) { entry in
                                let message = entry.message
                                let date = message.createdAt.apiDate
                                VStack(spacing: 0) {
                                if entry.startsDay, let date {
                                    DaySeparatorRow(date: date, now: dayReference, row: entry.id, pinned: pinnedDay)
                                }
                                if entry.isExchange {
                                    BotExchangeRow(messages: entry.messages, botId: botId)
                                } else {
                                transcriptRow(entry) { id in
                                    Task { await loadAndScrollToMessage(id, proxy: proxy) }
                                }
                                }
                                }
                                    // The pinned day pill follows the row at the top of the chat.
                                    .pinnedDayRow(pinnedDay, id: entry.id, date: date, startsDay: entry.startsDay)
                                    // Keep full text height when the list resizes or scrolls to a new message.
                                    .fixedSize(horizontal: false, vertical: true)
                                    .id(message.kind == "decision" ? (message.decisionId ?? entry.id) : entry.id)
                            }
                            if let activity {
                                BotStatusRow(bot: bot, activity: activity)
                                    .transition(.opacity.animation(BoardStyle.fade(reduceMotion: reduceMotion)))
                            }
                            // The row inset and this padding keep 14 points below the last row.
                            Color.clear.frame(height: 1).padding(.bottom, 9).id("conversation-bottom")
                        }
                        .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                    }
                    .listStyle(.plain)
                    #if os(macOS)
                    .contentMargins(.top, Self.titleHeight, for: .scrollContent)
                    #endif
                    .buttonStyle(.borderless)
                    .scrollContentBackground(.hidden)
                    .environment(\.defaultMinListRowHeight, 1)
                    .accessibilityIdentifier("conversation-transcript")
                    .modifier(ConversationTimestamps())
                    .defaultScrollAnchor(focusTarget == nil ? .bottom : .top, for: .initialOffset)
                    .defaultScrollAnchor(focusTarget == nil ? .bottom : .top, for: .alignment)
                    .scrollDismissesKeyboard(.interactively)
                    .contentShape(Rectangle())
                    .simultaneousGesture(TapGesture().onEnded { focusAfterPush = nil; composerFocused = false })
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                    } action: { _, value in
                        // Only a user scroll changes whether new messages follow the bottom.
                        if userScrolling { nearBottom = value }
                    }
                    .onScrollGeometryChange(for: Bool.self) { ConversationFooterScroll.hiddenBelow(in: $0) > 32 } action: { _, hidden in
                        withAnimation(BoardStyle.fade(reduceMotion: reduceMotion)) { latestHidden = hidden }
                    }
                    .onScrollGeometryChange(for: CGFloat.self) { ConversationFooterScroll.offset(in: $0) } action: { _, offset in
                        // This runs on every frame of a scroll. A write of an unchanged value would still redraw the
                        // whole chat on each frame, so only a change is written.
                        var next = footerScroll
                        next.scroll(to: offset)
                        if next != footerScroll { footerScroll = next }
                    }
                    .onScrollGeometryChange(for: [CGFloat].self) { [$0.contentOffset.y, $0.contentSize.height, $0.containerSize.height, $0.contentInsets.top + $0.contentInsets.bottom] } action: { old, new in
                        #if os(macOS)
                        // A mouse wheel, and some trackpad scrolls, report no scroll phase on the Mac, so userScrolling
                        // stays false. Then the chat never learned that the reader left the bottom, and each new line of
                        // a bot pulled it back down. An upward move while the content does not shrink is the reader's,
                        // and a downward move that reaches the bottom follows new messages again.
                        // A taller window or a new inset moves the offset too, so only a move in an unchanged viewport counts.
                        let sameViewport = abs(new[2] - old[2]) < 0.5 && abs(new[3] - old[3]) < 0.5
                        if sameViewport && new[0] < old[0] - 0.5 && new[1] >= old[1] - 0.5 { nearBottom = false }
                        else if new[0] > old[0] + 0.5 && new[0] + new[2] >= new[1] - 4 { nearBottom = true }
                        #endif
                        let change = zip(old.prefix(2), new.prefix(2)).map { abs($1 - $0) }.max() ?? 0
                        if let row = rowHold.rowToRestore(layoutChange: change, userScrolling: userScrolling, followsBottom: nearBottom) {
                            proxy.scrollTo(row, anchor: .top)
                        }
                    }
                    .onScrollPhaseChange { previous, phase, context in
                        footerScroll.phaseChanged(phase, offset: ConversationFooterScroll.offset(in: context.geometry))
                        // A Mac reports no phase for a mouse wheel, a scroll bar or the keyboard. There the pinned day
                        // pill follows the scroll position.
                        #if !os(macOS)
                        pinnedDay.scrolls(phase != .idle)
                        #endif
                        userScrolling = phase == .interacting || phase == .decelerating
                        rowHold.phaseChanged(phase)
                        if userScrolling || previous == .interacting || previous == .decelerating {
                            let geometry = context.geometry
                            nearBottom = geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                            // Content that arrived during the scroll did not pull the chat down. A scroll that ends near
                            // the bottom catches up with it once.
                            if !userScrolling && nearBottom && (focusTarget == nil || didFocusDecision) {
                                proxy.scrollTo("conversation-bottom", anchor: .bottom)
                            }
                        }
                    }
                    .onScrollGeometryChange(for: CGSize.self) { $0.contentSize } action: { _, _ in
                        // A scroll of the reader wins over new content: the chat follows again when the scroll ends.
                        if nearBottom && !userScrolling && (focusTarget == nil || didFocusDecision) {
                            proxy.scrollTo("conversation-bottom", anchor: .bottom)
                        }
                    }
                    .onScrollGeometryChange(for: [CGFloat].self) { [$0.containerSize.height, $0.contentInsets.bottom] } action: { _, _ in
                        // Follow keyboard and composer resizing only when the reader stays at the bottom. The keyboard
                        // and the composer change the bottom inset of the chat, not the size of its container.
                        if !userScrolling && nearBottom && (focusTarget == nil || didFocusDecision) {
                            proxy.scrollTo("conversation-bottom", anchor: .bottom)
                        }
                    }
                    .onChange(of: nearBottom) { _, following in
                        if following { rowHold.release() }
                    }
                    .onChange(of: activity != nil) { _, _ in
                        if nearBottom && !userScrolling { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last?.id) { _, _ in
                        if nearBottom { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last(where: { $0.role == "user" })?.id) { _, messageId in
                        guard let messageId, messages.last?.id == messageId else { return }
                        nearBottom = true
                        proxy.scrollTo("conversation-bottom", anchor: .bottom)
                    }
                    .onChange(of: messages.last?.text) { _, _ in
                        if nearBottom && !userScrolling { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .overlay(alignment: .bottomTrailing) {
                        // Newer messages can be below the screen, for example when a focused card opens the chat.
                        if latestHidden {
                            Button {
                                nearBottom = true
                                footerScroll.reset()
                                withAnimation(BoardStyle.fade(reduceMotion: reduceMotion)) {
                                    proxy.scrollTo("conversation-bottom", anchor: .bottom)
                                }
                            } label: {
                                Image(systemName: "arrow.down").font(.body.weight(.semibold))
                                    .frame(width: 44, height: 44)
                                    .background(.regularMaterial, in: Circle())
                                    .shadow(color: .black.opacity(0.12), radius: 6, y: 2)
                            }
                            .buttonStyle(.plain)
                            .padding(.trailing, 16).padding(.bottom, 10)
                            .transition(.opacity)
                            .accessibilityLabel("Latest messages")
                            .accessibilityIdentifier("conversation-latest")
                        }
                    }
                    .pinnedDayPill(pinnedDay, now: dayReference, below: Self.titleHeight)
                    .background(BoardStyle.paper)
                    .task { await store.markRead(bot) }
                    .task(id: "\(focusMessageId ?? ""):\(store.connection.rawValue):\(store.hasOlderMessages(botId))") {
                        guard store.connection == .connected || store.isDemo else { return }
                        while focusMessageId != nil && !focusTargetExists(in: current.messages) && store.hasOlderMessages(botId) && !Task.isCancelled {
                            if !(await store.loadOlderMessages(botId)) { break }
                        }
                    }
                    .task(id: focusTargetExists(in: messages)) {
                        guard let focusTarget, !didFocusDecision, focusTargetExists(in: current.messages) else { return }
                        nearBottom = false
                        await Task.yield()
                        guard !Task.isCancelled else { return }
                        show(TranscriptEntry.rowId(for: focusTarget, in: current.entries), proxy: proxy)
                        didFocusDecision = true
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    ConversationComposer(bot: bot, task: activeTask, text: $messageDraft,
                                         attachments: $messageAttachments, scheduling: $messageScheduling,
                                         scheduledAt: $messageScheduledAt, mentions: $messageMentions,
                                         focused: $composerFocused, replyTo: $replyTo, workContext: $workContext,
                                         onStop: requestStop)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .onAppear { isVisible = true; footerScroll.reset() }
                .onReceive(DaySeparator.changes) { _ in dayReference = Date() }
                .onDisappear { isVisible = false; composerFocused = false; footerScroll.reset() }
                // "Ask the bot about this page" opens the chat with the page quoted in the composer.
                .onChange(of: store.pageQuotes[botId], initial: true) { _, quote in
                    guard let quote else { return }
                    store.pageQuotes[botId] = nil
                    replyTo = quote
                    // The chat follows its latest message. The keyboard opens after the push, because a keyboard that
                    // opens during the push left the latest messages under the composer.
                    nearBottom = true
                    focusAfterPush = UUID()
                }
                // The view cancels this task when it leaves the screen, and a new ID when the request changes.
                .task(id: focusAfterPush) {
                    guard focusAfterPush != nil else { return }
                    try? await Task.sleep(for: .milliseconds(450))
                    guard !Task.isCancelled, focusAfterPush != nil else { return }
                    focusAfterPush = nil
                    composerFocused = true
                }
                .onChange(of: composerFocused) { _, _ in footerScroll.reset() }
                .toolbar {
                    #if os(iOS)
                    if #available(iOS 26.0, *) {
                        ToolbarSpacer(.fixed, placement: .leadingBar)
                    }
                    ToolbarItem(placement: .leadingBar) {
                        NavigationLink(value: BotSettingsRoute(botId: bot.id)) {
                            HStack(spacing: 8) {
                                LiveBotAvatar(bot: bot, size: 28)
                                Text(bot.name).font(.headline)
                                    .lineLimit(1).truncationMode(.tail)
                                    .frame(maxWidth: nameWidth, alignment: .leading)
                            }
                            .fixedSize(horizontal: true, vertical: false)
                            .padding(.leading, 4).padding(.trailing, 10)
                            .frame(minHeight: 44).contentShape(Capsule())
                        }
                        .buttonStyle(.plain).labelStyle(.titleAndIcon).accessibilityLabel("\(bot.name) settings")
                        .accessibilityIdentifier("bot-settings")
                        .simultaneousGesture(TapGesture().onEnded { focusAfterPush = nil; composerFocused = false })
                    }
                    #endif
                }
                #if os(macOS)
                // A Mac toolbar groups its items into one glass bar. The bot gets a pill of its own over the top of the
                // chat instead. Next to an inspector it shows and hides the details; elsewhere it opens the settings.
                .overlay(alignment: .top) {
                    Group {
                        if let toggleBotDetails {
                            Button(action: toggleBotDetails) { botPill(bot) }
                                .keyboardShortcut("i", modifiers: .command)
                                .help("Show or hide the details of \(bot.name)")
                        } else {
                            NavigationLink(value: BotSettingsRoute(botId: bot.id)) { botPill(bot) }
                                .help("Settings of \(bot.name)")
                        }
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 8)
                    .accessibilityLabel("\(bot.name) settings")
                    .accessibilityIdentifier("bot-settings")
                }
                #endif
                .confirmationDialog(StopCopy.title(executor: stop.item.map { executorName($0) } ?? bot.name),
                                    isPresented: $stop.isPresented, titleVisibility: .visible, presenting: stop.item) { task in
                    Button("Stop task", role: .destructive) { Task { await store.cancel(task) } }
                        .accessibilityIdentifier("confirm-stop-task")
                } message: { _ in
                    Text(StopCopy.message)
                }
            } else {
                QuietEmptyState(symbol: "bubble.left", title: "Conversation unavailable", message: "This bot may have been deleted. Return to your team to continue.")
            }
        }
        .inlineNavigationTitle()
        .preference(key: ConversationFooterPreference.self,
                    value: isVisible ? (!composerFocused && footerScroll.allowsMinimization) : nil)
    }

    #if os(macOS)
    private func botPill(_ bot: Bot) -> some View {
        HStack(spacing: 8) {
            LiveBotAvatar(bot: bot, size: 24)
            Text(bot.name).font(.headline).lineLimit(1).truncationMode(.tail)
        }
        .padding(.leading, 6).padding(.trailing, 12)
        .frame(height: 34)
        .modifier(FloatingCapsule())
        .contentShape(Capsule())
    }
    #endif

    @ViewBuilder
    private func conversationHeader(bot: Bot, messages: [Message]) -> some View {
        VStack(spacing: 6) {
            if let error = store.error {
                ErrorBanner(message: error) { store.error = nil }
            }
            if messages.isEmpty {
                VStack(spacing: 10) {
                    BotAvatar(bot: bot, size: 68)
                    Text("Meet \(bot.name)").font(.title2.weight(.semibold))
                    Text("Give \(bot.name) a task. You can leave while the work continues.")
                        .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity).padding(.vertical, 45)
            }
        }.frame(maxWidth: .infinity)
    }



    private func executorName(_ task: AgentTask) -> String { store.botName(task.botId) }

    // Every Stop in the chat asks first, because a stop cannot be undone.
    private func requestStop(_ task: AgentTask) {
        composerFocused = false
        stop.request(task)
    }

    @ViewBuilder
    private func transcriptRow(_ entry: TranscriptEntry, scrollToDecision: @escaping (String) -> Void) -> some View {
        TranscriptMessageRow(message: entry.message, photos: entry.photos, photoMessages: entry.messages,
                             scrollToDecision: scrollToDecision, onReply: { message in
            replyTo = store.messageReference(for: message)
            composerFocused = true
        })
    }
}

/// Scrolling changes the conversation's own state several times per swipe, and each change redraws it.
/// The transcript changes only with the store and with the calendar of the device, so a redraw reuses it. Comparing
/// the inputs still reads them, so the redraw keeps observing the store.
@MainActor
private final class TranscriptCache {
    private struct Inputs: Equatable {
        let botId: String
        let focusDecisionId: String?
        let messages: [Message]
        let outgoingMessages: [OutgoingMessage]
        let outgoingAnswers: [OutgoingAnswer]
        let decisions: [Decision]
        let artifacts: [Artifact]
        let tasks: [AgentTask]
        /// The rows group by the days of this calendar. A new time zone gives a different calendar.
        let calendar: Calendar
    }

    private var inputs: Inputs?
    private var value: (messages: [Message], entries: [TranscriptEntry]) = ([], [])

    func current(in store: AppStore, botId: String, focusDecisionId: String?,
                 calendar: Calendar) -> (messages: [Message], entries: [TranscriptEntry]) {
        let latest = Inputs(botId: botId, focusDecisionId: focusDecisionId, messages: store.messages,
                            outgoingMessages: store.outgoingMessages, outgoingAnswers: store.outgoingAnswers,
                            decisions: store.decisions, artifacts: store.transcriptArtifacts, tasks: store.tasks,
                            calendar: calendar)
        if latest != inputs {
            inputs = latest
            let messages = store.conversationTranscript(for: botId, focusDecisionId: focusDecisionId)
            value = (messages, TranscriptEntry.grouped(messages, botId: botId, calendar: calendar))
        }
        return value
    }
}

struct TranscriptMessageRow: View {
    @Environment(AppStore.self) private var store
    let message: Message
    var photos: [MessagePhoto] = []
    /// The messages of the photos, in the same order.
    var photoMessages: [Message] = []
    var scrollToDecision: (String) -> Void = { _ in }
    /// Takes the message to reply to, because a row of photos shows several messages.
    var onReply: ((Message) -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
        if let senderId = message.senderBotId, let sender = store.bots.first(where: { $0.id == senderId }),
           senderId != message.botId || !(message.recipientBotIds ?? []).isEmpty {
            HStack(spacing: 6) {
                NavigationLink(value: ConversationRoute(botId: sender.id)) {
                    HStack(spacing: 5) {
                        BotAvatar(bot: sender, size: 18)
                        Text(sender.name)
                    }.frame(minHeight: 44)
                }.buttonStyle(.plain).accessibilityIdentifier("message-sender-\(sender.id)")
                if let names = recipientNames, !names.isEmpty {
                    Image(systemName: "arrow.right").imageScale(.small)
                    Text(names).lineLimit(1)
                }
            }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 10)
        } else if let senderId = message.senderBotId, senderId != message.botId {
            Text("Deleted bot").font(.caption).foregroundStyle(.secondary).padding(.horizontal, 10)
        } else if message.role == "user", let names = recipientNames, !names.isEmpty {
            Text("To \(names)").font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .trailing).padding(.horizontal, 14)
        }
        Group {
        if !photos.isEmpty {
            PhotoCarousel(messages: photoMessages, photos: photos, onReply: onReply)
        } else if message.kind == "decision", let id = message.decisionId,
           let decision = store.decisions.first(where: { $0.id == id }) {
            DecisionCard(decision: decision, onViewPrevious: scrollToDecision)
        } else if message.kind == "artifact", let id = message.artifactId,
                  let artifact = store.artifacts.first(where: { $0.id == id }) {
            ArtifactCard(artifact: artifact)
        } else if message.kind == "document", let id = message.documentId {
            DocumentCard(documentId: id, title: message.text)
        } else {
            MessageBubble(message: message, scrollToDecision: scrollToDecision, onReply: onReply.map { reply in { reply(message) } })
        }
        }
        .modifier(MessageTimestamp(createdAt: message.createdAt))
        }
    }

    private var recipientNames: String? {
        TranscriptPresentation.recipientIDs(for: message).map { id in store.bots.first { $0.id == id }?.name ?? "Deleted bot" }.joined(separator: ", ")
    }
}

struct MessageBubble: View {
    @Environment(AppStore.self) private var store
    @State private var editingOutgoing: OutgoingMessage?
    @State private var replyOffset: CGFloat = 0
    @State private var held = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let message: Message
    var scrollToDecision: (String) -> Void = { _ in }
    var onReply: (() -> Void)? = nil
    @Environment(\.openDocument) private var openDocument
    @Environment(\.pushRoute) private var pushRoute
    private var isUser: Bool { message.role == "user" }
    private var displayText: String { message.text.trimmingCharacters(in: .newlines) }

    private func openPage(_ id: String) {
        if let openDocument { openDocument(id) } else { pushRoute?(DocumentRoute(id: id)) }
    }

    private var cardReference: MessageReference? {
        guard isUser, let id = message.decisionId else { return nil }
        if let reference = message.replyTo, reference.messageId == id { return reference }
        guard store.decisions.first(where: { $0.id == id })?.status == "replied" else { return nil }
        return store.decisionReference(for: id)
    }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 36) }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 3) {
            VStack(alignment: .leading, spacing: 4) {
                if let work = message.workContext {
                    Label(work.title, systemImage: "checklist").font(.caption).foregroundStyle(.secondary)
                        .accessibilityIdentifier("message-work-context")
                }
                if let reference = cardReference {
                    MessageQuote(reference: reference)
                        .accessibilityElement(children: .combine)
                        .accessibilityIdentifier("decision-reply-quote-\(message.decisionId ?? message.id)")
                } else if let reference = message.replyTo, let page = reference.documentId {
                    Button { openPage(page) } label: {
                        MessageQuote(reference: reference)
                    }.buttonStyle(.plain).accessibilityLabel("Open the page \(reference.text)")
                        .accessibilityIdentifier("message-page-\(message.id)")
                } else if let reference = message.replyTo {
                    Button { scrollToDecision(reference.messageId) } label: {
                        MessageQuote(reference: reference)
                    }.buttonStyle(.plain).accessibilityLabel("View replied message from \(reference.author)")
                        .accessibilityIdentifier("message-quote-\(message.id)")
                }
                if cardReference == nil, let decisionId = message.decisionId,
                   store.decisions.first(where: { $0.id == decisionId })?.status != "replied" {
                    Button { scrollToDecision(decisionId) } label: {
                        Label("View decision", systemImage: "arrow.turn.up.left")
                            .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                            .frame(minHeight: 44)
                    }.buttonStyle(.plain)
                }
                MessageText(text: displayText, linkColor: isUser ? BoardStyle.userBubbleLink : .blue, allowsTextSelection: false)
                    .accessibilityIdentifier("message-text-\(message.id)")
                if let requestId = message.clientRequestId,
                   store.outgoingAnswers.contains(where: { $0.id == requestId }) {
                    Text("Sending…").font(.caption).foregroundStyle(.secondary)
                }
                if let requestId = message.clientRequestId,
                   let outgoing = store.outgoingMessages.first(where: { $0.id == requestId }) {
                    if outgoing.status == .failed {
                        HStack(spacing: 16) {
                            Button("Not sent · Retry", systemImage: "arrow.clockwise") {
                                Task { await store.retryOutgoing(requestId) }
                            }
                            .accessibilityIdentifier("retry-message-\(requestId)")
                            // A failed message holds the later messages to this bot. Delete lets them go.
                            Button("Delete", role: .destructive) { store.deleteUnsent(requestId) }
                                .accessibilityIdentifier("delete-message-\(requestId)")
                        }
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(minHeight: 44)
                    } else if outgoing.status == .rejected {
                        Button("Not sent · Edit", systemImage: "pencil") { editingOutgoing = outgoing }
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(minHeight: 44).accessibilityIdentifier("edit-message-\(requestId)")
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(isUser ? BoardStyle.userBubble : BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
            #if os(iOS)
            .simultaneousGesture(LongPressGesture(minimumDuration: 0.45).onEnded { _ in
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                // The held message fades in over the chat, without the slide of a full-screen cover.
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { held = true }
            })
            .fullScreenCover(isPresented: $held) {
                HeldMessageView(message: message, onReply: onReply.map { reply in { _ in reply() } }, copyTitle: "Copy",
                                copy: { Pasteboard.copy(message.text) }, share: .text(message.text),
                                trailing: isUser) { maxHeight in
                    // A long message shows its start.
                    MessageText(text: displayText, linkColor: isUser ? BoardStyle.userBubbleLink : .blue,
                                allowsTextSelection: false)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 11)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxHeight: maxHeight, alignment: .top)
                        .background(isUser ? BoardStyle.userBubble : BoardStyle.surface)
                        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
                        .frame(maxWidth: 340, alignment: isUser ? .trailing : .leading)
                }
            }
            #else
            // A Mac shows the actions of a held message in the menu of a secondary click.
            .contextMenu {
                // As the swipe and VoiceOver do, Reply shows only for a saved message, which a reply can quote.
                if let onReply, store.messageReference(for: message) != nil {
                    Button("Reply", systemImage: "arrowshape.turn.up.left") { onReply() }
                }
                Button("Copy", systemImage: "doc.on.doc") { Pasteboard.copy(message.text) }
            }
            #endif
                if !isUser && !message.streaming {
                    LinkPreviewList(text: message.text)
                }
                MessageReactionList(message: message, enabled: onReply != nil)
            }
            if !isUser { Spacer(minLength: 18) }
        }
        .offset(x: replyOffset)
        .background(alignment: .leading) {
            Image(systemName: "arrowshape.turn.up.left.fill").font(.body)
                .foregroundStyle(.secondary).opacity(min(replyOffset / 55, 1))
        }
        #if os(iOS)
        .gesture(MessageReplyGesture(enabled: onReply != nil && store.messageReference(for: message) != nil,
            changed: { replyOffset = min(72, $0 * 0.7) }, ended: { shouldReply in
                if shouldReply { onReply?() }
                withAnimation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.8)) { replyOffset = 0 }
            }))
        #endif
        .accessibilityElement(children: .contain)
        .accessibilityAction(named: "Copy") { Pasteboard.copy(message.text) }
        .accessibilityActions {
            // VoiceOver gets the same Reply and reactions as the long-press menu, and only where that menu has them.
            if onReply != nil, store.messageReference(for: message) != nil {
                let reactions = store.reactions(for: message)
                Button("Reply") { onReply?() }
                ForEach(MessageReactions.selectable, id: \.self) { emoji in
                    Button("\(reactions.contains(emoji) ? "Remove" : "React with") \(MessageReactions.name(emoji))") {
                        Task { await store.react(to: message, emoji: emoji) }
                    }
                }
            }
        }
        .accessibilityLabel("\(isUser ? "You" : store.bots.first { $0.id == (message.senderBotId ?? message.botId) }?.name ?? "Deleted bot")")
        .sheet(item: $editingOutgoing) { RejectedMessageEditor(outgoing: $0) }
    }
}

/// The reactions on a message. A tap removes one.
struct MessageReactionList: View {
    @Environment(AppStore.self) private var store
    let message: Message
    let enabled: Bool

    var body: some View {
        let reactions = store.reactions(for: message)
        if !reactions.isEmpty {
            HStack(spacing: 3) {
                ForEach(MessageReactions.all.filter { reactions.contains($0) }, id: \.self) { emoji in
                    Button { Task { await store.react(to: message, emoji: emoji) } } label: {
                        Text(emoji).font(.caption).frame(minWidth: 44, minHeight: 44)
                    }.buttonStyle(.plain).accessibilityLabel("Remove \(emoji) reaction")
                        .disabled(!enabled)
                }
            }.padding(.horizontal, 5)
                .background(BoardStyle.surface, in: Capsule()).overlay(Capsule().stroke(BoardStyle.line))
                .accessibilityIdentifier("message-reactions-\(message.id)")
        }
    }
}

struct MessageQuote: View {
    let reference: MessageReference
    var body: some View {
        HStack(spacing: 8) {
            Capsule().fill(.secondary).frame(width: 3)
            VStack(alignment: .leading, spacing: 3) {
                if reference.documentId != nil {
                    HStack(spacing: 4) {
                        Image(systemName: "doc.text").imageScale(.small)
                        Text("Library page")
                    }.font(.caption.weight(.semibold))
                } else {
                    Text(reference.author).font(.caption.weight(.semibold))
                }
                Text(reference.text).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 5)
    }
}

private struct RejectedMessageEditor: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let outgoing: OutgoingMessage
    @State private var text: String

    init(outgoing: OutgoingMessage) {
        self.outgoing = outgoing
        _text = State(initialValue: outgoing.request.text)
    }

    var body: some View {
        NavigationStack {
            Form {
                if let reason = outgoing.failureReason { Text(reason).foregroundStyle(.secondary) }
                TextEditor(text: $text).frame(minHeight: 140).accessibilityIdentifier("edit-outgoing-input")
                Button("Send as new message") {
                    let corrected = text
                    Task { await store.replaceRejected(outgoing, text: corrected) }
                    dismiss()
                }
                .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("resend-edited-message")
                Button("Delete message", role: .destructive) {
                    store.deleteUnsent(outgoing.id)
                    dismiss()
                }
                .accessibilityIdentifier("delete-rejected-message")
            }
            .navigationTitle("Edit message").inlineNavigationTitle()
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

struct ConversationComposer: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let bot: Bot
    let task: AgentTask?
    @Binding var text: String
    @State private var importing = false
    @Binding var attachments: [Artifact]
    @State private var uploading = false
    @Binding var scheduling: Bool
    @Binding var scheduledAt: Date
    @Binding var mentions: [SelectedBotMention]
    @State private var selection: TextSelection?
    @FocusState.Binding var focused: Bool
    @Binding var replyTo: MessageReference?
    @Binding var workContext: WorkReference?
    var onStop: (AgentTask) -> Void = { _ in }
    @ScaledMetric(relativeTo: .body) private var sendSize = 34

    private var connected: Bool { store.hasPairing || store.isDemo }
    private var recipients: [SelectedBotMention] { BotMentions.recipients(in: text, selected: mentions, bots: store.bots) }
    private var executorName: String { store.bots.first { $0.id == task?.botId }?.name ?? bot.name }

    var body: some View {
        VStack(spacing: 0) {
            if let work = workContext {
                HStack(spacing: 8) {
                    Label(work.title, systemImage: "checklist")
                        .font(.subheadline).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                    Button { workContext = nil } label: {
                        Image(systemName: "xmark").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                    }.accessibilityLabel("Remove work context")
                }.padding(.leading, 16).padding(.trailing, 4)
                    .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
                    .padding(.horizontal, 12).padding(.bottom, 6)
                    .accessibilityIdentifier("work-context-preview")
            }
            if let reference = replyTo {
                // The chat scrolls under the composer, so the quote needs a surface of its own, as the field has.
                HStack(spacing: 8) {
                    MessageQuote(reference: reference)
                    Button { replyTo = nil } label: {
                        Image(systemName: "xmark").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                    }.accessibilityLabel(reference.documentId == nil ? "Cancel reply" : "Remove the page")
                }
                .padding(.leading, 14).padding(.trailing, 4).padding(.vertical, 2)
                .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous)))
                .padding(.horizontal, 12).padding(.bottom, 6)
                .accessibilityIdentifier("reply-preview")
            }
            BotMentionControls(text: $text, selection: $selection, selected: $mentions, bots: store.bots, focused: focused) { focused = true }
            if !attachments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(attachments) { artifact in
                            Button { attachments.removeAll { $0.id == artifact.id } } label: {
                                Label(artifact.title, systemImage: "xmark.circle.fill")
                                    .font(.caption).padding(9).frame(minHeight: 44)
                                    .background(BoardStyle.surface, in: Capsule())
                            }.accessibilityLabel("Remove attachment \(artifact.title)")
                        }
                    }
                }
            }
            if scheduling {
                DatePicker("Start", selection: $scheduledAt, in: Date()...)
                    .font(.caption)
            }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button("Mention a bot", systemImage: "at") {
                        text += text.isEmpty || text.last?.isWhitespace == true ? "@" : " @"
                        selection = TextSelection(insertionPoint: text.endIndex)
                        focused = true
                    }
                    Button("Attach file", systemImage: "paperclip") { importing = true }
                    if workContext == nil {
                        Button(scheduling ? "Send now" : "Schedule task", systemImage: "clock") {
                            scheduling.toggle()
                        }
                    }
                } label: {
                    Image(systemName: uploading ? "hourglass" : "plus").font(.title3.weight(.medium))
                        .foregroundStyle(.secondary).frame(width: 46, height: 46)
                        .contentShape(Circle())
                }
                #if os(macOS)
                // A Mac draws a menu as a pull-down button with an arrow. The composer shows only its icon.
                .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
                #endif
                .modifier(ComposerSurface(shape: Circle()))
                .disabled(!connected || uploading)
                .accessibilityLabel("More options")
                HStack(alignment: .bottom, spacing: 4) {
                TextField("Message \(bot.name)", text: $text, selection: $selection, axis: .vertical)
                    .lineLimit(1...5).font(.body)
                    #if os(macOS)
                    // The composer draws the box. A Mac text field would draw a second one inside it. Its line is
                    // shorter than on an iPhone, so it gets the height of the buttons next to it.
                    .textFieldStyle(.plain)
                    .frame(minHeight: 20)
                    #endif
                    .padding(.vertical, 12)
                    .focused($focused)
                    #if os(macOS)
                    // On a Mac, Return sends and Shift-Return starts a new line, as in Messages.
                    .onKeyPress(.return, phases: .down) { press in
                        guard !press.modifiers.contains(.shift) else { return .ignored }
                        send()
                        return .handled
                    }
                    #endif
                    .accessibilityIdentifier("message-input")
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Button(action: send) {
                    Image(systemName: scheduling ? "clock.fill" : "arrow.up")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(canSend ? BoardStyle.controlLabel : .secondary)
                        .frame(width: sendSize, height: sendSize)
                        .modifier(SendButtonFill(enabled: canSend))
                        .frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .accessibilityLabel(scheduling ? "Schedule task" : "Send message")
                .accessibilityIdentifier("send-message")
                } else if let task, task.isRunning, connected {
                    Button { onStop(task) } label: {
                        Image(systemName: "stop.fill").font(.footnote)
                            .foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                            .contentShape(Rectangle())
                    }.buttonStyle(.plain).accessibilityLabel("Stop \(executorName)'s task").accessibilityIdentifier("stop-current-task")
                }
                }
                .padding(.leading, 14).padding(.trailing, 4)
                .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: 23, style: .continuous)))
            }
            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .modifier(ComposerBackdrop())
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item]) { result in
            if case .success(let url) = result {
                Task {
                    uploading = true
                    if let artifact = await store.uploadAttachment(bot: bot, url: url) { attachments.append(artifact) }
                    uploading = false
                }
            } else if case .failure(let error) = result {
                store.error = error.localizedDescription
            }
        }
    }

    private var canSend: Bool { connected && !uploading && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func send() {
        guard canSend else { return }
        let submittedText = text
        let submittedWork = workContext
        let submittedReply = replyTo
        let submittedArtifacts = attachments
        let submittedAttachments = submittedArtifacts.map(\.id)
        let submittedScheduling = scheduling
        let submittedDate = scheduledAt
        let submittedRecipients = recipients.map(\.id)
        let submittedMentions = mentions
        text = ""; mentions = []; selection = nil; attachments = []; scheduling = false; replyTo = nil; workContext = nil
        Task {
            let success = await store.send(bot: bot, text: submittedText,
                                           attachments: submittedAttachments,
                                           scheduledAt: submittedScheduling ? ISO8601DateFormatter().string(from: submittedDate) : nil,
                                           mentionedBotIds: submittedRecipients, replyTo: submittedReply, workContext: submittedWork)
            if !success && text.isEmpty {
                text = submittedText; mentions = submittedMentions; attachments = submittedArtifacts
                scheduling = submittedScheduling; scheduledAt = submittedDate; replyTo = submittedReply; workContext = submittedWork
            }
        }
    }
}

/// The field and the plus button of the composer. With Liquid Glass (iOS 26, macOS 26) each is a glass shape that the
/// chat scrolls under; before, a tinted shape with a hairline.
private struct ComposerSurface<S: Shape>: ViewModifier {
    let shape: S
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            content.glassEffect(.regular.interactive(), in: shape)
        } else {
            content
                .background(BoardStyle.surface.opacity(0.45), in: shape)
                .overlay(shape.stroke(BoardStyle.line))
        }
    }
}

/// The fill of the send button. Inside the glass of the composer, a plain fill takes on the glass and looks dimmed, as if
/// the button were off. With Liquid Glass the button has its own glass, tinted in the control color.
private struct SendButtonFill: ViewModifier {
    let enabled: Bool
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *), enabled {
            content.glassEffect(.regular.tint(BoardStyle.control).interactive(), in: Circle())
        } else {
            content.background(enabled ? BoardStyle.control : BoardStyle.ink.opacity(0.07), in: Circle())
        }
    }
}

/// The glass shapes of the composer blend into each other when they come close.
private struct ComposerGlassGroup: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            GlassEffectContainer(spacing: 8) { content }
        } else {
            content
        }
    }
}

/// Behind the composer. Glass shows the chat through it, so only the earlier design paints the page there.
private struct ComposerBackdrop: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            content
        } else {
            content.background(BoardStyle.paper)
        }
    }
}

/// A small floating shape: Liquid Glass on macOS 26 and iOS 26, and a thin material before.
private struct FloatingCapsule: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            content.glassEffect(.regular, in: Capsule())
        } else {
            content.background(.regularMaterial, in: Capsule())
        }
    }
}
