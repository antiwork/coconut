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
        let pinnedBots = activeBots.filter(\.pinned).sorted {
            let first = $0.createdAt.apiDate ?? .distantPast
            let second = $1.createdAt.apiDate ?? .distantPast
            return first == second ? $0.id < $1.id : first < second
        }
        let rooms = store.team?.rooms ?? []
        let filter = ChatFilter.effective(store.chatFilter, rooms: rooms)
        let conversations = filter.items(ChatList.items(bots: activeBots.filter { !$0.pinned }, latest: latest,
                                                        rooms: rooms, local: store.newestLocalMessages))
        let pinned = filter.showsPinned ? pinnedBots : []
        return ScrollView {
            LazyVStack(spacing: 0) {
                // The chips show only for a Mac in a team. A person with bots only sees the list as before.
                if ChatFilter.showsChips(rooms: rooms) {
                    chips(filter)
                }
                if conversations.isEmpty && pinned.isEmpty && filter == .bots {
                    // The quiet line of the Team screen for a Mac without bots.
                    Text("This Mac has no bots yet.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, 45)
                        .accessibilityIdentifier("chats-empty")
                }
                if !pinned.isEmpty {
                    ViewThatFits(in: .horizontal) {
                        pinnedTiles(pinned)
                        ScrollView(.horizontal, showsIndicators: false) { pinnedTiles(pinned) }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, BoardStyle.Space.xl)
                }
                ForEach(conversations) { item in
                    switch item {
                    case .bot(let bot):
                        NavigationLink(value: ConversationRoute(botId: bot.id)) {
                            BotContactRow(bot: bot)
                                .padding(.vertical, BoardStyle.Space.m)
                                .background(BoardStyle.paper)
                                .contentShape(Rectangle())
                                .contextMenu { botActions(bot) }
                        }
                            .buttonStyle(HomePressStyle())
                            .accessibilityIdentifier("bot-conversation-\(bot.id)")
                            .padding(.horizontal, BoardStyle.Space.l)
                    case .team, .direct:
                        // A team and a direct chat have no pin and no delete here. The Team screen has the leave
                        // action.
                        if let chat = item.chat {
                            NavigationLink(value: TeamChatRoute(chat: chat)) {
                                PeopleRow(item: item)
                                    .padding(.vertical, BoardStyle.Space.m)
                                    .background(BoardStyle.paper)
                                    .contentShape(Rectangle())
                            }
                                .buttonStyle(HomePressStyle())
                                .accessibilityIdentifier(Self.identifier(chat))
                                .padding(.horizontal, BoardStyle.Space.l)
                        }
                    }
                }
            }
            .padding(.top, pinned.isEmpty || ChatFilter.showsChips(rooms: rooms) ? BoardStyle.Space.l : 0)
            .padding(.bottom, BoardStyle.Space.xl)
        }
        .background(BoardStyle.paper)
        .refreshable { await store.reconnect() }
        .modifier(DeleteBotDialog(bot: $deleting))
    }

    /// The row of filter chips, as the chips of WhatsApp. When large text does not fit one row, the chips stand one
    /// under the other, so no chip is cut.
    private func chips(_ selected: ChatFilter) -> some View {
        let chips = ForEach(ChatFilter.allCases) { filter in
            ChipButton(title: filter.title, selected: filter == selected) { store.chatFilter = filter }
                .accessibilityIdentifier("chats-filter-\(filter.rawValue)")
        }
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: BoardStyle.Space.s) { chips }
            VStack(alignment: .leading, spacing: 0) { chips }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, BoardStyle.Space.l)
    }

    private func pinnedTiles(_ pinned: [Bot]) -> some View {
        HStack(spacing: BoardStyle.Space.xl) {
            ForEach(pinned) { bot in
                NavigationLink(value: ConversationRoute(botId: bot.id)) {
                    VStack(spacing: BoardStyle.Space.s) {
                        LiveBotAvatar(bot: bot, size: 76)
                            // The badge sits on the top right corner of the face, not next to the name.
                            .overlay(alignment: .topTrailing) {
                                // The face keeps its size, so the badge on it stops growing at the largest
                                // standard text size and never covers the face.
                                if bot.unreadCount > 0 {
                                    UnreadBadge(count: bot.unreadCount).offset(x: 4, y: -2)
                                        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                                }
                            }
                        Text(bot.name).font(.subheadline).foregroundStyle(.secondary)
                            .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(width: pinWidth).frame(minHeight: 110)
                    .background(BoardStyle.paper)
                    .contentShape(Rectangle())
                    .contextMenu { botActions(bot) }
                }
                .buttonStyle(HomePressStyle())
                .accessibilityIdentifier("pinned-bot-\(bot.id)")
            }
        }.padding(.horizontal, BoardStyle.Space.l)
    }

    /// "team-chat-<room>" for a team, "team-chat-<room>-<chat>" for a named chat, "direct-chat-<member>" for a direct
    /// chat.
    static func identifier(_ chat: TeamChatKey) -> String {
        chat.member.map { "direct-chat-" + $0 } ?? "team-chat-" + chat.room + (chat.chat.map { "-" + $0 } ?? "")
    }

    @ViewBuilder
    private func botActions(_ bot: Bot) -> some View {
        Button(bot.pinned ? "Unpin bot" : "Pin bot", systemImage: bot.pinned ? "pin.slash" : "pin") {
            Task { await store.pinBot(bot, pinned: !bot.pinned) }
        }
        Button("Delete bot", systemImage: "trash", role: .destructive) { deleting = bot }
    }

}

