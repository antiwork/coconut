import SwiftUI

struct HomeLaunchView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .subheadline) private var pinWidth = 108
    let searchText: String

    private var activeBots: [Bot] {
        store.bots.filter { !$0.archived && matches($0) }.sorted { lastActivity($0) > lastActivity($1) }
    }
    private var pinned: [Bot] {
        activeBots.filter(\.pinned).sorted {
            let first = $0.createdAt.apiDate ?? .distantPast
            let second = $1.createdAt.apiDate ?? .distantPast
            return first == second ? $0.id < $1.id : first < second
        }
    }
    private var conversations: [Bot] { activeBots.filter { !$0.pinned } }
    private var archivedCount: Int { store.bots.filter(\.archived).count }

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                if !pinned.isEmpty {
                    ViewThatFits(in: .horizontal) {
                        pinnedTiles
                        ScrollView(.horizontal, showsIndicators: false) { pinnedTiles }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 24).padding(.bottom, 24)
                }
                NavigationLink(value: HomeDestination.inbox) { HomeInboxRow() }
                    .buttonStyle(HomePressStyle())
                    .accessibilityIdentifier("home-inbox")
                    .padding(.horizontal, 24)
                    .padding(.vertical, 12)
                ForEach(conversations) { bot in
                    NavigationLink(value: ConversationRoute(botId: bot.id)) {
                        BotContactRow(bot: bot)
                            .padding(.vertical, 12)
                            .background(BoardStyle.paper)
                            .contentShape(Rectangle())
                            .contextMenu { botActions(bot) }
                    }
                        .buttonStyle(HomePressStyle())
                        .accessibilityIdentifier("bot-conversation-\(bot.id)")
                        .padding(.horizontal, 24)
                }
                if activeBots.isEmpty && !searchText.isEmpty {
                    Text("No matching conversations").font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 24)
                }
                NavigationLink(value: HomeDestination.archived) {
                    HStack(spacing: 6) {
                        Text("Archived")
                        if archivedCount > 0 { Text("\(archivedCount)") }
                        Image(systemName: "chevron.right").font(.caption)
                    }
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                }
                .buttonStyle(HomePressStyle()).accessibilityIdentifier("archived-conversations")
                .padding(.horizontal, 24).padding(.top, 14)
            }
            .padding(.top, pinned.isEmpty ? 16 : 0)
            .padding(.bottom, 20)
        }
        .background(BoardStyle.paper)
        .refreshable { await store.reconnect() }
    }

    private var pinnedTiles: some View {
        HStack(spacing: 20) {
            ForEach(pinned) { bot in
                NavigationLink(value: ConversationRoute(botId: bot.id)) {
                    VStack(spacing: 9) {
                        BotAvatar(symbol: bot.avatar, color: bot.color, size: 76)
                            .overlay(alignment: .bottomTrailing) { BotActivityDot(bot: bot) }
                        HStack(spacing: 5) {
                            Text(bot.name).font(.subheadline).foregroundStyle(.secondary)
                                .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                                .fixedSize(horizontal: false, vertical: true)
                            if bot.unreadCount > 0 { UnreadMarker(count: bot.unreadCount) }
                        }
                    }
                    .frame(width: pinWidth).frame(minHeight: 110)
                    .background(BoardStyle.paper)
                    .contentShape(Rectangle())
                    .contextMenu { botActions(bot) }
                }
                .buttonStyle(HomePressStyle())
                .accessibilityIdentifier("pinned-bot-\(bot.id)")
            }
        }.padding(.horizontal, 24)
    }

    @ViewBuilder
    private func botActions(_ bot: Bot) -> some View {
        Button(bot.pinned ? "Unpin bot" : "Pin bot", systemImage: bot.pinned ? "pin.slash" : "pin") {
            Task { await store.pinBot(bot, pinned: !bot.pinned) }
        }
        Button("Archive bot", systemImage: "archivebox") { Task { await store.archiveBot(bot) } }
    }

    private func matches(_ bot: Bot) -> Bool {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return true }
        if [bot.name, bot.role, bot.lastMessage].contains(where: { $0.localizedCaseInsensitiveContains(query) }) { return true }
        return store.messages.contains { $0.botId == bot.id && $0.kind == "text" && $0.text.localizedCaseInsensitiveContains(query) }
    }
    private func lastActivity(_ bot: Bot) -> Date {
        store.messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
            ?? bot.createdAt.apiDate ?? .distantPast
    }
}

