import SwiftUI

struct HomeLaunchView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .subheadline) private var pinWidth = 108

    @State private var deleting: Bot?

    var body: some View {
        // One pass over the messages gives the newest activity of each bot. The list sorts by it one time.
        let latest = ChatList.lastMessageDates(store.messages)
        let activeBots = store.bots.filter { !$0.archived }.sorted {
            (latest[$0.id] ?? $0.createdAt.apiDate ?? .distantPast) > (latest[$1.id] ?? $1.createdAt.apiDate ?? .distantPast)
        }
        let pinned = activeBots.filter(\.pinned).sorted {
            let first = $0.createdAt.apiDate ?? .distantPast
            let second = $1.createdAt.apiDate ?? .distantPast
            return first == second ? $0.id < $1.id : first < second
        }
        let conversations = ChatList.items(bots: activeBots.filter { !$0.pinned }, latest: latest,
                                           rooms: store.team?.rooms ?? [])
        return ScrollView {
            LazyVStack(spacing: 0) {
                if !pinned.isEmpty {
                    ViewThatFits(in: .horizontal) {
                        pinnedTiles(pinned)
                        ScrollView(.horizontal, showsIndicators: false) { pinnedTiles(pinned) }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 24).padding(.bottom, 24)
                }
                ForEach(conversations) { item in
                    switch item {
                    case .bot(let bot):
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
                    case .team(let room):
                        // A team has no pin and no delete here. The Team screen has its leave action.
                        NavigationLink(value: TeamChatRoute(roomId: room.room)) {
                            TeamContactRow(room: room)
                                .padding(.vertical, 12)
                                .background(BoardStyle.paper)
                                .contentShape(Rectangle())
                        }
                            .buttonStyle(HomePressStyle())
                            .accessibilityIdentifier("team-chat-\(room.room)")
                            .padding(.horizontal, 24)
                    }
                }
            }
            .padding(.top, pinned.isEmpty ? 16 : 0)
            .padding(.bottom, 20)
        }
        .background(BoardStyle.paper)
        .refreshable { await store.reconnect() }
        .modifier(DeleteBotDialog(bot: $deleting))
    }

    private func pinnedTiles(_ pinned: [Bot]) -> some View {
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
        Button("Delete bot", systemImage: "trash", role: .destructive) { deleting = bot }
    }

}

struct HomePressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.55 : 1)
    }
}

func latestConversationMessage(in messages: [Message]) -> Message? {
    let textMessages = messages.filter {
        $0.kind == "text" && !$0.streaming && $0.reactionEvent == nil && ["user", "assistant"].contains($0.role)
            && !TranscriptPresentation.isSilenceControl($0)
            && !TranscriptPresentation.trimmed($0.text, equals: "")
    }
    // Older hosts stored this completion event as a text message.
    let content = textMessages.filter {
        !($0.role == "assistant" && TranscriptPresentation.trimmed($0.text, equals: "Ready to review."))
    }
    return (content.isEmpty ? textMessages : content).max {
        ($0.createdAt.apiDate ?? .distantPast) < ($1.createdAt.apiDate ?? .distantPast)
    }
}

/// The second line of a home row. A pending card comes first, because the face already shows work.
enum BotRowSubtitle: Equatable {
    case needsYou
    case phase(BotPhase)
    case health(BotHealth)
    case preview

    init(needsYou: Bool, phase: BotPhase?, health: BotHealth? = nil) {
        if needsYou { self = .needsYou } else if let phase { self = .phase(phase) } else if let health { self = .health(health) } else { self = .preview }
    }

