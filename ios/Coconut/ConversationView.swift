import SwiftUI
import UniformTypeIdentifiers

struct ConversationView: View {
    @Environment(AppStore.self) private var store
    let botId: String
    var focusDecisionId: String? = nil
    @State private var stopOpen = false
    @State private var taskToStop: AgentTask? = nil
    @State private var nearBottom = true

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var messages: [Message] {
        TranscriptPresentation.visibleMessages(store.messages.filter {
            $0.botId == botId || ($0.isBotExchange && $0.exchangeBotIds.contains(botId))
        }.sorted { $0.createdAt < $1.createdAt }, artifacts: store.artifacts)
    }
    private var waitingForResponse: [AgentTask] {
        guard store.connection == .connected || store.isDemo else { return [] }
        return store.tasks.filter { task in
            task.chatBotId == botId && ["working", "running"].contains(task.status)
                && !messages.contains { $0.taskId == task.id && $0.streaming && !$0.text.isEmpty }
        }
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
                                decision.botId == botId
                                    && !messages.contains { $0.kind == "decision" && $0.decisionId == decision.id }
                            }) { decision in
                                DecisionCard(decision: decision).id(decision.id)
                            }
                            ForEach(waitingForResponse) { task in
                                BotWorkingIndicator(botId: task.botId)
                            }
                            Color.clear.frame(height: 1).id("conversation-bottom")
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 4)
                        .padding(.bottom, 14)
                    }
                    .defaultScrollAnchor(.bottom)
                    .scrollDismissesKeyboard(.interactively)
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                    } action: { _, value in
                        nearBottom = value
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
                    .task {
                        await store.markRead(bot)
                        if let focusDecisionId { proxy.scrollTo(focusDecisionId, anchor: .top) }
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    ConversationComposer(bot: bot, task: activeTask)
                }
                .toolbar {
                    ToolbarItem(placement: .principal) {
                        NavigationLink(value: BotSettingsRoute(botId: bot.id)) {
                        HStack(spacing: 10) {
                            BotAvatar(symbol: bot.avatar, color: bot.color, size: 34)
                                .overlay(alignment: .bottomTrailing) { BotActivityDot(bot: bot) }
                            Text(bot.name).font(.subheadline.weight(.semibold))
                        }.frame(minHeight: 44).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain).accessibilityLabel("\(bot.name) settings")
                        .accessibilityIdentifier("bot-settings")
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
                    BotAvatar(symbol: bot.avatar, color: bot.color, size: 68)
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
                        BotAvatar(symbol: sender.avatar, color: sender.color, size: 18)
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

struct TaskThreadView: View {
    @Environment(AppStore.self) private var store
    let taskId: String
    var focusDecisionId: String? = nil
    @State private var nearBottom = false

    private var rootTask: AgentTask? { store.tasks.first { $0.id == taskId } }
    private var threadId: String { rootTask?.threadId ?? taskId }
    private var latestTask: AgentTask? {
        store.threadTasks(threadId).max { $0.createdAt < $1.createdAt } ?? rootTask
    }
    private var activeTask: AgentTask? {
        let tasks = store.threadTasks(threadId).sorted { $0.createdAt < $1.createdAt }
        return tasks.last { ["working", "running"].contains($0.status) } ?? tasks.last(where: \.isActive)
    }
    private var replyTask: AgentTask? { activeTask ?? latestTask }
    private var bot: Bot? { store.bots.first { $0.id == replyTask?.botId } }
    private var conversationBot: Bot? {
        store.bots.first { $0.id == (rootTask?.conversationBotId ?? rootTask?.botId) }
    }
    private var messages: [Message] {
        TranscriptPresentation.visibleMessages(store.threadMessages(threadId), artifacts: store.artifacts)
    }
    private var waitingForResponse: [AgentTask] {
        guard store.connection == .connected || store.isDemo else { return [] }
        return store.threadTasks(threadId).filter { task in
            ["working", "running"].contains(task.status)
                && !messages.contains { $0.taskId == task.id && $0.streaming && !$0.text.isEmpty }
        }
    }
    private var decisions: [Decision] { store.threadDecisions(threadId) }
    private var subject: String {
        (decisions.first { $0.id == focusDecisionId } ?? decisions.first(where: \.isPending) ?? decisions.first)?.title
            ?? rootTask?.title ?? "Task"
    }

    var body: some View {
        Group {
            if rootTask != nil, let replyTask, let bot {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 10) {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(subject).font(.headline).lineLimit(3)
                                    .fixedSize(horizontal: false, vertical: true)
                                HStack {
                                    let origin = conversationBot ?? bot
                                    NavigationLink(value: ConversationRoute(botId: origin.id)) {
                                        HStack(spacing: 9) {
                                            BotAvatar(symbol: origin.avatar, color: origin.color, size: 26)
                                            Text(origin.name).font(.subheadline.weight(.medium)).foregroundStyle(.secondary)
                                        }.frame(minHeight: 44)
                                    }.buttonStyle(.plain)
                                    Spacer()
                                    StatusLabel(status: replyTask.status)
                                }
                            }.padding(.bottom, 6)
                            if let error = store.error {
                                ErrorBanner(message: error) { store.error = nil }
                            }
                            ForEach(messages) { message in
                                TranscriptMessageRow(message: message) { id in proxy.scrollTo(id, anchor: .top) }
                                    .id(message.kind == "decision" ? (message.decisionId ?? message.id) : message.id)
                            }
                            ForEach(decisions.filter { decision in !messages.contains { $0.kind == "decision" && $0.decisionId == decision.id } }) { decision in
                                DecisionCard(decision: decision).id(decision.id)
                            }
                            ForEach(waitingForResponse) { task in
                                BotWorkingIndicator(botId: task.botId)
                            }
                            Color.clear.frame(height: 1).id("thread-bottom")
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 12)
                        .padding(.bottom, 14)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
                    } action: { _, value in nearBottom = value }
                    .onChange(of: messages.last?.id) { _, _ in
                        if nearBottom { proxy.scrollTo("thread-bottom", anchor: .bottom) }
                    }
                    .onChange(of: messages.last(where: { $0.role == "user" })?.id) { _, messageId in
                        guard messageId != nil else { return }
                        nearBottom = true
                        proxy.scrollTo("thread-bottom", anchor: .bottom)
                    }
                    .onChange(of: messages.last?.text) { _, _ in
                        if nearBottom { proxy.scrollTo("thread-bottom", anchor: .bottom) }
                    }
                    .task {
                        await store.markRead(conversationBot ?? bot)
                        if let focusDecisionId { proxy.scrollTo(focusDecisionId, anchor: .top) }
                    }
                    .safeAreaInset(edge: .bottom, spacing: 0) {
                        ThreadReplyComposer(conversationBot: conversationBot ?? bot, task: replyTask, pending: decisions.filter(\.isPending)) {
                            proxy.scrollTo("thread-bottom", anchor: .bottom)
                        }
                    }
                }
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            NavigationLink("Open \((conversationBot ?? bot).name)", value: ConversationRoute(botId: (conversationBot ?? bot).id))
                            if let activeTask {
                                Button("Stop \(store.bots.first { $0.id == activeTask.botId }?.name ?? "Bot")'s task", systemImage: "stop.circle", role: .destructive) {
                                    Task { await store.cancel(activeTask) }
                                }
                            }
                        } label: {
                            Image(systemName: "ellipsis").frame(width: 44, height: 44)
                        }.accessibilityLabel("Task options")
                    }
                }
            } else {
                QuietEmptyState(symbol: "tray", title: "Task unavailable", message: "Reconnect to your Mac to load this thread.")
            }
        }
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .background(BoardStyle.paper)
    }
}