struct HomePressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.55 : 1)
    }
}

private func latestConversationMessage(in messages: [Message]) -> Message? {
    let textMessages = messages.filter {
        $0.kind == "text" && ["user", "assistant"].contains($0.role)
            && !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    // Older hosts stored this completion event as a text message.
    let content = textMessages.filter {
        !($0.role == "assistant" && $0.text.trimmingCharacters(in: .whitespacesAndNewlines) == "Ready to review.")
    }
    return (content.isEmpty ? textMessages : content).max {
        ($0.createdAt.apiDate ?? .distantPast) < ($1.createdAt.apiDate ?? .distantPast)
    }
}

private struct HomeInboxRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    private var pending: [Decision] { store.decisions.filter(\.isPending) }
    private var preview: String {
        if let decision = InboxPresentation.threads(decisions: pending, tasks: store.tasks).first?.decision {
            let name = store.bots.first { $0.id == decision.botId }?.name ?? "Bot"
            return "\(name): \(decision.title)"
        }
        return "No pending decisions"
    }

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "tray")
                .font(.system(size: 22, weight: .light)).foregroundStyle(.primary)
                .frame(width: 46, height: 46)
                .background(BoardStyle.ink.opacity(0.07), in: RoundedRectangle(cornerRadius: 15))
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text("Inbox").font(.body.weight(.semibold)).foregroundStyle(.primary)
                    Spacer()
                    if !pending.isEmpty {
                        Text("\(pending.count)").font(.caption.weight(.semibold)).foregroundStyle(BoardStyle.accent)
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(BoardStyle.accent.opacity(0.1), in: Capsule())
                    }
                }
                Text(preview).font(.subheadline).foregroundStyle(.secondary)
                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
            }
        }.frame(minHeight: 52)
    }
}

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let bot: Bot
    private var latestMessage: Message? {
        latestConversationMessage(in: store.messages.filter { $0.botId == bot.id })
    }
    private var preview: String {
        guard let message = latestMessage else {
            return bot.lastMessage.isEmpty || TranscriptPresentation.isLegacySettingsNotice(bot.lastMessage)
                ? bot.role : bot.lastMessage
        }
        let sender = message.senderBotId.flatMap { id in id == bot.id ? nil : store.bots.first { $0.id == id }?.name ?? "Deleted bot" }
        let prefix = message.role == "user" ? "You: " : sender.map { $0 + ": " } ?? ""
        return prefix + message.text.replacingOccurrences(of: "\n", with: " ")
    }
    private var lastMessageDate: Date? {
        store.messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
    }

    var body: some View {
        HStack(spacing: 14) {
            BotAvatar(symbol: bot.avatar, color: bot.color, size: 46)
                .overlay(alignment: .bottomTrailing) { BotActivityDot(bot: bot) }
            VStack(alignment: .leading, spacing: 5) {
                if dynamicTypeSize.isAccessibilitySize {
                    name
                    if let lastMessageDate { CompactAgeLabel(date: lastMessageDate).font(.caption).foregroundStyle(.tertiary) }
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        name
                        Spacer(minLength: 8)
                        if let lastMessageDate {
                            CompactAgeLabel(date: lastMessageDate).font(.caption).foregroundStyle(.tertiary).fixedSize()
                        }
                    }
                }
                Text(preview)
                    .font(.subheadline).foregroundStyle(.secondary)
                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
            }
        }
        .frame(minHeight: 52)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(bot.name), \(bot.role), \(bot.status.replacingOccurrences(of: "_", with: " ")), \(preview), \(bot.unreadCount) unread")
    }

    private var name: some View {
        HStack(spacing: 5) {
            Text(bot.name).font(.body.weight(bot.unreadCount > 0 ? .semibold : .medium))
            if bot.unreadCount > 0 { UnreadMarker(count: bot.unreadCount) }
        }
    }
}

