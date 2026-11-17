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
    /// The rows that the list builds (see TranscriptWindow).
    @State private var window = TranscriptWindow()
    /// A press of the control above the window makes a new list. The control shows that it works.
    @State private var growingAbove = false
    /// The moment that the day labels count from. A new day or a new time zone replaces it, so an open chat groups
    /// its rows and writes its labels again at once.
    @State private var dayReference = Date()
    @State private var pinnedDay = PinnedDay()
    /// Slack's "New" line: the newest message that the person had read when this chat opened with unread replies. It
    /// stays while the chat is open, so a reply that comes while the person reads does not move it.
    @State private var unreadMark: UnreadMark?
    #if os(macOS)
    @State private var sentScroll = SentMessageScroll()
    /// The chat runs up under the toolbar of the window, where the title of the bot floats. The first message and the
    /// pinned day pill start below the title.
    private static let titleHeight: CGFloat = 52
    #else
    private static let titleHeight: CGFloat = 0
    #endif
    @FocusState private var composerFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.toggleBotDetails) private var toggleBotDetails
    @Environment(\.showChatDeck) private var showChatDeck
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    #if os(macOS)
    /// The height of the notice of the window above the composer (ChatWindowNotice).
    @State private var noticeHeight: CGFloat = 0
    #endif
    @ScaledMetric(relativeTo: .body) private var nameWidth = 150
    /// The space that the last row of the transcript keeps for the notice of the Mac window.
    private var noticeSpace: CGFloat {
        #if os(macOS)
        noticeHeight
        #else
        0
        #endif
    }

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
    /// The model and the cost of each turn, on its last reply.
    private var replyNotes: [String: String] {
        ReplySpend.notes(messages: current.messages, usage: store.chatUsage[botId]) { model in
            store.modelCatalog?.models.first { $0.id == model }?.name ?? model
        }
    }
    /// The row that the window must hold: the card or the message that the chat opens at.
    private var windowFocus: String? { focusTarget.flatMap { Self.entryId(containing: $0, in: current.entries) } }
    private var activeTask: AgentTask? {
        store.tasks.last { $0.chatBotId == botId && $0.isRunning }
            ?? store.tasks.last { $0.chatBotId == botId && ["needs_you", "waiting"].contains($0.status) }
    }


    @MainActor
    private func loadAndScrollToMessage(_ id: String, proxy: ScrollViewProxy) async {
        nearBottom = false
        // Pages of history from the Mac stay above the window until the window takes the quoted message.
        window.keep(current.entries.map(\.id), focusId: windowFocus)
        while !store.messages.contains(where: { $0.id == id || ($0.kind == "decision" && $0.decisionId == id) }) && store.hasOlderMessages(botId) && !Task.isCancelled {
            if !(await store.loadOlderMessages(botId)) { break }
        }
        let row = TranscriptEntry.rowId(for: store.displayedMessageId(for: id), in: current.entries)
        // The quoted message can be older than the rows that the list holds. Then a new list holds it before the scroll.
        if let entry = Self.entryId(containing: row, in: current.entries) {
            window.include(entry, in: current.entries.map(\.id))
        }
        await Task.yield()
        guard !Task.isCancelled else { return }
        show(row, proxy: proxy)
    }

    /// The control above the window. While the window lacks older rows that the app has, it adds a batch of them;
    /// when the window holds every row, it loads a page from the Mac first. Then the chat shows the row that was first.
    private func loadEarlier(anchor: String?, proxy: ScrollViewProxy) async {
        let ids = current.entries.map(\.id)
        switch window.press(ids, focusId: windowFocus) {
        case .loadPage:
            // The page from the Mac arrives above the window. Then the window takes a batch of it.
            window.keep(ids, focusId: windowFocus)
            await store.loadOlderMessages(botId)
        case .growWindow:
            growingAbove = true
            // A frame with the progress, then the new list.
            try? await Task.sleep(for: .milliseconds(16))
        }
        window.growTop(in: current.entries.map(\.id), focusId: windowFocus)
        await Task.yield()
        growingAbove = false
        if let anchor, let entry = current.entries.first(where: { $0.messages.contains { $0.id == anchor } }) {
            let target = entry.message.kind == "decision" ? (entry.message.decisionId ?? entry.id) : entry.id
            show(target, proxy: proxy)
        }
    }

    /// The tasks that this bot runs for another chat. Each one has a link to that chat above the transcript.
    private var originTasks: [AgentTask] {
        store.tasks.filter { task in
            task.botId == botId && task.sourceBotId == nil && (task.conversationBotId ?? botId) != botId
                && store.bots.contains { $0.id == task.conversationBotId }
        }
    }

    /// The row that shows a message, also a message inside a group of photos or of bot messages.
    private static func entryId(containing id: String, in entries: [TranscriptEntry]) -> String? {
        entries.first { $0.id == id || $0.messages.contains { $0.id == id } }?.id
    }

    /// A sent message waits for the layout of its row, and the other scroll rules wait too (SentMessageScroll). Only on
    /// a Mac.
    private var sentWaits: Bool {
        #if os(macOS)
        sentScroll.waits
        #else
        false
        #endif
    }

    private func sendStarted() {
        #if os(macOS)
        sentScroll.sent()
        #endif
    }

    private func show(_ row: String, proxy: ScrollViewProxy) {
        proxy.scrollTo(row, anchor: .top)
        rowHold.hold(row)
    }

    var body: some View {
        let (messages, entries) = current
        let shown = window.range(of: entries.map(\.id), focusId: windowFocus)
        // The rows that come before the first row of the chat show only when the list holds that first row.
        let startsAtFirstRow = shown.lowerBound == 0
        // A new first row of the window makes a new list, so the list measures every row (see TranscriptWindow).
        let listId = window.isUsed && !shown.isEmpty ? entries[shown.lowerBound].id : nil
        let newLineRow = unreadMark.flatMap { $0.botId == botId ? UnreadMark.firstNewRow(after: $0.messageId, in: entries) : nil }
        Group {
            if let bot {
                ScrollViewReader { proxy in
                    // A lazy stack froze here: rows of very different heights switched between their estimated and
                    // real height on every frame. A list sizes its rows without that loop and stays as fast.
                    List {
                        Group {
                            #if os(macOS)
                            // The Coconut chat leaves room for its bar too.
                            ChatTitleSpace(titleHeight: Self.titleHeight + (bot.coconut ? 44 : 0))
                            #endif
                            conversationHeader(bot: bot, messages: messages)
                            // Older rows that the window does not hold yet come with this control too: a press adds
                            // a batch of them. When the window holds every row, a press loads a page from the Mac.
                            if !startsAtFirstRow || store.hasOlderMessages(botId) {
                                Button {
                                    nearBottom = false
                                    let anchor = entries[shown].first?.message.id
                                    Task { await loadEarlier(anchor: anchor, proxy: proxy) }
                                } label: {
                                    if store.historyLoading.contains(botId) || growingAbove { ProgressView() }
                                    else { Text("Load earlier messages").font(.footnote) }
                                }
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .disabled(store.historyLoading.contains(botId) || growingAbove)
                                .accessibilityIdentifier("load-earlier-messages")
                            }
                            ForEach(startsAtFirstRow ? originTasks : []) { task in
                                if let origin = store.bots.first(where: { $0.id == task.conversationBotId }) {
                                    NavigationLink(value: ConversationRoute(botId: origin.id)) {
                                        Label("In \(origin.name)", systemImage: "arrow.up.left")
                                            .font(.footnote).foregroundStyle(.secondary)
                                            .frame(maxWidth: .infinity, minHeight: 44)
                                    }.buttonStyle(.plain)
                                }
                            }
                            ForEach(entries[shown]) { entry in
                                let message = entry.message
                                let date = message.createdAt.apiDate
                                VStack(spacing: 0) {
                                if entry.startsDay, let date {
                                    DaySeparatorRow(date: date, now: dayReference, row: entry.id, pinned: pinnedDay)
                                }
                                if entry.id == newLineRow {
                                    NewMessagesLine()
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
                                // The helpers that run under the working line: goal, time and current call.
                                if let task = activeTask, task.isRunning { ChatHelpersRow(taskId: task.id).id(task.id) }
                            }
                            // What the chat cost so far. It shows between turns, so the number does not move while
                            // the agent works.
                            if activity == nil, let spend = store.chatUsage[botId], spend.total.tokens > 0 {
                                ChatSpendFooter(usage: spend)
                            }
                            // The row inset and this padding keep 12 points below the last row, and the space of
                            // the notice of the Mac window.
                            Color.clear.frame(height: 1).padding(.bottom, BoardStyle.Space.s + noticeSpace)
                                .id("conversation-bottom")
                        }
                        .listRowInsets(EdgeInsets(top: BoardStyle.Space.xs, leading: BoardStyle.Space.l,
                                                  bottom: BoardStyle.Space.xs, trailing: BoardStyle.Space.l))
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                    }
                    .listStyle(.plain)
                    .id(listId)
                    .buttonStyle(.borderless)
                    .scrollContentBackground(.hidden)
                    .environment(\.defaultMinListRowHeight, 1)
                    .accessibilityIdentifier("conversation-transcript")
                    .modifier(ConversationTimestamps())
                    .environment(\.replySpend, replyNotes)
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
                        if sameViewport && abs(new[1] - old[1]) < 0.5 && abs(new[0] - old[0]) > 0.5 { rowHold.offsetMoved() }
                        #endif
                        let change = zip(old.prefix(2), new.prefix(2)).map { abs($1 - $0) }.max() ?? 0
                        if let row = rowHold.rowToRestore(layoutChange: change, userScrolling: userScrolling, followsBottom: nearBottom) {
                            proxy.scrollTo(row, anchor: .top)
                            // The scroll back to the row is not a move of the reader.
                            rowHold.hold(row)
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
                        if nearBottom && !userScrolling && !sentWaits && (focusTarget == nil || didFocusDecision) {
                            proxy.scrollTo("conversation-bottom", anchor: .bottom)
                        }
                    }
                    .onScrollGeometryChange(for: [CGFloat].self) { [$0.containerSize.height, $0.contentInsets.bottom] } action: { _, _ in
                        // Follow keyboard and composer resizing only when the reader stays at the bottom. The keyboard
                        // and the composer change the bottom inset of the chat, not the size of its container.
                        if !userScrolling && nearBottom && !sentWaits && (focusTarget == nil || didFocusDecision) {
                            proxy.scrollTo("conversation-bottom", anchor: .bottom)
                        }
                    }
                    #if os(macOS)
                    // A sent message moves the chat one time, after the layout of its row.
                    .modifier(SentMessageMove(scroll: $sentScroll, follows: { nearBottom }) {
                        footerScroll.reset()
                        proxy.scrollTo("conversation-bottom", anchor: .bottom)
                    })
                    #endif
                    .onChange(of: nearBottom) { _, following in
                        if following { rowHold.release() }
                    }
                    .onChange(of: activity != nil) { _, working in
                        if !working { Task { await store.loadChatUsage(botId) } }
                        if nearBottom && !userScrolling && !sentWaits { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last?.id) { _, _ in
                        if nearBottom && !sentWaits { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last(where: { $0.role == "user" })?.id) { _, messageId in
                        guard let messageId, messages.last?.id == messageId else { return }
                        nearBottom = true
                        if !sentWaits { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last?.text) { _, _ in
                        if nearBottom && !userScrolling && !sentWaits { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
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
                            // Above the notice of the Mac window, which stands at the bottom of the transcript.
                            .padding(.trailing, 16).padding(.bottom, BoardStyle.Space.s + noticeSpace)
                            .transition(.opacity)
                            .accessibilityLabel("Latest messages")
                            .accessibilityIdentifier("conversation-latest")
                        }
                    }
                    .pinnedDayPill(pinnedDay, now: dayReference, below: Self.titleHeight)
                    .background(BoardStyle.paper)
                    // The "New" line comes from the read mark before this read clears the count: onAppear runs before
                    // the task below starts.
                    .onAppear { if unreadMark?.botId != botId, let mark = UnreadMark(bot: bot) { unreadMark = mark } }
                    .onChange(of: botId) { unreadMark = self.bot.flatMap(UnreadMark.init(bot:)) }
                    // The chat reads each reply that comes while it is open, not only the replies before it opened. Before,
                    // a chat that stayed open on a Mac counted each new reply as unread until the person left and came back.
                    .task(id: bot.unreadCount) { if bot.unreadCount > 0 { await store.markRead(bot) } }
                    // From now on a new message does not push the first row out of the window.
                    .onAppear { window.keep(current.entries.map(\.id), focusId: windowFocus) }
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
                #if os(iOS)
                // The notice stays below the header at each scroll position, over the messages only.
                .modifier(ChatErrorNotice())
                #endif
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    VStack(spacing: 0) {
                        #if os(macOS)
                        // In the Mac window the notice stands above the composer. The last row of the transcript
                        // keeps its space (noticeSpace).
                        ChatWindowNotice(height: $noticeHeight)
                        #endif
                        ConversationComposer(bot: bot, task: activeTask, text: $messageDraft,
                                             attachments: $messageAttachments, scheduling: $messageScheduling,
                                             scheduledAt: $messageScheduledAt, mentions: $messageMentions,
                                             focused: $composerFocused, replyTo: $replyTo, workContext: $workContext,
                                             onStop: requestStop, onSend: sendStarted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .onAppear { isVisible = true; footerScroll.reset() }
                #if os(macOS)
                // A click on a chat in the sidebar puts the cursor in the message field, so the person can type at once.
                // An iPhone keeps the keyboard closed: it would cover the latest messages.
                .task(id: botId) {
                    try? await Task.sleep(for: .milliseconds(150))
                    guard !Task.isCancelled else { return }
                    composerFocused = true
                }
                #endif
                .onReceive(DaySeparator.changes) { _ in dayReference = Date() }
                .task(id: botId) { await store.loadChatUsage(botId) }
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
                // A forwarded message opens the chat with the message quoted in the composer (MessageForward).
                .onChange(of: store.forwardedDrafts[botId], initial: true) { _, forwarded in
                    guard let forwarded else { return }
                    store.forwardedDrafts[botId] = nil
                    messageDraft = messageDraft.isEmpty ? forwarded : messageDraft + "\n\n" + forwarded
                    nearBottom = true
                    focusAfterPush = UUID()
                }
                // A new chat opens with the cursor in its message field (AppStore.newChat).
                .onChange(of: store.composerFocusRequest == botId, initial: true) { _, wanted in
                    guard wanted else { return }
                    store.composerFocusRequest = nil
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
                // The unsent text of each chat stays when the person leaves it.
                .task(id: botId) { messageDraft = ComposerDrafts.read(botId) }
                .onChange(of: messageDraft) { _, draft in ComposerDrafts.write(draft, chat: botId) }
                .toolbar {
                    #if os(iOS)
                    if #available(iOS 26.0, *) {
                        ToolbarSpacer(.fixed, placement: .leadingBar)
                    }
                    ToolbarItem(placement: .leadingBar) {
                        HStack(spacing: 8) {
                            LiveBotAvatar(bot: bot, size: 28)
                            Text(bot.isChat ? bot.chatLabel : bot.name).font(.body.weight(.semibold))
                                .lineLimit(1).truncationMode(.tail)
                                .frame(maxWidth: nameWidth, alignment: .leading)
                        }
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.leading, 4).padding(.trailing, BoardStyle.Space.m)
                        .frame(minHeight: 44).contentShape(Capsule())
                        // A tap opens the settings of the agent. A long press shows the deck of all chats, which a link
                        // cannot tell apart from a tap: it opens its page when the finger lifts after the hold.
                        .onTapGesture {
                            focusAfterPush = nil
                            composerFocused = false
                            pushRoute?(BotSettingsRoute(botId: bot.agentId))
                        }
                        .onLongPressGesture(minimumDuration: 0.4) { showDeck(bot) }
                        .accessibilityElement(children: .combine)
                        .accessibilityAddTraits(.isButton)
                        .accessibilityLabel("\(bot.name) settings")
                        .accessibilityAction(named: "Show all chats") { showDeck(bot) }
                        .accessibilityIdentifier("bot-settings")
                    }
                    // The model and its reasoning are in the composer, under the text (ComposerModelMenu).
                    ToolbarItem(placement: .trailingBar) { NewChatHeaderButton(bot: bot) }
                    #endif
                }
                #if os(macOS)
                // The window moves with a drag in the band above the chat, under the pill.
                .overlay(alignment: .top) { WindowDragStrip() }
                // A Mac toolbar groups its items into one glass bar. The bot gets a pill of its own over the top of the
                // chat instead. Next to an inspector it shows and hides the details; elsewhere it opens the settings.
                .overlay(alignment: .top) {
                    HStack(spacing: BoardStyle.Space.s) {
                        Group {
                            if let toggleBotDetails {
                                Button(action: toggleBotDetails) { botPill(bot) }
                                    .keyboardShortcut("i", modifiers: .command)
                                    .help("Show or hide the details of \(bot.name)")
                            } else {
                                NavigationLink(value: BotSettingsRoute(botId: bot.agentId)) { botPill(bot) }
                                    .help("Settings of \(bot.name)")
                            }
                        }
                        .accessibilityLabel("\(bot.name) settings")
                        .accessibilityIdentifier("bot-settings")
                        NewChatHeaderButton(bot: bot)
                    }
                    .buttonStyle(.plain)
                    .padding(.top, 8)
                }
                #endif
                // The Coconut chat has its bar: where Coconut runs, and the things that need this device.
                .modifier(CoconutChatArea(bot: bot, top: Self.titleHeight + 4))
                .confirmationDialog(StopCopy.title(executor: stop.item.map { executorName($0) } ?? bot.name),
                                    isPresented: $stop.isPresented, titleVisibility: .visible, presenting: stop.item) { task in
                    Button("Stop task", role: .destructive) { Task { await store.cancel(task) } }
                        .accessibilityIdentifier("confirm-stop-task")
                } message: { _ in
                    Text(StopCopy.message)
                }
            } else {
                QuietEmptyState(symbol: "bubble.left", title: "Chat unavailable", message: "This agent may have been deleted. Return to your team to continue.")
            }
        }
        .inlineNavigationTitle()
        .preference(key: ConversationFooterPreference.self,
                    value: isVisible ? (!composerFocused && footerScroll.allowsMinimization) : nil)
        #if os(macOS)
        // A pinch in on the chat shows the deck of all chats, as a pinch on a page of a browser shows its tabs.
        .modifier(ChatDeckPinch(chat: botId) { id in openChat?(id) })
        #endif
    }

    /// Shows the deck of all chats with this chat in the middle. A card of another chat opens that chat.
    private func showDeck(_ bot: Bot) {
        showChatDeck?(ChatDeckRequest(start: bot.id, opened: bot.id) { id in
            if let openChat { openChat(id) } else { pushRoute?(ConversationRoute(botId: id)) }
        })
    }

    #if os(macOS)
    private func botPill(_ bot: Bot) -> some View {
        HStack(spacing: 8) {
            LiveBotAvatar(bot: bot, size: 24)
            Text(bot.name).font(.body.weight(.semibold)).lineLimit(1).truncationMode(.tail)
            // A further chat of the agent shows its title after the name.
            if bot.isChat {
                Text(bot.chatLabel).font(.body).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                    .frame(maxWidth: 260, alignment: .leading).fixedSize(horizontal: true, vertical: false)
            }
        }
        .padding(.leading, BoardStyle.Space.xs).padding(.trailing, BoardStyle.Space.m)
        .frame(height: 34)
        .modifier(FloatingCapsule())
        .contentShape(Capsule())
    }
    #endif

    @ViewBuilder
    private func conversationHeader(bot: Bot, messages: [Message]) -> some View {
        VStack(spacing: BoardStyle.Space.s) {
            if messages.isEmpty {
                VStack(spacing: BoardStyle.Space.m) {
                    BotAvatar(bot: bot, size: 68)
                    Text("Meet \(bot.name)").font(.title3.weight(.semibold))
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

/// The read mark that a chat opened with: the newest message that the person had read. A chat without unread replies,
/// or without a mark, has none.
struct UnreadMark: Equatable {
    let botId: String
    let messageId: String

    init?(bot: Bot) {
        guard bot.unreadCount > 0, let read = bot.lastReadMessageId, !read.isEmpty else { return nil }
        botId = bot.id
        messageId = read
    }

    /// The row that the "New" line goes above: the first row after the row that holds the read message. A mark that
    /// the loaded rows do not hold, or a mark in the last row, draws no line.
    static func firstNewRow(after messageId: String, in entries: [TranscriptEntry]) -> String? {
        guard let index = entries.firstIndex(where: { $0.messages.contains { $0.id == messageId } }),
              entries.indices.contains(index + 1) else { return nil }
        return entries[index + 1].id
    }
}

/// Slack's line above the first message that came since the person last read the chat: a thin green rule and "New".
struct NewMessagesLine: View {
    var body: some View {
        HStack(spacing: BoardStyle.Space.s) {
            Rectangle().fill(BoardStyle.positive).frame(height: 1)
            Text("New").font(.caption.weight(.semibold)).foregroundStyle(BoardStyle.positive)
        }
        .padding(.vertical, BoardStyle.Space.s)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("New messages")
        .accessibilityIdentifier("new-messages-line")
    }
}

struct TranscriptMessageRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.replySpend) private var replySpend
    let message: Message
    var photos: [MessagePhoto] = []
    /// The messages of the photos, in the same order.
    var photoMessages: [Message] = []
    var scrollToDecision: (String) -> Void = { _ in }
    /// Takes the message to reply to, because a row of photos shows several messages.
    var onReply: ((Message) -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
        if let senderId = message.senderBotId, let sender = store.bots.first(where: { $0.id == senderId }),
           senderId != message.botId || !(message.recipientBotIds ?? []).isEmpty {
            HStack(spacing: BoardStyle.Space.s) {
                NavigationLink(value: ConversationRoute(botId: sender.id)) {
                    HStack(spacing: BoardStyle.Space.xs) {
                        BotAvatar(bot: sender, size: 18)
                        Text(sender.name)
                    }.frame(minHeight: 44)
                }.buttonStyle(.plain).accessibilityIdentifier("message-sender-\(sender.id)")
                if let names = recipientNames, !names.isEmpty {
                    Image(systemName: "arrow.right").imageScale(.small)
                    Text(names).lineLimit(1)
                }
            }.font(.footnote).foregroundStyle(.secondary).padding(.horizontal, BoardStyle.Space.m)
        } else if let senderId = message.senderBotId, senderId != message.botId {
            Text("Deleted agent").font(.footnote).foregroundStyle(.secondary).padding(.horizontal, BoardStyle.Space.m)
        } else if message.role == "user", let names = recipientNames, !names.isEmpty {
            Text("To \(names)").font(.footnote).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .trailing).padding(.horizontal, BoardStyle.Space.m)
        }
        Group {
        if !photos.isEmpty {
            PhotoCarousel(messages: photoMessages, photos: photos, trailing: message.role == "user", onReply: onReply)
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
        .modifier(MessageTimestamp(createdAt: message.createdAt, trailing: message.role == "user", note: replySpend[message.id]))
        }
    }

    private var recipientNames: String? {
        TranscriptPresentation.recipientIDs(for: message).map { id in store.bots.first { $0.id == id }?.name ?? "Deleted agent" }.joined(separator: ", ")
    }
}

struct MessageBubble: View {
    @Environment(AppStore.self) private var store
    @State private var editingOutgoing: OutgoingMessage?
    @State private var held = false
    @State private var forwarding = false
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
            VStack(alignment: isUser ? .trailing : .leading, spacing: BoardStyle.Space.xs) {
            VStack(alignment: .leading, spacing: 4) {
                if let work = message.workContext {
                    Label(work.title, systemImage: "checklist").font(.footnote).foregroundStyle(.secondary)
                        .accessibilityIdentifier("message-work-context")
                }
                if let reference = cardReference {
                    MessageQuote(reference: reference)
                        .accessibilityElement(children: .combine)
                        .accessibilityIdentifier("decision-reply-quote-\(message.decisionId ?? message.id)")
                } else if let reference = message.replyTo, let page = reference.documentId {
                    Button { openPage(page) } label: {
                        MessageQuote(reference: reference)
                    }.buttonStyle(.plain).accessibilityLabel(PageQuoteText.open(reference.text))
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
                            .font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
                            .frame(minHeight: 44)
                    }.buttonStyle(.plain)
                }
                MessageText(text: displayText, linkColor: isUser ? BoardStyle.userBubbleLink : .blue, allowsTextSelection: false)
                    .accessibilityIdentifier("message-text-\(message.id)")
                if let requestId = message.clientRequestId,
                   store.outgoingAnswers.contains(where: { $0.id == requestId }) {
                    Text("Sending…").font(.footnote).foregroundStyle(.secondary)
                }
                if let requestId = message.clientRequestId,
                   let outgoing = store.outgoingMessages.first(where: { $0.id == requestId }) {
                    if outgoing.status == .failed {
                        // A failed message holds the later messages to this bot. Delete lets them go.
                        NotSentControls(id: requestId, retry: { Task { await store.retryOutgoing(requestId) } },
                                        delete: { store.deleteUnsent(requestId) })
                    } else if outgoing.status == .rejected {
                        Button("Not sent · Edit", systemImage: "pencil") { editingOutgoing = outgoing }
                            .font(.footnote).foregroundStyle(.secondary)
                            .frame(minHeight: 44).accessibilityIdentifier("edit-message-\(requestId)")
                    }
                }
            }
            .modifier(ChatBubbleBackground(own: isUser))
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
                                trailing: isUser, onForward: canForward ? { forwarding = true } : nil) { maxHeight in
                    HeldTextPreview(text: displayText, own: isUser, maxHeight: maxHeight)
                }
            }
            #else
            // A Mac shows the actions of a held message in the menu of a secondary click.
            .contextMenu { macMenu }
            #endif
                if !isUser && !message.streaming {
                    LinkPreviewList(text: message.text)
                } else if isUser, !store.githubEmbeds.links(in: message.text).isEmpty {
                    // A message of the person shows the GitHub pages that it names, and no other page.
                    LinkPreviewList(text: message.text, githubOnly: true)
                }
                MessageReactionList(message: message, enabled: onReply != nil)
            }
            if !isUser { Spacer(minLength: 18) }
        }
        .modifier(SwipeToReply(enabled: onReply != nil && store.messageReference(for: message) != nil) { onReply?() })
        .accessibilityElement(children: .contain)
        .accessibilityAction(named: "Copy") { Pasteboard.copy(message.text) }
        .accessibilityAction(named: "Forward") { if canForward { forwarding = true } }
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
        .accessibilityLabel(speaker)
        .sheet(item: $editingOutgoing) { RejectedMessageEditor(outgoing: $0) }
        .sheet(isPresented: $forwarding) { ForwardMessageSheet(message: message, fromBotId: message.botId) }
    }

    #if os(macOS)
    @ViewBuilder private var macMenu: some View {
        // As the swipe and VoiceOver do, Reply and the reactions show only for a saved message, which a reply
        // can quote.
        if let onReply, store.messageReference(for: message) != nil {
            let reactions = store.reactions(for: message)
            ReactionMenuRow(emoji: MessageReactions.selectable, marked: reactions.contains,
                            label: { "\(reactions.contains($0) ? "Remove" : "React with") \(MessageReactions.name($0))" }) { emoji in
                Task { await store.react(to: message, emoji: emoji) }
            }
            Button("Reply", systemImage: "arrowshape.turn.up.left") { onReply() }
        }
        Button("Copy", systemImage: "doc.on.doc") { Pasteboard.copy(message.text) }
        if canForward { Button("Forward", systemImage: "arrowshape.turn.up.right") { forwarding = true } }
    }
    #endif

    private var speaker: String {
        if isUser { return "You" }
        let id = message.senderBotId ?? message.botId
        return store.bots.first { $0.id == id }?.name ?? "Deleted agent"
    }

    /// A message with text can be forwarded once it is complete.
    private var canForward: Bool { !message.streaming && !displayText.isEmpty }
}

/// The reactions on a message. A tap removes one.
struct MessageReactionList: View {
    @Environment(AppStore.self) private var store
    let message: Message
    let enabled: Bool

    var body: some View {
        let reactions = store.reactions(for: message)
        if !reactions.isEmpty {
            HStack(spacing: BoardStyle.Space.xs) {
                ForEach(MessageReactions.all.filter { reactions.contains($0) }, id: \.self) { emoji in
                    Button { Task { await store.react(to: message, emoji: emoji) } } label: {
                        Text(emoji).font(.footnote).frame(minWidth: 44, minHeight: 44)
                    }.buttonStyle(.plain).accessibilityLabel("Remove \(emoji) reaction")
                        .disabled(!enabled)
                }
            }.modifier(ReactionCapsule())
                .accessibilityIdentifier("message-reactions-\(message.id)")
        }
    }
}

#if os(macOS)
/// The emoji at the top of the menu of a message on a Mac, in one row. A marked emoji is the one that the person has.
/// The bot chat and the team chats share it.
struct ReactionMenuRow: View {
    let emoji: [String]
    let marked: (String) -> Bool
    /// The VoiceOver label of the item of an emoji.
    let label: (String) -> String
    let pick: (String) -> Void

    var body: some View {
        ControlGroup {
            ForEach(emoji, id: \.self) { emoji in
                // A palette in a menu draws only the image of a label. macOS 27 draws a Text label as an empty cell,
                // and a marked Text item as a check mark. So each emoji goes in as an image, and the system draws the
                // mark of a marked item around it.
                Toggle(isOn: Binding(get: { marked(emoji) }, set: { _ in pick(emoji) })) {
                    Image(nsImage: Self.image(of: emoji))
                }
                .accessibilityLabel(label(emoji))
            }
        }
        .controlGroupStyle(.palette)
    }

    /// An emoji drawn in the center of a square of 26 points. The image is not a template, so the emoji keeps its
    /// colors.
    static func image(of emoji: String) -> NSImage {
        let side: CGFloat = 26
        return NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            let text = NSAttributedString(string: emoji, attributes: [.font: NSFont.systemFont(ofSize: 16)])
            let size = text.size()
            text.draw(at: NSPoint(x: (side - size.width) / 2, y: (side - size.height) / 2))
            return true
        }
    }
}
#endif

/// The look of the reactions under a bubble: a capsule on the surface, with the line. The chat of an own bot and the
/// team chats share it.
struct ReactionCapsule: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(.horizontal, BoardStyle.Space.xs)
            .background(BoardStyle.surface, in: Capsule()).overlay(Capsule().stroke(BoardStyle.line))
    }
}

struct MessageQuote: View {
    let reference: MessageReference
    var body: some View {
        HStack(spacing: 8) {
            Capsule().fill(.secondary).frame(width: 3)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                if reference.isPage {
                    HStack(spacing: BoardStyle.Space.xs) {
                        Image(systemName: "doc.text").imageScale(.small)
                        Text("Library page")
                    }.font(.footnote.weight(.semibold))
                } else if !reference.author.isEmpty {
                    Text(reference.author).font(.footnote.weight(.semibold))
                }
                Text(reference.text).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.fixedSize(horizontal: false, vertical: true).padding(.vertical, BoardStyle.Space.xs)
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
    @State private var pickingPhotos = false
    @State private var takingPhoto = false
    @State private var editLast = 0
    @State private var slashIndex = 0
    @Binding var attachments: [Artifact]
    /// The attachments from the pick until the Mac has them, in the order of the picks.
    @State private var uploads = OrderedUploads<Artifact>()
    @Binding var scheduling: Bool
    @Binding var scheduledAt: Date
    @Binding var mentions: [SelectedBotMention]
    @State private var selection: TextSelection?
    /// One draft sends one time (DraftSend).
    @State private var draftSend = DraftSend()
    @FocusState.Binding var focused: Bool
    @Binding var replyTo: MessageReference?
    @Binding var workContext: WorkReference?
    var onStop: (AgentTask) -> Void = { _ in }
    /// The person sends the draft. It comes before the draft clears.
    var onSend: () -> Void = {}

    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    private var connected: Bool { store.hasPairing || store.isDemo }
    /// A chat of an agent can send its draft to a new chat with the same agent. The Coconut chat has one chat.
    private var canSendInNewChat: Bool { canSend && !scheduling && !bot.coconut && !bot.archived }
    private var recipients: [SelectedBotMention] { BotMentions.recipients(in: text, selected: mentions, bots: store.bots.withoutChats) }
    private var executorName: String { store.bots.first { $0.id == task?.botId }?.name ?? bot.name }

    var body: some View {
        VStack(spacing: 0) {
            // The messages that wait for the next turn of the agent.
            MessageQueueStack(botId: bot.id, editLast: editLast)
            if let work = workContext {
                HStack(spacing: 8) {
                    Label(work.title, systemImage: "checklist")
                        .font(.subheadline).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                    Button { workContext = nil } label: {
                        Image(systemName: "xmark").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                    }.accessibilityLabel("Remove work context")
                }.padding(.leading, 16).padding(.trailing, 4)
                    .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
                    .padding(.horizontal, BoardStyle.Space.m).padding(.bottom, BoardStyle.Space.s)
                    .accessibilityIdentifier("work-context-preview")
            }
            if let reference = replyTo {
                ComposerQuoteBar(reference: reference,
                                 cancelLabel: reference.documentId == nil ? "Cancel reply" : PageQuoteText.remove) { replyTo = nil }
                    .accessibilityIdentifier("reply-preview")
            }
            BotMentionControls(text: $text, selection: $selection, selected: $mentions, bots: store.bots.withoutChats, focused: focused) { focused = true }
            ComposerAttachmentChips(chips: attachments.map {
                ComposerChip(id: $0.id, title: $0.title,
                             image: TranscriptPresentation.isImageName($0.title) ? URL(string: "coconut-file://" + $0.id) : nil)
            }) { id in
                attachments.removeAll { $0.id == id }
            }
            if scheduling {
                DatePicker("Start", selection: $scheduledAt, in: Date()...)
                    .font(.footnote)
            }
            ComposerSlashMenu(suggestions: slashSuggestions, highlighted: slashIndex) { run($0) }
            // One box, as in Codex. A short draft is one line with the plus and Send beside it. When the box opens, the
            // text is on top, and under it are the plus, the model, its reasoning, the access mode, and Send or Stop.
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .bottom, spacing: 0) {
                    if !expanded { plusMenu }
                    TextField("Message \(bot.name)", text: $text, selection: $selection, axis: .vertical)
                        .focused($focused)
                        .modifier(ComposerFieldStyle(send: { send() }, sendInNewChat: canSendInNewChat ? { send(inNewChat: true) } : nil,
                                                     stacked: expanded, arrow: { arrow($0) }))
                        .padding(.horizontal, BoardStyle.Space.m)
                        .accessibilityIdentifier("message-input")
                    if !expanded { sendControls }
                }
                if expanded {
                    HStack(alignment: .center, spacing: 0) {
                        plusMenu
                        ComposerModelMenu(botId: bot.id, showsEffort: false)
                        ComposerEffortMenu(botId: bot.id)
                        ComposerAccessMenu(botId: bot.id)
                        Spacer(minLength: BoardStyle.Space.s)
                        sendControls
                    }
                    .padding(.horizontal, BoardStyle.Space.xs)
                }
            }
            .modifier(ComposerClickArea(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)) {
                focused = true
            })
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)))

            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .modifier(ComposerBackdrop())
        #if os(iOS)
        .fullScreenCover(isPresented: $takingPhoto) {
            CameraPicker { data in
                takingPhoto = false
                guard let data else { return }
                upload {
                    let outcome = await FileFit.fitted(PickedFile(data: data, name: FileFit.photoName(at: .now, ext: "jpg")), removesPosition: false)
                    guard let fitted = outcome.file else { store.error = outcome.text; return nil }
                    return await store.uploadAttachment(bot: bot, data: fitted.data, filename: fitted.name)
                }
            }.ignoresSafeArea()
        }
        #endif
        .modifier(ComposerAttachmentSources(importing: $importing, pickingPhotos: $pickingPhotos,
                                            pasteEnabled: focused && connected) { url in
            upload { await store.uploadAttachment(bot: bot, url: url) }
        } photo: { load in
            upload {
                guard let picked = await load() else { return nil }
                // A photo of the library can be a HEIC image or larger than the limit. The Mac of the person gets it
                // with its metadata, as a file of the file importer.
                let outcome = await FileFit.fitted(picked, removesPosition: false)
                guard let fitted = outcome.file else {
                    store.error = outcome.text
                    return nil
                }
                return await store.uploadAttachment(bot: bot, data: fitted.data, filename: fitted.name)
            }
        } paste: { pasted in
            upload { await store.uploadAttachment(bot: bot, data: pasted.data, filename: pasted.name) }
        } report: { store.error = $0 })
        // Only after a send: a key without a send changes no state.
        .onChange(of: draftKey) { _, draft in if draftSend.sent != nil { draftSend.shows(draft) } }
        // A new character starts the choice of the slash menu at its first row.
        .onChange(of: text) { _, _ in slashIndex = 0 }
    }

    #if os(iOS)
    private let phone = true
    #else
    private let phone = false
    #endif

    /// The box shows its toolbar under the text (ComposerExpansion).
    private var expanded: Bool {
        ComposerExpansion.isOpen(text: text, focused: focused, phone: phone, hasAttachments: !attachments.isEmpty)
    }

    private var canEditLast: Bool { store.messageQueue(bot.id).contains(where: \.canChange) }

    private var slashSuggestions: [ComposerSlash.Suggestion] {
        let choice = ModelChoice(store: store, botId: bot.id)
        return ComposerSlash.suggestions(for: text, models: choice.catalog?.models ?? [], efforts: choice.entry?.reasoningEfforts ?? [],
                                         running: task?.isRunning == true && connected,
                                         canStartChat: !bot.coconut && !bot.archived && connected)
    }

    /// The row of the slash menu that Return runs.
    private var slashChoice: ComposerSlash.Suggestion? {
        let list = slashSuggestions
        return list.isEmpty ? nil : list[min(slashIndex, list.count - 1)]
    }

    /// An arrow key in the field. -1 is up. In the slash menu it moves the row. Up in an empty field edits the last
    /// message of the queue.
    private func arrow(_ step: Int) -> Bool {
        let list = slashSuggestions
        if !list.isEmpty {
            slashIndex = (min(slashIndex, list.count - 1) + step + list.count) % list.count
            return true
        }
        guard step < 0, text.isEmpty, canEditLast else { return false }
        editLast += 1
        return true
    }

    /// Runs a command of the slash menu.
    private func run(_ suggestion: ComposerSlash.Suggestion) {
        let choice = ModelChoice(store: store, botId: bot.id)
        switch suggestion.action {
        case .complete(let next):
            text = next
            selection = TextSelection(insertionPoint: text.endIndex)
            focused = true
        case .newChat:
            text = ""
            Task {
                guard let chat = await store.newChat(with: bot) else { return }
                if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
            }
        case .compact:
            text = ""
            Task { _ = await store.send(bot: bot, text: "/compress") }
        case .stop:
            text = ""
            if let task { onStop(task) }
        case .model(let key):
            text = ""
            if let model = choice.catalog?.models.first(where: { $0.selectionKey == key }) { choice.choose(model) }
        case .effort(let value):
            text = ""
            if let model = choice.entry { choice.choose(model, effort: value) }
        }
    }

    private var plusMenu: some View {
        ComposerPlusMenu(uploading: uploading, enabled: connected, embedded: true) {
            Button("Mention an agent", systemImage: "at") {
                text += text.isEmpty || text.last?.isWhitespace == true ? "@" : " @"
                selection = TextSelection(insertionPoint: text.endIndex)
                focused = true
            }
            AttachMenuItems(importing: $importing, pickingPhotos: $pickingPhotos)
            #if os(iOS)
            if CameraPicker.isAvailable {
                Button("Take photo", systemImage: "camera") { takingPhoto = true }
            }
            #endif
            Button("Paste", systemImage: "doc.on.clipboard") { pasteFromPasteboard() }
            if workContext == nil {
                Button(scheduling ? "Send now" : "Schedule task", systemImage: "clock") {
                    scheduling.toggle()
                }
            }
        }
    }

    /// Stop while the agent works, and Send when there is a draft.
    @ViewBuilder
    private var sendControls: some View {
        if let task, task.isRunning, connected {
            Button { onStop(task) } label: {
                Image(systemName: "stop.fill").font(.footnote)
                    .foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityLabel("Stop \(executorName)'s task").accessibilityIdentifier("stop-current-task")
        }
        if hasContent {
            ComposerSendButton(symbol: scheduling ? "clock.fill" : "arrow.up", enabled: canSend,
                               label: scheduling ? "Schedule task" : "Send message", action: { send() },
                               newChat: canSendInNewChat ? { send(inNewChat: true) } : nil)
        }
    }

    private func pasteFromPasteboard() {
        switch ComposerPaste.read() {
        case .file(let file): upload { await store.uploadAttachment(bot: bot, data: file.data, filename: file.name) }
        case .text(let pasted):
            text += pasted
            selection = TextSelection(insertionPoint: text.endIndex)
            focused = true
        case nil: break
        }
    }

    private var uploading: Bool { uploads.count > 0 }

    /// The upload counts from the pick. The attachments take the order of the picks, also when a later upload ends
    /// first.
    private func upload(_ work: @escaping @MainActor () async -> Artifact?) {
        let id = uploads.start()
        Task {
            let artifact = await work()
            attachments += uploads.finish(id, artifact)
        }
    }

    /// The draft of one send: its text and its attachments, so a second image alone counts as a new draft.
    private var draftKey: String { ([text] + attachments.map(\.id)).joined(separator: "\u{0}") }

    /// Text, or attachments alone: a pasted screenshot can go without words.
    private var hasContent: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty || uploading
    }
    private var canSend: Bool { connected && !uploading && hasContent }

    /// Clears the draft. `sent` is the text that the clear follows: what the person typed after it stays.
    private func clearDraft(after sent: String? = nil) {
        text = sent.map { Self.typed(after: $0, in: text) } ?? ""
        mentions = []; selection = nil; attachments = []; scheduling = false; replyTo = nil; workContext = nil
    }

    /// What the person typed after `sent`, the text of a send, when the draft clears later than the send. A draft that
    /// does not start with the sent text was changed in another way, and nothing of it stays.
    static func typed(after sent: String, in draft: String) -> String {
        draft.hasPrefix(sent) ? String(draft.dropFirst(sent.count)) : ""
    }

    /// Sends the draft. In a new chat, the draft starts a fresh chat with the same agent, which then opens.
    private func send(inNewChat: Bool = false) {
        // A command that the text begins runs instead of sending the text.
        if let choice = slashChoice, attachments.isEmpty { run(choice); return }
        guard canSend, draftSend.begin(draftKey) else { return }
        let submittedText = text
        let submittedWork = workContext
        let submittedReply = replyTo
        let submittedArtifacts = attachments
        let submittedAttachments = submittedArtifacts.map(\.id)
        let submittedScheduling = scheduling
        let submittedDate = scheduledAt
        let submittedRecipients = recipients.map(\.id)
        let submittedMentions = mentions
        onSend()
        #if !os(macOS)
        clearDraft()
        #endif
        Task {
            #if os(macOS)
            // On a Mac the draft clears in the same update as the row of the message comes. When the composer got short
            // first, a chat at its newest row went down by the height that the composer lost, and then up to the row.
            // Keys that the person typed after Return stay.
            clearDraft(after: submittedText)
            #endif
            var target = bot
            if inNewChat {
                guard let chat = await store.newChat(with: bot) else {
                    draftSend.failed()
                    if text.isEmpty { text = submittedText; attachments = submittedArtifacts }
                    return
                }
                target = chat
                if let openChat { openChat(chat.id) } else { pushRoute?(ConversationRoute(botId: chat.id)) }
            }
            let success = await store.send(bot: target, text: submittedText,
                                           attachments: submittedAttachments,
                                           scheduledAt: submittedScheduling ? ISO8601DateFormatter().string(from: submittedDate) : nil,
                                           mentionedBotIds: submittedRecipients, replyTo: submittedReply, workContext: submittedWork)
            guard !success else { return }
            draftSend.failed()
            if text.isEmpty {
                text = submittedText; mentions = submittedMentions; attachments = submittedArtifacts
                scheduling = submittedScheduling; scheduledAt = submittedDate; replyTo = submittedReply; workContext = submittedWork
            }
        }
    }
}