struct ThreadReplyComposer: View {
    @Environment(AppStore.self) private var store
    let conversationBot: Bot
    let task: AgentTask
    let pending: [Decision]
    let onSent: () -> Void
    @State private var text = ""
    @State private var sending = false
    @State private var importing = false
    @State private var uploading = false
    @State private var attachments: [Artifact] = []
    @State private var contextDecisionId = ""
    @State private var mentions: [SelectedBotMention] = []
    @State private var selection: TextSelection?
    @FocusState private var replyFocused: Bool

    private var connected: Bool { store.hasPairing || store.isDemo }
    private var recipients: [SelectedBotMention] { BotMentions.recipients(in: text, selected: mentions, bots: store.bots) }
    private var selectedDecisionId: String? {
        if pending.count == 1 { return pending.first?.id }
        return pending.contains { $0.id == contextDecisionId } ? contextDecisionId : nil
    }
    private var targetName: String {
        let targetId = pending.first { $0.id == selectedDecisionId }?.botId ?? task.botId
        return store.bots.first { $0.id == targetId }?.name ?? "Bot"
    }
    private var workingBotName: String { store.bots.first { $0.id == task.botId }?.name ?? "Bot" }
    private var targetTask: AgentTask? {
        guard recipients.isEmpty, let selectedDecisionId else { return task }
        guard let decision = pending.first(where: { $0.id == selectedDecisionId }) else { return nil }
        return store.tasks.first { $0.id == decision.taskId }
    }
    private var canSend: Bool {
        connected && !sending && !uploading && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && targetTask != nil && (!recipients.isEmpty || pending.count < 2 || selectedDecisionId != nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            BotMentionControls(text: $text, selection: $selection, selected: $mentions, bots: store.bots, focused: replyFocused) { replyFocused = true }
            if pending.count > 1 && recipients.isEmpty {
                Menu {
                    Picker("Decision to discuss", selection: $contextDecisionId) {
                        Text("Choose a decision").tag("")
                        ForEach(pending) { decision in Text(decision.title).tag(decision.id) }
                    }
                } label: {
                    Label(pending.first(where: { $0.id == contextDecisionId })?.title ?? "Choose a decision to discuss", systemImage: "bubble.left")
                        .font(.caption).frame(minHeight: 44)
                }
            }
            if !attachments.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(attachments) { artifact in
                            Button { attachments.removeAll { $0.id == artifact.id } } label: {
                                Label(artifact.title, systemImage: "xmark.circle.fill")
                                    .font(.caption).padding(9).frame(minHeight: 44).background(BoardStyle.surface, in: Capsule())
                            }.accessibilityLabel("Remove attachment \(artifact.title)")
                        }
                    }
                }
            }
            HStack(alignment: .bottom, spacing: 4) {
                Button { importing = true } label: {
                    Image(systemName: uploading ? "hourglass" : "plus")
                        .font(.system(size: 22)).foregroundStyle(.secondary).frame(width: 44, height: 44)
                }.disabled(!connected || uploading).accessibilityLabel("Attach file")
                HStack(alignment: .bottom, spacing: 4) {
                TextField(connected ? (pending.isEmpty ? "Reply to \(targetName)" : "Ask about this decision…") : "Reconnect to reply", text: $text, selection: $selection, axis: .vertical)
                    .font(.body).lineLimit(1...5).padding(.vertical, 12)
                    .focused($replyFocused)
                    .accessibilityIdentifier("thread-reply-input")
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending {
                Button {
                    guard let submittedTask = targetTask else { return }
                    let submittedText = text
                    let submittedAttachments = attachments.map(\.id)
                    let submittedDecisionId = recipients.isEmpty ? selectedDecisionId : nil
                    let submittedRecipients = recipients.map(\.id)
                    let submittedMentions = mentions
                    sending = true
                    Task {
                        let success = await store.reply(task: submittedTask, text: submittedText,
                                                       attachments: submittedAttachments, decisionId: submittedDecisionId,
                                                       recipientBotIds: submittedRecipients)
                        sending = false
                        if success {
                            if text == submittedText && mentions == submittedMentions { text = ""; mentions = []; selection = nil }
                            attachments.removeAll { submittedAttachments.contains($0.id) }
                            if text.isEmpty && attachments.isEmpty { replyFocused = false }
                            onSent()
                        }
                    }
                } label: {
                    Image(systemName: sending ? "ellipsis" : "arrow.up")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(canSend ? BoardStyle.controlLabel : .secondary)
                        .frame(width: 34, height: 34)
                        .background(canSend ? BoardStyle.control : BoardStyle.ink.opacity(0.07), in: Circle())
                        .frame(width: 44, height: 44)
                }.disabled(!canSend).accessibilityLabel("Send task reply").accessibilityIdentifier("send-thread-reply")
                } else if ["working", "running"].contains(task.status), connected {
                    Button { Task { await store.cancel(task) } } label: {
                        Image(systemName: "stop.fill").font(.system(size: 14))
                            .foregroundStyle(.secondary).frame(width: 44, height: 44)
                    }.accessibilityLabel("Stop \(workingBotName)'s task").accessibilityIdentifier("stop-thread-task")
                }
                }
            }
                .padding(.horizontal, 4)
                .background(BoardStyle.surface.opacity(0.45), in: RoundedRectangle(cornerRadius: 24))
                .overlay(RoundedRectangle(cornerRadius: 24).stroke(BoardStyle.line))
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 8)
        .background(BoardStyle.paper)
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item]) { result in
            switch result {
            case .success(let url):
                Task {
                    uploading = true
                    if let artifact = await store.uploadAttachment(bot: conversationBot, url: url) { attachments.append(artifact) }
                    uploading = false
                }
            case .failure(let error): store.error = error.localizedDescription
            }
        }
    }
}