/// A text chip that one of a few choices selects: the filters of the Chats list, and the tabs of the bot details on
/// a Mac. The selected chip sits on a soft capsule of the text color, so it reads in light and in dark mode.
struct ChipButton: View {
    let title: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.body.weight(selected ? .semibold : .regular))
                .foregroundStyle(selected ? .primary : .secondary)
                .lineLimit(1)
                .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                .frame(minHeight: 30)
                .background(selected ? Color.primary.opacity(0.1) : .clear, in: Capsule())
                #if os(iOS)
                // A finger needs 44 points. The capsule stays small.
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                #else
                .contentShape(Capsule())
                #endif
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
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
        if bot.unreadCount > 0 { parts.append(UnreadBadge.spoken(bot.unreadCount)) }
        return parts.joined(separator: ", ")
    }
}

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot
    /// The face of the bot. The iPhone list shows a large face; a Mac sidebar shows a smaller one.
    var avatarSize: CGFloat = BoardStyle.rowFace
    private var latestMessage: Message? {
        latestConversationMessage(in: store.messages.filter { $0.botId == bot.id })
    }
    /// The line under the name: never empty.
    private var preview: String { ChatRowText.line(newestText) }
    private var newestText: String {
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
        ChatRowLayout(name: bot.name, unread: bot.unreadCount, date: lastMessageDate) {
            LiveBotAvatar(bot: bot, size: avatarSize)
        } subtitle: {
            Group {
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
        .accessibilityElement(children: .combine)
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, preview: subtitle == .preview ? preview : ""))
    }
}

enum ChatRowText {
    /// The line under the name. A chat without a message says so, for a bot as for a team, so no row has an empty line.
    static func line(_ preview: String) -> String {
        preview.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? TeamChatPreview.empty : preview
    }
}