/// One draft sends one time. Keys and clicks that wait in the event queue come to the composer before its view updates.
/// Until that update, the draft binding of the composer gives the draft that it sent, also after the send cleared it.
/// Two Returns then sent the same message two times. A state of the composer gives what the first send wrote.
struct DraftSend: Equatable {
    /// The draft of the last send, until the composer shows another draft.
    private(set) var sent: String?

    /// The person sends `draft`. False when it is the draft of the last send and the composer did not show another
    /// draft after it.
    mutating func begin(_ draft: String) -> Bool {
        guard draft != sent else { return false }
        sent = draft
        return true
    }

    /// The view of the composer updated and shows `draft`. When it is another draft, the person can send again.
    mutating func shows(_ draft: String) {
        if draft != sent { sent = nil }
    }

    /// The send failed. The draft comes back, and the person can send it again.
    mutating func failed() { sent = nil }
}

/// The field and the plus button of the composer. With Liquid Glass (iOS 26, macOS 26) each is a glass shape that the
/// chat scrolls under; before, a tinted shape with a hairline.
struct ComposerSurface<S: Shape>: ViewModifier {
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
struct ComposerGlassGroup: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            GlassEffectContainer(spacing: 8) { content }
        } else {
            content
        }
    }
}

/// Behind the composer. Glass shows the chat through it, so only the earlier design paints the page there.
struct ComposerBackdrop: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            content
        } else {
            content.background(BoardStyle.paper)
        }
    }
}