private struct UnreadMarker: View {
    let count: Int
    var body: some View {
        Text("\(count)").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            .accessibilityElement().accessibilityLabel("\(count) unread messages")
    }
}

struct BotActivityDot: View {
    @Environment(AppStore.self) private var store
    let bot: Bot
    var body: some View {
        if store.isBotActive(bot) {
            Circle().fill(BoardStyle.green).frame(width: 10, height: 10)
                .overlay(Circle().stroke(BoardStyle.paper, lineWidth: 2))
                .offset(x: 2, y: 2)
                .accessibilityElement().accessibilityLabel("Working")
                .accessibilityIdentifier("bot-active-\(bot.id)")
        }
    }
}

struct ArchivedBotsView: View {
    @Environment(AppStore.self) private var store
    private var bots: [Bot] { store.bots.filter(\.archived) }

    var body: some View {
        List {
            if bots.isEmpty {
                Text("No archived conversations").foregroundStyle(.secondary)
            }
            ForEach(bots) { bot in
                NavigationLink(value: ConversationRoute(botId: bot.id)) { BotContactRow(bot: bot) }
                    .listRowBackground(Color.clear)
                    .swipeActions {
                        Button("Restore", systemImage: "tray.and.arrow.up") { Task { await store.archiveBot(bot) } }
                    }
            }
        }
        .listStyle(.plain).scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle("Archived").navigationBarTitleDisplayMode(.inline)
    }
}

struct DecisionInboxView: View {
    @Environment(AppStore.self) private var store
    private var threads: [PendingDecisionThread] {
        InboxPresentation.threads(decisions: store.decisions, tasks: store.tasks)
    }

    var body: some View {
        List {
            if threads.isEmpty {
                QuietEmptyState(symbol: "tray", title: "No pending decisions", message: "Decisions that need your answer appear here.")
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                    .accessibilityIdentifier("inbox-empty")
            }
            ForEach(threads) { thread in
                NavigationLink(value: ConversationRoute(botId: store.conversationBotId(for: thread.decision), decisionId: thread.decision.id)) {
                    InboxThreadRow(thread: thread)
                }
                .accessibilityIdentifier("inbox-thread-\(thread.id)")
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 12, leading: 20, bottom: 12, trailing: 16))
                .alignmentGuide(.listRowSeparatorLeading) { _ in 46 }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .refreshable { await store.reconnect() }
        .background(BoardStyle.paper)
    }
}

struct InboxThreadRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let thread: PendingDecisionThread
    private var decision: Decision { thread.decision }
    private var bot: Bot? { store.bots.first { $0.id == decision.botId } }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if let bot { BotAvatar(symbol: bot.avatar, color: bot.color, size: 34) }
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline) {
                    Text(bot?.name ?? "Bot").font(.subheadline.weight(.semibold))
                    if thread.decisions.count > 1 {
                        Text("\(thread.decisions.count) decisions").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 8)
                    if ["high", "critical"].contains(decision.urgency) {
                        Image(systemName: "exclamationmark.circle")
                            .font(.caption).foregroundStyle(BoardStyle.accent)
                            .accessibilityLabel("Urgent")
                    }
                    if let date = decision.createdAt.apiDate {
                        CompactAgeLabel(date: date).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
                Text(decision.title).font(.subheadline.weight(.semibold))
                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                    .accessibilityIdentifier("inbox-subject-\(thread.id)")
                Text(decision.explanation)
                    .font(.caption).foregroundStyle(.secondary)
                    .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
            }
        }
        .frame(minHeight: 56, alignment: .top)
    }
}
