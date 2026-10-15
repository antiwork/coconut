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
    @State private var messageAttachments: [Artifact] = []
    @State private var messageScheduling = false
    @State private var messageScheduledAt = Date().addingTimeInterval(3600)
    @State private var messageMentions: [SelectedBotMention] = []
    @State private var taskDetailsOpen = false
    @State private var stopOpen = false
    @State private var taskToStop: AgentTask? = nil
    @State private var didFocusDecision = false
    @State private var nearBottom = true
    @State private var userScrolling = false
    @State private var footerScroll = ConversationFooterScroll()
    @State private var isVisible = false
    @State private var transcript = TranscriptCache()
    @FocusState private var composerFocused: Bool

    init(botId: String, focusDecisionId: String? = nil, focusMessageId: String? = nil, attachedWork: WorkReference? = nil) {
        self.botId = botId
        self.focusDecisionId = focusDecisionId
        self.focusMessageId = focusMessageId
        _workContext = State(initialValue: attachedWork)
    }

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var current: (messages: [Message], entries: [TranscriptEntry]) {
        transcript.current(in: store, botId: botId, focusDecisionId: focusDecisionId)
    }
    private var showsActivity: Bool { store.isConversationResponding(botId) }
    private var activeTask: AgentTask? {
        store.tasks.last { $0.chatBotId == botId && ["working", "running"].contains($0.status) }
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
        proxy.scrollTo(store.displayedMessageId(for: id), anchor: .top)
    }

    var body: some View {
        let (messages, entries) = current
        Group {
            if let bot {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
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
                                            proxy.scrollTo(target, anchor: .top)
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
                                Group {
                                if entry.isExchange {
                                    BotExchangeRow(messages: entry.messages, botId: botId)
                                } else {
                                transcriptRow(message) { id in
                                    Task { await loadAndScrollToMessage(id, proxy: proxy) }
                                }
                                }
                                }
                                    // Keep full text height when the lazy stack resizes or scrolls to a new message.
                                    .fixedSize(horizontal: false, vertical: true)
                                    .id(message.kind == "decision" ? (message.decisionId ?? entry.id) : entry.id)
                            }
                            if showsActivity {
                                BotWorkingIndicator(botId: botId)
                            }
                            Color.clear.frame(height: 1).id("conversation-bottom")
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 4)
                        .padding(.bottom, 14)
                    }
                    .accessibilityIdentifier("conversation-transcript")
                    .modifier(ConversationTimestamps())
                    .defaultScrollAnchor(focusTarget == nil ? .bottom : .top, for: .initialOffset)
                    .defaultScrollAnchor(focusTarget == nil ? .bottom : .top, for: .alignment)
                    .scrollDismissesKeyboard(.interactively)
                    .contentShape(Rectangle())
                    .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                    } action: { _, value in
                        // Only a user scroll changes whether new messages follow the bottom.
                        if userScrolling { nearBottom = value }
                    }
                    .onScrollGeometryChange(for: CGFloat.self) { ConversationFooterScroll.offset(in: $0) } action: { _, offset in
                        footerScroll.scroll(to: offset)
                    }
                    .onScrollPhaseChange { previous, phase, context in
                        footerScroll.phaseChanged(phase, offset: ConversationFooterScroll.offset(in: context.geometry))
                        userScrolling = phase == .interacting || phase == .decelerating
                        if userScrolling || previous == .interacting || previous == .decelerating {
                            let geometry = context.geometry
                            nearBottom = geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                        }
                    }
                    .onScrollGeometryChange(for: CGSize.self) { $0.contentSize } action: { _, _ in
                        if nearBottom && (focusTarget == nil || didFocusDecision) { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onScrollGeometryChange(for: CGSize.self) { $0.containerSize } action: { _, _ in
                        // Follow keyboard and composer resizing only when the reader stays at the bottom.
                        if !userScrolling && nearBottom && (focusTarget == nil || didFocusDecision) {
                            proxy.scrollTo("conversation-bottom", anchor: .bottom)
                        }
                    }
                    .onChange(of: showsActivity) { _, _ in
                        if nearBottom { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
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
                        if nearBottom { footerScroll.reset(); proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
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
                        proxy.scrollTo(focusTarget, anchor: .top)
                        didFocusDecision = true
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    ConversationComposer(bot: bot, task: activeTask, text: $messageDraft,
                                         attachments: $messageAttachments, scheduling: $messageScheduling,
                                         scheduledAt: $messageScheduledAt, mentions: $messageMentions,
                                         focused: $composerFocused, replyTo: $replyTo, workContext: $workContext)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .onAppear { isVisible = true; footerScroll.reset() }
                .onDisappear { isVisible = false; composerFocused = false; footerScroll.reset() }
                .onChange(of: composerFocused) { _, _ in footerScroll.reset() }
                .toolbar {
                    if #available(iOS 26.0, *) {
                        ToolbarSpacer(.fixed, placement: .topBarLeading)
                    }
                    ToolbarItem(placement: .topBarLeading) {
                        NavigationLink(value: BotSettingsRoute(botId: bot.id)) {
                            HStack(spacing: 8) {
                                LiveBotAvatar(bot: bot, size: 28)
                                Text(bot.name).font(.headline)
                                    .lineLimit(1).truncationMode(.tail)
                                    .frame(maxWidth: 150, alignment: .leading)
                            }
                            .fixedSize(horizontal: true, vertical: false)
                            .padding(.leading, 4).padding(.trailing, 10)
                            .frame(height: 44).contentShape(Capsule())
                        }
                        .buttonStyle(.plain).labelStyle(.titleAndIcon).accessibilityLabel("\(bot.name) settings")
                        .accessibilityIdentifier("bot-settings")
                        .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            NavigationLink("Bot settings", value: BotSettingsRoute(botId: bot.id))
                            Button("Task details", systemImage: "list.bullet.rectangle") {
                                taskDetailsOpen = true
                            }.accessibilityIdentifier("chat-task-details")
                            if let activeTask {
                                Button("Stop \(executorName(activeTask))'s task", systemImage: "stop.circle", role: .destructive) {
                                    taskToStop = activeTask
                                    stopOpen = true
                                }
                            }
                        } label: {
                            Image(systemName: "ellipsis").frame(width: 44, height: 44)
                        }.accessibilityLabel("Bot options")
                            .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
                    }
                }
                .confirmationDialog("Stop \(taskToStop.map { executorName($0) } ?? bot.name)'s task?", isPresented: $stopOpen, titleVisibility: .visible) {
                    Button("Stop task", role: .destructive) {
                        if let taskToStop { Task { await store.cancel(taskToStop) } }
                    }
                } message: {
                    Text("The Mac records the stop request. An action already executing can finish before it stops.")
                }
            } else {
                QuietEmptyState(symbol: "bubble.left", title: "Conversation unavailable", message: "This bot may have been deleted. Return to your team to continue.")
            }
        }
        .sheet(isPresented: $taskDetailsOpen) {
            NavigationStack { NativeTaskDetailsView(botId: botId) }
        }
        .navigationBarTitleDisplayMode(.inline)
        .preference(key: ConversationFooterPreference.self,
                    value: isVisible ? (!composerFocused && footerScroll.allowsMinimization) : nil)
    }

    @ViewBuilder
    private func conversationHeader(bot: Bot, messages: [Message]) -> some View {
        VStack(spacing: 6) {
            if let error = store.error {
                ErrorBanner(message: error) { store.error = nil }
            }
            if let date = messages.first?.createdAt.apiDate {
                Text(date.formatted(date: .abbreviated, time: .omitted))
                    .font(.caption2).foregroundStyle(.tertiary)
                    .padding(.vertical, 12)
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



    private func executorName(_ task: AgentTask) -> String {
        store.bots.first { $0.id == task.botId }?.name ?? "Bot"
    }

    @ViewBuilder
    private func transcriptRow(_ message: Message, scrollToDecision: @escaping (String) -> Void) -> some View {
        TranscriptMessageRow(message: message, scrollToDecision: scrollToDecision, onReply: {
            replyTo = store.messageReference(for: message)
            composerFocused = true
        })
    }
}

/// Scrolling changes the conversation's own state several times per swipe, and each change redraws it.
/// The transcript changes only with the store, so a redraw reuses it. Comparing the inputs still reads them,
/// so the redraw keeps observing the store.
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
    }

    private var inputs: Inputs?
    private var value: (messages: [Message], entries: [TranscriptEntry]) = ([], [])

    func current(in store: AppStore, botId: String, focusDecisionId: String?) -> (messages: [Message], entries: [TranscriptEntry]) {
        let latest = Inputs(botId: botId, focusDecisionId: focusDecisionId, messages: store.messages,
                            outgoingMessages: store.outgoingMessages, outgoingAnswers: store.outgoingAnswers,
                            decisions: store.decisions, artifacts: store.transcriptArtifacts, tasks: store.tasks)
        if latest != inputs {
            inputs = latest
            let messages = store.conversationTranscript(for: botId, focusDecisionId: focusDecisionId)
            value = (messages, TranscriptEntry.grouped(messages, botId: botId))
        }
        return value
    }
}