    /// VoiceOver reads what the row shows. It reads the unread count only when there is one.
    func accessibilityLabel(bot: Bot, preview: String) -> String {
        var parts = [bot.name]
        switch self {
        case .needsYou: parts.append(StatusLabel.label(for: "needs_you"))
        case .phase(let phase): parts.append(phase.label)
        case .health(let health): parts.append(health.label)
        case .preview: parts += [bot.status.replacingOccurrences(of: "_", with: " "), preview]
        }
        if bot.unreadCount > 0 { parts.append("\(bot.unreadCount) unread") }
        return parts.joined(separator: ", ")
    }
}

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot
    /// The face of the bot. The iPhone list shows a large face; a Mac sidebar shows a smaller one.
    var avatarSize: CGFloat = 46
    private var latestMessage: Message? {
        latestConversationMessage(in: store.messages.filter { $0.botId == bot.id })
    }
    private var preview: String {
        guard let message = latestMessage else {
            return TranscriptPresentation.previewText(
                TranscriptPresentation.previewFallback(bot.lastMessage, messages: store.messages.filter { $0.botId == bot.id }))
        }
        let sender = message.senderBotId.flatMap { id in id == bot.id ? nil : store.bots.first { $0.id == id }?.name ?? "Deleted bot" }
        let prefix = message.role == "user" ? "You: " : sender.map { $0 + ": " } ?? ""
        return prefix + (message.photo?.preview ?? TranscriptPresentation.previewText(message.text))
    }
    private var lastMessageDate: Date? {
        store.messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
    }
    private var phase: BotPhase? { store.botPhase(bot) }

    var body: some View {
        let subtitle = BotRowSubtitle(needsYou: store.botNeedsYou(bot), phase: phase, health: bot.health)
        HStack(spacing: 14) {
            LiveBotAvatar(bot: bot, size: avatarSize)
            VStack(alignment: .leading, spacing: 5) {
                if dynamicTypeSize.isAccessibilitySize {
                    name
                    if let lastMessageDate { CompactAgeLabel(date: lastMessageDate).font(.caption).foregroundStyle(.secondary) }
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        name
                        Spacer(minLength: 8)
                        if let lastMessageDate {
                            CompactAgeLabel(date: lastMessageDate).font(.caption).foregroundStyle(.secondary).fixedSize()
                        }
                    }
                }
                // A pending card or a working phase takes the place of the last message, like a typing notice.
                switch subtitle {
                case .needsYou:
                    StatusLabel(status: "needs_you", compact: true)
                        .transition(.opacity)
                        .accessibilityIdentifier("bot-needs-you-\(bot.id)")
                case .phase(let phase):
                    ShimmerText(text: phase.label).font(.subheadline)
                        .transition(.opacity)
                case .health(let health):
                    Text(health.label)
                        .font(.subheadline).foregroundStyle(BoardStyle.attention)
                        .lineLimit(1)
                        .transition(.opacity)
                        .accessibilityIdentifier("bot-health-\(bot.id)")
                case .preview:
                    Text(preview)
                        .font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                        .transition(.opacity)
                }
            }
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: subtitle)
        }
        .frame(minHeight: 52)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, preview: subtitle == .preview ? preview : ""))
    }

    private var name: some View {
        HStack(spacing: 5) {
            Text(bot.name).font(.body.weight(bot.unreadCount > 0 ? .semibold : .medium))
            if bot.unreadCount > 0 { UnreadMarker(count: bot.unreadCount) }
        }
    }
}

struct UnreadMarker: View {
    let count: Int
    var body: some View {
        Text("\(count)").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            .accessibilityElement().accessibilityLabel("\(count) unread messages")
    }
}

struct BotActivityDot: View {
    @Environment(AppStore.self) private var store
    @ScaledMetric(relativeTo: .caption) private var size = 10
    let bot: Bot
    var body: some View {
        if store.isBotActive(bot) {
            Circle().fill(BoardStyle.positive).frame(width: size, height: size)
                .overlay(Circle().stroke(BoardStyle.paper, lineWidth: 2))
                .offset(x: 2, y: 2)
                .accessibilityElement().accessibilityLabel("Working")
                .accessibilityIdentifier("bot-active-\(bot.id)")
        }
    }
}