/// A small floating shape: Liquid Glass on macOS 26 and iOS 26, and a thin material before.
struct FloatingCapsule: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, macOS 26.0, *) {
            content.glassEffect(.regular, in: Capsule())
        } else {
            content.background(.regularMaterial, in: Capsule())
        }
    }
}

/// The bubble of a message: on the right in the fill of the person's messages, on the left in the fill of the page
/// surface. The bot chat and the Team chat share it.
struct ChatBubbleBackground: ViewModifier {
    let own: Bool

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, BoardStyle.Space.m)
            .padding(.vertical, BoardStyle.Space.m)
            .background(own ? BoardStyle.userBubble : BoardStyle.surface,
                        in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
    }
}

/// The controls under a message that did not send: try again with the same request, or remove the message.
struct NotSentControls: View {
    let id: String
    let retry: () -> Void
    let delete: () -> Void

    var body: some View {
        HStack(spacing: 16) {
            Button("Not sent · Retry", systemImage: "arrow.clockwise", action: retry)
                .accessibilityIdentifier("retry-message-\(id)")
            Button("Delete", role: .destructive, action: delete)
                .accessibilityIdentifier("delete-message-\(id)")
        }
        .font(.footnote).foregroundStyle(.secondary)
        .frame(minHeight: 44)
    }
}

