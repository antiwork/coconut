import SwiftUI
import UniformTypeIdentifiers

struct ConversationView: View {
    @Environment(AppStore.self) private var store
    let botId: String
    var focusDecisionId: String? = nil
    @State private var stopOpen = false
    @State private var taskToStop: AgentTask? = nil
    @State private var didFocusDecision = false
    @State private var nearBottom = true
    @FocusState private var composerFocused: Bool

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var messages: [Message] {
        TranscriptPresentation.visibleMessages(store.conversationMessages(for: botId), artifacts: store.artifacts)
    }
    private var waitingForResponse: [AgentTask] {
        guard store.connection == .connected || store.isDemo else { return [] }
        return store.tasks.filter { task in
            task.chatBotId == botId && ["queued", "working", "running"].contains(task.status)
                && !messages.contains { $0.taskId == task.id && $0.streaming && !$0.text.isEmpty }
        }
    }
    private var waitingBotIds: [String] {
        var seen = Set<String>()
        let pending = store.outgoingMessages.filter { $0.botId == botId && $0.expectsResponse }.flatMap(\.executorIds)
        return (waitingForResponse.map(\.botId) + pending).filter { seen.insert($0).inserted }
    }
    private var activeTask: AgentTask? {
        store.tasks.last { $0.chatBotId == botId && ["working", "running"].contains($0.status) }
            ?? store.tasks.last { $0.chatBotId == botId && ["needs_you", "waiting"].contains($0.status) }
    }