/// The layout of one row of the Chats list and of the Mac sidebar, for a bot and a team, as the chat list of WhatsApp.
/// The face comes first. Then a grid of two lines. The name and the age of the newest message share the first line.
/// The line under the name and the badges share the second line. The badges are the round "@" mark of a team whose
/// unread message names this person, and then the unread count. WhatsApp and Telegram have the same mark. The age
/// and the badges end at one right edge. No line is empty, so a row of one line has its text centered on the face.
/// At the accessibility text sizes the name, the age with the badges, and the line under the name each have their
/// own line.
struct ChatRowLayout<Face: View, Subtitle: View>: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let name: String
    let unread: Int
    let date: Date?
    var mentioned = false
    @ViewBuilder let face: () -> Face
    @ViewBuilder let subtitle: () -> Subtitle

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            face()
            if dynamicTypeSize.isAccessibilitySize {
                // The badge grows with the text and stands on its own line, so it covers no name.
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    nameText
                    if date != nil || hasBadges {
                        HStack(spacing: BoardStyle.Space.s) {
                            if let date { ageText(date) }
                            badges
                        }
                    }
                    subtitle()
                }
            } else {
                // The name and the age share the first baseline. The badges fill the second line: a badge is as high
                // as the line under the name. The gap between the columns keeps the text clear of the wider of the
                // age and the badges.
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: BoardStyle.Space.l,
                     verticalSpacing: BoardStyle.Space.line) {
                    GridRow {
                        nameText.frame(maxWidth: .infinity, alignment: .leading)
                        if let date {
                            ageText(date).fixedSize().gridColumnAlignment(.trailing)
                        }
                    }
                    GridRow(alignment: .center) {
                        subtitle()
                        if hasBadges {
                            // A frame of no height puts the badges on the center of the line and gives the line no
                            // height. A badge thus does not move the text and does not make the row taller.
                            badges.fixedSize().frame(height: 0).gridColumnAlignment(.trailing)
                        }
                    }
                }
            }
        }
        .frame(minHeight: 52)
    }

    private var hasBadges: Bool { unread > 0 || mentioned }

    private var nameText: some View {
        Text(name).font(.body.weight(unread > 0 ? .semibold : .regular)).lineLimit(1)
    }

    private func ageText(_ date: Date) -> some View {
        CompactAgeLabel(date: date).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
    }

    /// The "@" mark, then the unread count.
    private var badges: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if mentioned { MentionBadge() }
            if unread > 0 { UnreadBadge(count: unread) }
        }
    }
}

/// The mark of a team whose unread message names this person: an "@" in a circle of the size and the fill of the
/// unread badge. The label of the row says it to VoiceOver (TeamRowSpeech), so the mark itself is silent.
struct MentionBadge: View {
    @ScaledMetric(relativeTo: .subheadline) private var size = BoardStyle.badge

    var body: some View {
        Text("@")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(BoardStyle.controlLabel)
            .frame(width: size, height: size)
            .background(UnreadBadge.fill, in: Circle())
            .accessibilityHidden(true)
    }
}

/// The unread count of a chat: a filled round badge in the ink of the app with digits in the paper color, as the
/// round send button of the composer. One digit gives a circle, more digits a capsule of the same height, and over 99
/// shows "99+".
struct UnreadBadge: View {
    let count: Int
    @ScaledMetric(relativeTo: .subheadline) private var size = BoardStyle.badge

    var body: some View {
        let text = Self.text(count)
        Text(text)
            .font(.caption2.weight(.semibold).monospacedDigit())
            .foregroundStyle(BoardStyle.controlLabel)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, Self.sidePadding(text))
            .frame(minWidth: size, minHeight: size)
            // The ink as a plain color: a Mac sidebar draws `Color.primary` with the vibrancy of its material, which
            // would make the fill of the badge pale.
            .background(Self.fill, in: Capsule())
            .accessibilityElement()
            .accessibilityLabel(Self.spoken(count))
            .accessibilityIdentifier("unread-badge")
    }

    /// The ink of the app (the fill of the send button) without vibrancy: black in light mode, white in dark mode.
    static let fill = Color.adaptive { dark, _ in dark ? .white : .black }

    /// The digits of the badge. Over 99 shows "99+".
    nonisolated static func text(_ count: Int) -> String { count > 99 ? "99+" : "\(count)" }

    /// The space at each side of the digits. One character has none, so the badge is a circle of its height. More
    /// characters give a capsule.
    nonisolated static func sidePadding(_ text: String) -> CGFloat { text.count > 1 ? BoardStyle.Space.s : 0 }

    /// What VoiceOver says for the count of a row, a tile and the badge.
    nonisolated static func spoken(_ count: Int) -> String { count == 1 ? "1 unread message" : "\(count) unread messages" }
}

struct BotActivityDot: View {
    @Environment(AppStore.self) private var store
    @ScaledMetric(relativeTo: .footnote) private var size = 10
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