struct MessageBubble: View {
    @Environment(AppStore.self) private var store
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
                MessageText(text: displayText)
                    .accessibilityIdentifier("message-text-\(message.id)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(isUser ? BoardStyle.ink.opacity(0.09) : BoardStyle.surface, in: RoundedRectangle(cornerRadius: 22))
            if !isUser { Spacer(minLength: 18) }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(isUser ? "You" : store.bots.first { $0.id == (message.senderBotId ?? message.botId) }?.name ?? "Deleted bot")")
        .accessibilityValue(message.streaming ? "Response is streaming" : "")
    }
}

private struct BotWorkingIndicator: View {
    @Environment(AppStore.self) private var store
    let botId: String
    private var bot: Bot? { store.bots.first { $0.id == botId } }

    var body: some View {
        HStack(spacing: 8) {
            if let bot { BotAvatar(symbol: bot.avatar, color: bot.color, size: 26) }
            ProgressView().controlSize(.mini).tint(.secondary)
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
    @State private var mode = "steer"
    @State private var sending = false
    @State private var importing = false
    @State private var attachments: [Artifact] = []
    @State private var uploading = false
    @State private var scheduling = false
    @State private var scheduledAt = Date().addingTimeInterval(3600)
    @State private var mentions: [SelectedBotMention] = []
    @State private var selection: TextSelection?
    @FocusState private var focused: Bool

    private var connected: Bool { store.hasPairing || store.isDemo }
    private var recipients: [SelectedBotMention] { BotMentions.recipients(in: text, selected: mentions, bots: store.bots) }
    private var executorName: String { store.bots.first { $0.id == task?.botId }?.name ?? bot.name }

    var body: some View {
        VStack(spacing: 0) {
            BotMentionControls(text: $text, selection: $selection, selected: $mentions, bots: store.bots, focused: focused) { focused = true }
            if task != nil && recipients.isEmpty && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !scheduling {
                HStack {
                    Menu {
                        Picker("Message behavior", selection: $mode) {
                            Text("Steer \(executorName)'s task").tag("steer")
                            Text("Queue a new task").tag("queue")
                        }
                    } label: {
                        HStack(spacing: 5) {
                            Text(mode == "steer" ? "Continue with \(executorName)" : "New task")
                            Image(systemName: "chevron.down").font(.system(size: 9, weight: .semibold))
                        }.font(.caption).foregroundStyle(.secondary).frame(minHeight: 44)
                    }.accessibilityIdentifier("message-mode")
                    Spacer()
                }
            }
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
                        if scheduling { mode = "queue" }
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
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending {
                Button(action: send) {
                    Image(systemName: sending ? "ellipsis" : scheduling ? "clock.fill" : "arrow.up")
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

    private var canSend: Bool { connected && !sending && !uploading && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func send() {
        let submittedText = text
        let submittedAttachments = attachments.map(\.id)
        let submittedScheduling = scheduling
        let submittedDate = scheduledAt
        let submittedTaskId = task?.id
        let submittedRecipients = recipients.map(\.id)
        let submittedMentions = mentions
        let submittedMode = scheduling || task == nil || !submittedRecipients.isEmpty ? "queue" : mode
        sending = true
        Task {
            let success = await store.send(bot: bot, text: submittedText, mode: submittedMode, taskId: submittedTaskId,
                                           attachments: submittedAttachments,
                                           scheduledAt: submittedScheduling ? ISO8601DateFormatter().string(from: submittedDate) : nil,
                                           recipientBotIds: submittedRecipients)
            if success {
                if text == submittedText && mentions == submittedMentions { text = ""; mentions = []; selection = nil }
                attachments.removeAll { submittedAttachments.contains($0.id) }
                if scheduling == submittedScheduling && scheduledAt == submittedDate { scheduling = false }
            }
            sending = false
        }
    }
}
