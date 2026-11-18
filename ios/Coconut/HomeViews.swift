import SwiftUI

/// Three tabs of the iPhone. Inbox has what asks for the person: the rules that breach, the cards that wait, and the
/// to-dos. Chats is one list of the chats with people, as Telegram has it: the teams, their named chats and the direct
/// chats. Agents is the list of the chats of the projects in two tabs, Working and Idle (AgentsFilter), with the
/// message box under the list (NewChatBar).
struct HomeLaunchView: View {
    enum Part { case inbox, chats, agents }
    var part = Part.agents
    @Environment(AppStore.self) private var store
    /// The tab of Agents that shows. The phone keeps the choice.
    @AppStorage(AgentsFilter.key) private var filter = AgentsFilter.working
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.pushRoute) private var pushRoute
    /// Opens the place of a row of Needs attention, or the cards that wait.
    let open: (HomeTarget) -> Void
    @ScaledMetric(relativeTo: .title3) private var todoIconWidth = 24

    var body: some View {
        switch part {
        case .inbox: todoList
        case .chats: chatList(ChatList.chats(store.shownTeamRooms, local: store.newestLocalMessages))
        case .agents: chatList(ChatList.agents(bots: store.bots, latest: store.chatSummaries.newestActivity,
                                               rooms: store.shownTeamRooms, local: store.newestLocalMessages))
        }
    }

    private var todoList: some View {
        let todos = AgentTodos.make(artifacts: store.artifacts, bots: store.bots)
        let cards = store.pendingCardCount
        let quick = QuickTodos.list(store).count + PendingTodos.shared.items.count
        return List {
            attention(cards: cards)
            QuickTodoRows()
            ForEach(todos) { item in todoRow(item).homeRow() }
            if todos.isEmpty && cards == 0 && quick == 0 {
                Text("Nothing waits for you. Tap + to add a to-do.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .accessibilityIdentifier("todos-empty")
                    .homeRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.l, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        .navigationTitle("Inbox")
        .toolbar {
            // One tap and one line add a to-do, which an agent starts on at once (QuickTodos).
            ToolbarItem(placement: .primaryAction) {
                Button("New to-do", systemImage: "plus") { PendingTodos.shared.adding = true }
                    .accessibilityIdentifier("new-todo")
            }
        }
        .sheet(isPresented: Binding(get: { PendingTodos.shared.adding }, set: { PendingTodos.shared.adding = $0 })) { NewTodoSheet() }
        // The root of the app loads the overview of Finance for the count of the tab (CoconutHome), so a pull only
        // loads it again.
        .refreshable { await store.reconnect(); await store.loadBusinessAttention() }
    }

    /// The list of Chats or of Agents, as Telegram has it: the pinned chats first, then every other chat newest
    /// activity first (ChatList.chats, ChatList.agents). Every row has the same form: the name, the last message, the
    /// age and the badges. A starred row only adds the star mark. Agents shows the chats of one of its two tabs, in the
    /// same order.
    private func chatList(_ rows: [ChatListItem]) -> some View {
        let archivedCount = part == .agents ? store.bots.filter(\.archived).count : 0
        let now = Date.now
        let working: Set<String> = part == .agents ? Set(rows.filter { works($0, at: now) }.map(\.id)) : []
        let shown = part != .agents ? rows : rows.filter { working.contains($0.id) == (filter == .working) }
        // A list, not a stack, so that a row of a chat has the swipe of the system: swipe left to archive.
        return List {
            if shown.isEmpty {
                Text(part == .chats ? "No chats with people yet." : rows.isEmpty ? "No chats yet."
                     : filter == .working ? "No chat is working." : "No chat is idle.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .accessibilityIdentifier("chats-empty")
                    .homeRow()
            }
            ForEach(shown) { item in
                switch item {
                case .bot(let bot):
                    BotContactRow(bot: bot, title: bot.isChat ? bot.listName : nil, pinned: ChatList.isStarred(bot))
                        .padding(.vertical, BoardStyle.Space.m)
                        .background(BoardStyle.paper)
                        .modifier(ChatDeckRow(botId: bot.id) { pushRoute?(ConversationRoute(botId: bot.id)) })
                        .accessibilityIdentifier(ChatList.isCoconut(bot) ? "coconut-chat" : "bot-conversation-\(bot.id)")
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            // A project stays, and so does the Coconut chat (Bot.canRemove).
                            if bot.canRemove { archiveButton(bot) }
                            muteButton(bot)
                        }
                        .homeRow()
                case .team, .direct, .botDirect:
                    // A team chat has Star and Mute in its menu, and a named chat has Mute in its swipe too. The Team
                    // screen has the leave action.
                    if let chat = item.chat {
                        Button { pushRoute?(TeamChatRoute(chat: chat)) } label: {
                            PeopleRow(item: item, pinMark: item.isPinnedChat)
                                .padding(.vertical, BoardStyle.Space.m)
                                .background(BoardStyle.paper)
                                .contentShape(Rectangle())
                                .modifier(TeamChatMuteMenu(item: item))
                        }
                            .buttonStyle(HomePressStyle())
                            .accessibilityIdentifier(Self.identifier(chat))
                            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                if let mute = TeamChatMute.listAction(item) { teamMuteButton(mute) }
                            }
                            .homeRow()
                    }
                }
            }
            // The archived chats wait behind one row at the end, as the archived chats of WhatsApp. On Agents the row
            // ends Idle, because an archived chat does not work.
            if archivedCount > 0 && (part != .agents || filter == .idle) {
                Button { pushRoute?(ArchivedAgentsRoute()) } label: {
                    HStack(spacing: BoardStyle.Space.m) {
                        Image(systemName: "archivebox").foregroundStyle(.secondary)
                        Text("Archived").foregroundStyle(.primary)
                        Spacer(minLength: 0)
                        Text("\(archivedCount)").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, BoardStyle.Space.m)
                    .contentShape(Rectangle())
                }
                .buttonStyle(HomePressStyle())
                .accessibilityIdentifier("chats-archived")
                .homeRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .contentMargins(.top, BoardStyle.Space.s, for: .scrollContent)
        .contentMargins(.bottom, BoardStyle.Space.xl, for: .scrollContent)
        .background(BoardStyle.paper)
        // The two tabs of Agents, with the count of each. The control stays under the title while the list scrolls, as
        // Overview and Work of a function (BusinessWorkspaceView).
        .safeAreaInset(edge: .top, spacing: 0) {
            if part == .agents {
                Picker("Agents", selection: $filter) {
                    Text("Working \(working.count)").tag(AgentsFilter.working)
                    Text("Idle \(rows.count - working.count)").tag(AgentsFilter.idle)
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 360)
                    .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.s)
                    // The paper ends at the control and leaves the bar of the page to the system.
                    .frame(maxWidth: .infinity).background(BoardStyle.paper, ignoresSafeAreaEdges: [])
                    .accessibilityIdentifier("agents-filter")
            }
        }
        .navigationTitle(part == .agents ? "Agents" : "Chats")
        .toolbar {
            // One tap reads every chat of both lists, as Read All in Telegram. It shows while a chat of this list has
            // an unread count.
            if rows.contains(where: { $0.unreadCount > 0 }) {
                ToolbarItem(placement: .primaryAction) {
                    MarkAllReadButton().accessibilityIdentifier("chats-mark-all-read")
                }
            }
        }
        .refreshable { await store.reconnect() }
        // The message box under the list gives the keyboard back when the list moves.
        .scrollDismissesKeyboard(.interactively)
    }

    /// A chat of Agents works while its row says so: a working line, or a card that waits for the person. A direct chat
    /// with a bot of a team works while the bot types in it. Every other chat is idle.
    private func works(_ item: ChatListItem, at date: Date) -> Bool {
        switch item {
        case .bot(let bot): store.botPhase(bot) != nil || store.botNeedsYou(bot)
        case .botDirect(_, let direct): !(direct.typing?.active(at: date).isEmpty ?? true)
        case .team, .direct: false
        }
    }

    /// What asks for the person comes first on Inbox: the rules that breach now, then the cards that wait. Each row
    /// draws nothing when there is nothing to show.
    @ViewBuilder
    private func attention(cards: Int) -> some View {
        // The rules read the clock, so the card checks them again each minute.
        TimelineView(.periodic(from: .now, by: 60)) { timeline in
            let items = store.attentionItems(now: timeline.date)
            AttentionCard(items: items, open: open)
                .padding(.bottom, items.isEmpty ? 0 : BoardStyle.Space.m)
        }
        .homeRow(top: BoardStyle.Space.s)
        if cards > 0 {
            Button { open(.cards(nil)) } label: {
                HStack(spacing: BoardStyle.Space.m) {
                    // The count is what the person came for, so it is the largest thing under the title.
                    Text("\(cards)").font(.largeTitle.weight(.bold).monospacedDigit()).foregroundStyle(BoardStyle.attention)
                    Text(cards == 1 ? "card waits for you" : "cards wait for you")
                        .font(.body.weight(.semibold)).foregroundStyle(.primary)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
                .padding(.vertical, BoardStyle.Space.m)
                .contentShape(Rectangle())
            }
            .buttonStyle(HomePressStyle())
            .accessibilityIdentifier("home-review-cards")
            .homeRow()
        }
    }

    private func archiveButton(_ bot: Bot) -> some View {
        Button { Task { await store.archiveBot(bot, archived: true) } } label: {
            Label("Archive", systemImage: "archivebox")
        }
        .tint(.gray)
        .accessibilityIdentifier("archive-bot-\(bot.id)")
    }

    /// Mute or Unmute, beside Archive, as "Hide Alerts" in Messages and Mute in Telegram (BotChatMute).
    private func muteButton(_ bot: Bot) -> some View {
        Button { Task { await store.muteBot(bot, muted: bot.notifications) } } label: {
            Label(BotChatMute.label(bot), systemImage: BotChatMute.symbol(bot))
        }
        .tint(BoardStyle.husk)
        .accessibilityIdentifier("mute-bot-\(bot.id)")
    }

    /// Mute or Unmute for a named chat of a team: the same choice as its menu (TeamChatMuteMenu).
    private func teamMuteButton(_ mute: TeamChatMute.ListAction) -> some View {
        Button {
            store.error = nil
            Task { await store.setTeamChatNotifications(mute.room, chat: mute.chat, enabled: mute.enabled) }
        } label: {
            Label(mute.label, systemImage: mute.symbol)
        }
        .tint(BoardStyle.husk)
        .disabled(TeamChatMute.waits(mute.chat, store.teamChatMuteChoices))
        .accessibilityIdentifier("chat-row-mute-swipe")
    }

    private func todoRow(_ item: Artifact) -> some View {
        let agent = store.bots.first { $0.id == item.botId }?.name ?? ""
        return Button {
            pushRoute?(ConversationRoute(botId: item.botId, workContext: WorkReference(id: item.id, title: item.title)))
        } label: {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: AgentTodos.symbol(item.status))
                    .font(.title3)
                    .foregroundStyle(item.status == "blocked" ? BoardStyle.destructive : .secondary)
                    .frame(width: todoIconWidth)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(item.title).font(.body).foregroundStyle(.primary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                    Text("\(agent) · \(item.status.capitalized)").font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("chats-todo-\(item.id)")
    }

    /// "team-chat-<room>" for a team, "team-chat-<room>-<chat>" for a named chat, "direct-chat-<member>" for a direct
    /// chat, "bot-direct-chat-<team bot ID>" for a direct chat with a bot.
    static func identifier(_ chat: TeamChatKey) -> String {
        if let bot = chat.bot { return "bot-direct-chat-" + bot }
        return chat.member.map { "direct-chat-" + $0 } ?? "team-chat-" + chat.room + (chat.chat.map { "-" + $0 } ?? "")
    }

}

/// The two tabs of Agents. Working has the chats that work or wait for the person, and Idle has the others, so the
/// person finds a free chat for the next task in one tap.
enum AgentsFilter: String {
    case working, idle
    static let key = "agents-filter"
}

/// The archived chats, and the archived agents of an earlier release: swipe left or hold a row to bring one back. A row
/// opens the conversation, which keeps its history.
struct ArchivedAgentsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @State private var deleting: Bot?

    var body: some View {
        let archived = store.bots.filter(\.archived).sorted { $0.listName.localizedStandardCompare($1.listName) == .orderedAscending }
        List {
            if archived.isEmpty {
                Text("No archived chats.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
                    .homeRow()
            }
            ForEach(archived) { bot in
                Button { pushRoute?(ConversationRoute(botId: bot.id)) } label: {
                    BotContactRow(bot: bot, title: bot.isChat ? bot.listName : nil)
                        .padding(.vertical, BoardStyle.Space.m)
                        .background(BoardStyle.paper)
                        .contentShape(Rectangle())
                        .contextMenu {
                            Button("Unarchive", systemImage: "tray.and.arrow.up") {
                                Task { await store.archiveBot(bot, archived: false) }
                            }
                            Button(bot.isChat ? "Delete chat" : "Delete agent", systemImage: "trash", role: .destructive) { deleting = bot }
                        }
                }
                .buttonStyle(HomePressStyle())
                .accessibilityIdentifier("archived-bot-\(bot.id)")
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    Button { Task { await store.archiveBot(bot, archived: false) } } label: {
                        Label("Unarchive", systemImage: "tray.and.arrow.up")
                    }
                    .tint(BoardStyle.husk)
                    .accessibilityIdentifier("unarchive-bot-\(bot.id)")
                }
                .homeRow()
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 0)
        .background(BoardStyle.paper)
        .navigationTitle("Archived")
        .modifier(DeleteBotDialog(bot: $deleting))
    }
}

extension View {
    /// A row of the Chats list: the paper behind it, no line under it, and the side space of the page.
    func homeRow(inset: CGFloat = BoardStyle.Space.l, top: CGFloat = 0) -> some View {
        listRowInsets(EdgeInsets(top: top, leading: inset, bottom: 0, trailing: inset))
            .listRowSeparator(.hidden)
            .listRowBackground(BoardStyle.paper)
    }
}

/// A text chip that one of a few choices selects: the filters of the Chats list, and the tabs of the bot details on
/// a Mac. The selected chip sits on a soft capsule of the text color, so it reads in light and in dark mode.
struct ChipButton: View {
    let title: String
    let selected: Bool
    /// A count after the title, for a chip of the filter of the alerts. Nil for a chip without one.
    var count: String? = nil
    /// A symbol before the title. Nil for a chip without one.
    var symbol: String? = nil
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: BoardStyle.Space.xs) {
                if let symbol { Image(systemName: symbol).imageScale(.small) }
                Text(title)
                if let count { Text(count).monospacedDigit().fontWeight(.regular).foregroundStyle(.secondary) }
            }
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
    let textMessages = messages.filter(isPreviewText)
    let content = textMessages.filter { !isCompletionLabel($0) }
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
    func accessibilityLabel(bot: Bot, preview: String, queued: Int = 0) -> String {
        var parts = [bot.name]
        switch self {
        case .needsYou: parts.append(StatusLabel.label(for: "needs_you"))
        case .phase(let phase): parts += [phase.label] + [MessageQueue.label(queued)].compactMap { $0 }
        case .health(let health): parts.append(health.label)
        case .preview: parts += [bot.status.replacingOccurrences(of: "_", with: " "), preview]
        }
        if !bot.notifications { parts.append(TeamChatMute.spoken) }
        if BotChatMute.unread(bot) > 0 { parts.append(UnreadBadge.spoken(bot.unreadCount)) }
        return parts.joined(separator: ", ")
    }
}

struct BotContactRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot
    /// The name of the row. A further chat of a project shows its title next to the name of the project.
    var title: String? = nil
    /// The star mark of a starred project at the top of the Chats list.
    var pinned = false
    /// The row reads the summary of its bot, not the messages, so a streamed word does not redraw it.
    private var summary: BotChatSummary { store.chatSummaries[bot.id] }
    private var latestMessage: Message? { summary.latestMessage }
    /// The line under the name: never empty.
    private var preview: String { ChatRowText.line(newestText) }
    private var newestText: String {
        guard let message = latestMessage else {
            return TranscriptPresentation.previewText(
                TranscriptPresentation.previewFallback(bot.lastMessage, streaming: summary.streaming))
        }
        let sender = message.senderBotId.flatMap { id in id == bot.id ? nil : store.bots.first { $0.id == id }?.name ?? "Deleted project" }
        let prefix = message.role == "user" ? "You: " : sender.map { $0 + ": " } ?? ""
        let body = AttachmentOnly.isMarker(message.text) ? "Photo" : TranscriptPresentation.previewText(message.text)
        return prefix + (message.photo?.preview ?? body)
    }
    private var lastMessageDate: Date? { summary.newestDate }
    private var phase: BotPhase? { store.botPhase(bot) }

    var body: some View {
        let subtitle = BotRowSubtitle(needsYou: store.botNeedsYou(bot), phase: phase, health: bot.health)
        // A project has no face: its name and its state are the row.
        ChatRowLayout(name: title ?? bot.name, unread: BotChatMute.unread(bot), date: lastMessageDate,
                      muted: !bot.notifications, pinned: pinned) {
            EmptyView()
        } subtitle: {
            Group {
                // A pending card or a working phase takes the place of the last message, like a typing notice.
                switch subtitle {
                case .needsYou:
                    StatusLabel(status: "needs_you", compact: true)
                        .transition(.opacity)
                        .accessibilityIdentifier("bot-needs-you-\(bot.id)")
                case .phase(let phase):
                    HStack(spacing: BoardStyle.Space.xs) {
                        ShimmerText(text: phase.label)
                        // The messages of the person that wait for the next turn.
                        if let queued = MessageQueue.label(store.queuedCount(bot.id)) {
                            Text("· " + queued).foregroundStyle(.secondary).lineLimit(1)
                                .accessibilityIdentifier("bot-queued-\(bot.id)")
                        }
                    }
                    .font(.subheadline)
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
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, preview: subtitle == .preview ? preview : "",
                                                        queued: store.queuedCount(bot.id)))
    }
}

enum ChatRowText {
    /// The line under the name. A chat without a message says so, for a bot as for a team, so no row has an empty line.
    static func line(_ preview: String) -> String {
        preview.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? TeamChatPreview.empty : preview
    }
}

/// The layout of one row of the Chats list and of the Mac sidebar, for a bot and a team, as the chat list of WhatsApp.
/// The face comes first. A grid of two lines follows. The name and the age of the newest message share the first line.
/// The line under the name and the badges share the second line. The badges are the mark of a muted chat
/// (`bell.slash`), the round "@" mark of a team whose unread message names this person, and then the unread count. WhatsApp and Telegram have the same mark. The age
/// and the badges end at one right edge. No line is empty, so a row of one line has its text centered on the face.
/// At the accessibility text sizes the name, the age with the badges, and the line under the name each have their
/// own line.
struct ChatRowLayout<Face: View, Subtitle: View>: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let name: String
    let unread: Int
    let date: Date?
    var mentioned = false
    /// The person muted the chat (TeamChatMute).
    var muted = false
    /// The chat is pinned and shows at the top of the list (TeamChatPin).
    var pinned = false
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
                            // The row centers its cells. A frame of no height adds no height to the line. A badge thus
                            // does not move the text and does not make the row taller.
                            badges.fixedSize().frame(height: 0).gridColumnAlignment(.trailing)
                        }
                    }
                }
            }
        }
        .frame(minHeight: 52)
    }

    private var hasBadges: Bool { unread > 0 || mentioned || muted || pinned }

    private var nameText: some View {
        Text(name).font(.body.weight(unread > 0 ? .semibold : .regular)).lineLimit(1)
    }

    private func ageText(_ date: Date) -> some View {
        CompactAgeLabel(date: date).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
    }

    /// The mark of a muted chat, the "@" mark, then the unread count.
    private var badges: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if muted { MutedMark() }
            if pinned { MutedMark(symbol: "star") }
            if mentioned { MentionBadge() }
            if unread > 0 { UnreadBadge(count: unread) }
        }
    }
}

/// The mark of a muted chat in the secondary color, as WhatsApp has it: in the font of the line under the name, so it
/// is as high as that line. The label of the row says it to VoiceOver (TeamRowSpeech), so the mark itself is silent.
struct MutedMark: View {
    /// The mark of a pinned chat at the top of the list uses the same form with the line symbol `pin`.
    var symbol = TeamChatMute.mark

    var body: some View {
        Image(systemName: symbol)
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .accessibilityHidden(true)
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

/// The unread count of a chat: a filled round badge in the green of the New line of a chat, with digits in the paper
/// color. One digit gives a circle, more digits a capsule of the same height, and over 99 shows "99+".
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
            // A plain color: a Mac sidebar draws `Color.primary` with the vibrancy of its material, which would make
            // the fill of the badge pale.
            .background(Self.fill, in: Capsule())
            .accessibilityElement()
            .accessibilityLabel(Self.spoken(count))
            .accessibilityIdentifier("unread-badge")
    }

    /// The green of the New line of a chat (BoardStyle.green), never black. Unread is green everywhere.
    static let fill = BoardStyle.green

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