/// A quote above the field of a composer, with a way to cancel it. The chat scrolls under the composer, so the quote
/// needs a surface of its own, as the field has.
struct ComposerQuoteBar: View {
    let reference: MessageReference
    let cancelLabel: String
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            MessageQuote(reference: reference)
            Button(action: cancel) {
                Image(systemName: "xmark").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
            }.accessibilityLabel(cancelLabel)
        }
        .padding(.leading, BoardStyle.Space.m).padding(.trailing, BoardStyle.Space.xs).padding(.vertical, BoardStyle.Space.xs)
        .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous)))
        .padding(.horizontal, BoardStyle.Space.m).padding(.bottom, BoardStyle.Space.s)
    }
}

/// The text field of a composer: at most five lines. On a Mac, Return sends and Shift-Return starts a new line, as in
/// Messages.
struct ComposerFieldStyle: ViewModifier {
    let send: () -> Void
    /// Option-Return on a Mac: send the draft in a new chat with the same agent.
    var sendInNewChat: (() -> Void)? = nil
    /// The field sits above a row of controls in the same box: less space under the text.
    var stacked = false
    /// Up-Arrow and Down-Arrow, as -1 and 1. Returns true when it took the key: it moves in the slash menu, or edits the
    /// last queued message from an empty field.
    var arrow: ((Int) -> Bool)? = nil

