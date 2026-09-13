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
                        LiveBotAvatar(bot: bot, size: 76)
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
        store.messages.filter { $0.botId == bot.id && $0.reactionEvent == nil }.compactMap { $0.createdAt.apiDate }.max()
            ?? bot.createdAt.apiDate ?? .distantPast
    }
}

struct HomePressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.55 : 1)
    }
}

func latestConversationMessage(in messages: [Message]) -> Message? {
    let textMessages = messages.filter {
        $0.kind == "text" && $0.reactionEvent == nil && ["user", "assistant"].contains($0.role)
            && !TranscriptPresentation.isSilenceControl($0)
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

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let bot: Bot
    private var latestMessage: Message? {
        latestConversationMessage(in: store.messages.filter { $0.botId == bot.id })
    }
    private var preview: String {
        guard let message = latestMessage else {
            return bot.lastMessage.isEmpty || bot.lastMessage.trimmingCharacters(in: .whitespacesAndNewlines) == "[SILENT]" || TranscriptPresentation.isLegacySettingsNotice(bot.lastMessage)
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
            LiveBotAvatar(bot: bot, size: 46)
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