struct TranscriptMessageRow: View {
    @Environment(AppStore.self) private var store
    let message: Message
    var scrollToDecision: (String) -> Void = { _ in }
    var onReply: (() -> Void)? = nil

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
                    Image(systemName: "arrow.right").font(.system(size: 10))
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
        if message.kind == "decision", let id = message.decisionId,
           let decision = store.decisions.first(where: { $0.id == id }) {
            DecisionCard(decision: decision, onViewPrevious: scrollToDecision)
        } else if message.kind == "artifact", let id = message.artifactId,
                  let artifact = store.artifacts.first(where: { $0.id == id }) {
            ArtifactCard(artifact: artifact)
        } else {
            MessageBubble(message: message, scrollToDecision: scrollToDecision, onReply: onReply)
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
    @State private var actionsOpen = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let message: Message
    var scrollToDecision: (String) -> Void = { _ in }
    var onReply: (() -> Void)? = nil
    private var isUser: Bool { message.role == "user" }
    private var displayText: String { message.text.trimmingCharacters(in: .newlines) }
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
                        Button("Waiting to send. Retry", systemImage: "arrow.clockwise") {
                            Task { await store.retryOutgoing(requestId) }
                        }
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(minHeight: 44).accessibilityIdentifier("retry-message-\(requestId)")
                    } else if outgoing.status == .rejected {
                        Button("Not sent. Edit", systemImage: "pencil") { editingOutgoing = outgoing }
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(minHeight: 44).accessibilityIdentifier("edit-message-\(requestId)")
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(isUser ? BoardStyle.userBubble : BoardStyle.surface, in: RoundedRectangle(cornerRadius: 22))
            .simultaneousGesture(LongPressGesture(minimumDuration: 0.45).onEnded { _ in
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                actionsOpen = true
            })
            .popover(isPresented: $actionsOpen, arrowEdge: .bottom) {
                VStack(spacing: 8) {
                    if onReply != nil, store.messageReference(for: message) != nil {
                        HStack(spacing: 4) {
                            ForEach(MessageReactions.selectable.indices, id: \.self) { reactionButton($0) }
                        }
                        Divider()
                        Button { actionsOpen = false; onReply?() } label: {
                            Label("Reply", systemImage: "arrowshape.turn.up.left").frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        }
                    }
                    Button { UIPasteboard.general.string = message.text; actionsOpen = false } label: {
                        Label("Copy", systemImage: "doc.on.doc").frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    }
                }.buttonStyle(.plain).padding(12).frame(width: 280)
                    .presentationCompactAdaptation(.popover)
            }
                let reactions = store.reactions(for: message)
                if !reactions.isEmpty {
                    HStack(spacing: 3) {
                        ForEach(MessageReactions.all.filter { reactions.contains($0) }, id: \.self) { emoji in
                            Button { Task { await store.react(to: message, emoji: emoji) } } label: {
                                Text(emoji).font(.caption).frame(minWidth: 28, minHeight: 32)
                            }.buttonStyle(.plain).accessibilityLabel("Remove \(emoji) reaction")
                                .disabled(onReply == nil)
                        }
                    }.padding(.horizontal, 5)
                        .background(BoardStyle.surface, in: Capsule()).overlay(Capsule().stroke(BoardStyle.line))
                        .accessibilityIdentifier("message-reactions-\(message.id)")
                }
            }
            if !isUser { Spacer(minLength: 18) }
        }
        .offset(x: replyOffset)
        .background(alignment: .leading) {
            Image(systemName: "arrowshape.turn.up.left.fill").font(.body)
                .foregroundStyle(.secondary).opacity(min(replyOffset / 55, 1))
        }
        .gesture(MessageReplyGesture(enabled: onReply != nil && store.messageReference(for: message) != nil,
            changed: { replyOffset = min(72, $0 * 0.7) }, ended: { shouldReply in
                if shouldReply { onReply?() }
                withAnimation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.8)) { replyOffset = 0 }
            }))
        .accessibilityAction(named: "Reply") { onReply?() }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(isUser ? "You" : store.bots.first { $0.id == (message.senderBotId ?? message.botId) }?.name ?? "Deleted bot")")
        .sheet(item: $editingOutgoing) { RejectedMessageEditor(outgoing: $0) }
    }
    private func reactionButton(_ index: Int) -> some View {
        let emoji = MessageReactions.selectable[index]
        return Button { actionsOpen = false; Task { await store.react(to: message, emoji: emoji) } } label: {
            Text(emoji).font(.system(size: 30)).frame(maxWidth: .infinity, minHeight: 56)
                .background(store.reactions(for: message).contains(emoji) ? Color.primary.opacity(0.1) : .clear, in: RoundedRectangle(cornerRadius: 12))
        }.accessibilityLabel(MessageReactions.names[index])
    }
}