    var body: some View {
        Group {
            if let bot {
                ScrollViewReader { proxy in
                    ScrollView {
                        // Measure message heights before applying scroll and keyboard offsets.
                        VStack(alignment: .leading, spacing: 10) {
                            conversationHeader(bot: bot)
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
                            ForEach(TranscriptEntry.grouped(messages, botId: botId)) { entry in
                                let message = entry.message
                                Group {
                                if entry.isExchange {
                                    BotExchangeRow(messages: entry.messages, botId: botId)
                                } else {
                                transcriptRow(message) { id in proxy.scrollTo(id, anchor: .top) }
                                }
                                }
                                    .id(message.kind == "decision" ? (message.decisionId ?? entry.id) : entry.id)
                            }
                            ForEach(store.decisions.filter { decision in
                                store.conversationBotId(for: decision) == botId
                                    && !messages.contains { $0.kind == "decision" && $0.decisionId == decision.id }
                            }) { decision in
                                DecisionCard(decision: decision).id(decision.id)
                            }
                            ForEach(waitingBotIds, id: \.self) { id in
                                BotWorkingIndicator(botId: id)
                            }
                            Color.clear.frame(height: 1).id("conversation-bottom")
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 4)
                        .padding(.bottom, 14)
                    }
                    .accessibilityIdentifier("conversation-transcript")
                    .defaultScrollAnchor(.bottom)
                    .scrollDismissesKeyboard(.interactively)
                    .contentShape(Rectangle())
                    .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                    } action: { _, value in
                        nearBottom = value
                    }
                    .onScrollGeometryChange(for: CGSize.self) { $0.containerSize } action: { _, _ in
                        if nearBottom && focusDecisionId == nil { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: waitingBotIds) { _, _ in
                        if nearBottom { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last?.id) { _, _ in
                        if nearBottom { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last(where: { $0.role == "user" })?.id) { _, messageId in
                        guard messageId != nil else { return }
                        nearBottom = true
                        proxy.scrollTo("conversation-bottom", anchor: .bottom)
                    }
                    .onChange(of: messages.last?.text) { _, _ in
                        if nearBottom { proxy.scrollTo("conversation-bottom", anchor: .bottom) }
                    }
                    .background(BoardStyle.paper)
                    .task { await store.markRead(bot) }
                    .task(id: store.decisions.contains { $0.id == focusDecisionId }) {
                        guard let focusDecisionId, !didFocusDecision,
                              store.decisions.contains(where: { $0.id == focusDecisionId }) else { return }
                        nearBottom = false
                        proxy.scrollTo(focusDecisionId, anchor: .top)
                        didFocusDecision = true
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    ConversationComposer(bot: bot, task: activeTask, focused: $composerFocused)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .onDisappear { composerFocused = false }
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
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private func conversationHeader(bot: Bot) -> some View {
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
                    Text(bot.role).font(.subheadline).foregroundStyle(.secondary)
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
        TranscriptMessageRow(message: message, scrollToDecision: scrollToDecision)
    }
}

struct TranscriptMessageRow: View {
    @Environment(AppStore.self) private var store
    let message: Message
    var scrollToDecision: (String) -> Void = { _ in }

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
        if message.kind == "decision", let id = message.decisionId,
           let decision = store.decisions.first(where: { $0.id == id }) {
            DecisionCard(decision: decision)
        } else if message.kind == "artifact", let id = message.artifactId,
                  let artifact = store.artifacts.first(where: { $0.id == id }) {
            ArtifactCard(artifact: artifact)
        } else if message.kind == "activity" {
            ActivityMessageRow(message: message)
        } else {
            MessageBubble(message: message, scrollToDecision: scrollToDecision)
        }
        }
    }

    private var recipientNames: String? {
        TranscriptPresentation.recipientIDs(for: message).map { id in store.bots.first { $0.id == id }?.name ?? "Deleted bot" }.joined(separator: ", ")
    }
}

struct MessageBubble: View {
    @Environment(AppStore.self) private var store
    @State private var editingOutgoing: OutgoingMessage?
    let message: Message
    var scrollToDecision: (String) -> Void = { _ in }
    private var isUser: Bool { message.role == "user" }
    private var displayText: String { message.text.trimmingCharacters(in: .newlines) }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 36) }
            VStack(alignment: .leading, spacing: 4) {
                if let decisionId = message.decisionId {
                    Button { scrollToDecision(decisionId) } label: {
                        Label("View decision", systemImage: "arrow.turn.up.left")
                            .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                            .frame(minHeight: 44)
                    }.buttonStyle(.plain)
                }
                MessageText(text: displayText, linkColor: isUser ? BoardStyle.userBubbleLink : .blue)
                    .accessibilityIdentifier("message-text-\(message.id)")
                if let requestId = message.clientRequestId,
                   let outgoing = store.outgoingMessages.first(where: { $0.id == requestId }) {
                    if outgoing.status == .failed {
                        Button("Not confirmed. Retry", systemImage: "arrow.clockwise") {
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
            if !isUser { Spacer(minLength: 18) }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(isUser ? "You" : store.bots.first { $0.id == (message.senderBotId ?? message.botId) }?.name ?? "Deleted bot")")
        .accessibilityValue(message.streaming ? "Response is streaming" : "")
        .sheet(item: $editingOutgoing) { RejectedMessageEditor(outgoing: $0) }
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

struct ActivityMessageRow: View {
    let message: Message
    @State private var expanded = false
    private var summary: String { message.text.components(separatedBy: "\n").first ?? message.text }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            Text(message.text).font(.caption.monospaced()).foregroundStyle(.secondary)
                .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 8)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "terminal").font(.caption)
                Text(summary).font(.caption).lineLimit(1)
            }.foregroundStyle(.secondary)
        }
        .tint(.secondary)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .frame(minHeight: 44)
        .accessibilityIdentifier("activity-row")
    }
}

struct ConversationComposer: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let bot: Bot
    let task: AgentTask?
    @State private var text = ""
    @State private var importing = false
    @State private var attachments: [Artifact] = []
    @State private var uploading = false
    @State private var scheduling = false
    @State private var scheduledAt = Date().addingTimeInterval(3600)
    @State private var mentions: [SelectedBotMention] = []
    @State private var selection: TextSelection?
    @FocusState.Binding var focused: Bool

    private var connected: Bool { store.hasPairing || store.isDemo }
    private var recipients: [SelectedBotMention] { BotMentions.recipients(in: text, selected: mentions, bots: store.bots) }
    private var executorName: String { store.bots.first { $0.id == task?.botId }?.name ?? bot.name }

    var body: some View {
        VStack(spacing: 0) {
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
                    Button(scheduling ? "Send now" : "Schedule task", systemImage: "clock") {
                        scheduling.toggle()
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
        let submittedArtifacts = attachments
        let submittedAttachments = submittedArtifacts.map(\.id)
        let submittedScheduling = scheduling
        let submittedDate = scheduledAt
        let submittedRecipients = recipients.map(\.id)
        let submittedMentions = mentions
        text = ""; mentions = []; selection = nil; attachments = []; scheduling = false
        Task {
            let success = await store.send(bot: bot, text: submittedText,
                                           attachments: submittedAttachments,
                                           scheduledAt: submittedScheduling ? ISO8601DateFormatter().string(from: submittedDate) : nil,
                                           recipientBotIds: submittedRecipients)
            if !success && text.isEmpty {
                text = submittedText; mentions = submittedMentions; attachments = submittedArtifacts
                scheduling = submittedScheduling; scheduledAt = submittedDate
            }
        }
    }
}