    func body(content: Content) -> some View {
        content
            .lineLimit(1...5).font(.body)
            #if os(macOS)
            // The composer draws the box. A Mac text field would draw a second one inside it. The field fills the box,
            // and the box is taller than the line, so a click does not need to hit the line itself (ComposerClickArea).
            .textFieldStyle(.plain)
            .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
            .padding(.top, 16).padding(.bottom, stacked ? 4 : 16)
            #else
            .padding(.top, stacked ? 14 : 12).padding(.bottom, stacked ? 2 : 12)
            #endif
            #if os(macOS)
            .onKeyPress(.return, phases: .down) { press in
                guard !press.modifiers.contains(.shift) else { return .ignored }
                if press.modifiers.contains(.option) {
                    guard let sendInNewChat else { return .ignored }
                    sendInNewChat()
                    return .handled
                }
                send()
                return .handled
            }
            #else
            // A keyboard on an iPhone sends with Command-Return. Return alone is a new line.
            .onKeyPress(.return, phases: .down) { press in
                guard press.modifiers.contains(.command) else { return .ignored }
                send()
                return .handled
            }
            #endif
            .onKeyPress(.upArrow, phases: .down) { press in
                guard press.modifiers.isEmpty, let arrow, arrow(-1) else { return .ignored }
                return .handled
            }
            .onKeyPress(.downArrow, phases: .down) { press in
                guard press.modifiers.isEmpty, let arrow, arrow(1) else { return .ignored }
                return .handled
            }
    }
}