struct MessageQuote: View {
    let reference: MessageReference
    var body: some View {
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 2).fill(.secondary).frame(width: 3)
            VStack(alignment: .leading, spacing: 3) {
                Text(reference.author).font(.caption.weight(.semibold))
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
            }
            .navigationTitle("Edit message").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

private struct BotWorkingIndicator: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let botId: String
    private var bot: Bot? { store.bots.first { $0.id == botId } }

    var body: some View {
        HStack(spacing: 8) {
            if let bot {
                BotAvatar(bot: bot, size: 38, isThinking: true)
                    .overlay(alignment: .bottomTrailing) {
                        if reduceMotion { BotActivityDot(bot: bot).padding(38 * 0.12) }
                    }
            }
        }
        .frame(minHeight: 44).padding(.horizontal, 10)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(bot?.name ?? "Bot") is working")
        .accessibilityIdentifier("bot-working-\(botId)")
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
                        Image(systemName: "xmark").foregroundStyle(.secondary).frame(width: 44, height: 44)
                    }.accessibilityLabel("Remove work context")
                }.padding(.leading, 16).padding(.trailing, 4)
                    .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 16))
                    .padding(.horizontal, 12).padding(.bottom, 6)
                    .accessibilityIdentifier("work-context-preview")
            }
            if let reference = replyTo {
                HStack(spacing: 8) {
                    MessageQuote(reference: reference)
                    Button { replyTo = nil } label: {
                        Image(systemName: "xmark").foregroundStyle(.secondary).frame(width: 44, height: 44)
                    }.accessibilityLabel("Cancel reply")
                }.padding(.horizontal, 12).accessibilityIdentifier("reply-preview")
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
            HStack(alignment: .bottom, spacing: 4) {
                Menu {
                    Button("Mention a bot", systemImage: "at") {
                        text += text.isEmpty || text.last?.isWhitespace == true ? "@" : " @"
                        selection = TextSelection(insertionPoint: text.endIndex)
                        focused = true
                    }
                    Button("Attach file", systemImage: "paperclip") { importing = true }
                    if let task {
                        Button("Stop \(executorName)'s task", systemImage: "stop.circle", role: .destructive) {
                            Task { await store.cancel(task) }
                        }
                    }
                    if workContext == nil {
                        Button(scheduling ? "Send now" : "Schedule task", systemImage: "clock") {
                            scheduling.toggle()
                        }
                    }
                } label: {
                    Image(systemName: uploading ? "hourglass" : "plus").font(.system(size: 22))
                        .foregroundStyle(.secondary).frame(width: 44, height: 44)
                }
                .disabled(!connected || uploading)
                .accessibilityLabel("Add attachment or schedule")
                HStack(alignment: .bottom, spacing: 4) {
                TextField("Message \(bot.name)", text: $text, selection: $selection, axis: .vertical)
                    .lineLimit(1...5).font(.body)
                    .padding(.vertical, 12)
                    .focused($focused)
                    .accessibilityIdentifier("message-input")
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Button(action: send) {
                    Image(systemName: scheduling ? "clock.fill" : "arrow.up")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(canSend ? BoardStyle.controlLabel : .secondary)
                        .frame(width: 34, height: 34)
                        .background(canSend ? BoardStyle.control : BoardStyle.ink.opacity(0.07), in: Circle())
                        .frame(width: 44, height: 44)
                }
                .disabled(!canSend)
                .accessibilityLabel(scheduling ? "Schedule task" : "Send message")
                .accessibilityIdentifier("send-message")
                } else if let task, ["working", "running"].contains(task.status), connected {
                    Button { Task { await store.cancel(task) } } label: {
                        Image(systemName: "stop.fill").font(.system(size: 14))
                            .foregroundStyle(.secondary).frame(width: 44, height: 44)
                    }.accessibilityLabel("Stop \(executorName)'s task").accessibilityIdentifier("stop-current-task")
                }
                }
            }
                .padding(.horizontal, 4)
                .background(BoardStyle.surface.opacity(0.45), in: RoundedRectangle(cornerRadius: 24))
                .overlay(RoundedRectangle(cornerRadius: 24).stroke(BoardStyle.line))
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .background(BoardStyle.paper)
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