/// A click anywhere in the box of a composer puts the cursor in its field. A Mac text field takes clicks only on its
/// line of text, so a click above or below it, or in its padding, did nothing. The area lies behind the field and the
/// buttons, so they keep their own clicks.
///
/// The area is for the Mac only. On an iPhone, its tap gesture took the tap of the Send button, and the message did
/// not send.
struct ComposerClickArea<S: Shape>: ViewModifier {
    let shape: S
    let focus: () -> Void

    func body(content: Content) -> some View {
        #if os(macOS)
        content.background {
            shape.fill(Color.clear).contentShape(shape).onTapGesture(perform: focus)
        }
        #else
        content
        #endif
    }
}

/// The round send button inside the field of a composer.
struct ComposerSendButton: View {
    let symbol: String
    let enabled: Bool
    let label: String
    let action: () -> Void
    /// Send in New Chat, from the menu of the button: a long press on an iPhone, a secondary click on a Mac.
    var newChat: (() -> Void)? = nil
    @ScaledMetric(relativeTo: .body) private var size = 34

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(enabled ? BoardStyle.controlLabel : .secondary)
                .frame(width: size, height: size)
                .modifier(SendButtonFill(enabled: enabled))
                .frame(minWidth: 44, minHeight: 44)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .contextMenu {
            if let newChat {
                Button("Send in New Chat", systemImage: "square.and.pencil", action: newChat)
                    .accessibilityIdentifier("send-in-new-chat")
            }
        }
        .accessibilityLabel(label)
        .accessibilityAction(named: "Send in New Chat") { newChat?() }
        .accessibilityIdentifier("send-message")
    }
}

/// A swipe to the right on a message answers it, with an arrow that shows how far the swipe goes. A Mac has no swipe:
/// there Reply is in the menu of a secondary click.
struct SwipeToReply: ViewModifier {
    let enabled: Bool
    let reply: () -> Void
    @State private var offset: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .offset(x: offset)
            .background(alignment: .leading) {
                Image(systemName: "arrowshape.turn.up.left.fill").font(.body)
                    .foregroundStyle(.secondary).opacity(min(offset / 55, 1))
            }
            #if os(iOS)
            .gesture(MessageReplyGesture(enabled: enabled, changed: { offset = min(72, $0 * 0.7) }, ended: { shouldReply in
                if shouldReply { reply() }
                withAnimation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.8)) { offset = 0 }
            }))
            #endif
    }
}
