import SwiftUI

/// The chat of a team: a bot chat with more than one other party (docs/team-log.md, "Team chat"). It reuses the bubble,
/// the quote, the day pill, the times and the composer of the bot chat, and adds only what a group needs: the name of
/// the writer above a run, Edit and Delete for the person's own messages, and Delete for the admin of the team
/// (TeamMessageDeletion).
///
/// The same view is the direct chat with one person of the team ("Direct messages"): its title is the person, no name
/// shows above a message, and the composer has no list for `@`. It is also each named chat of the team ("Chats of a
/// team"): its title is the name of the chat, with the name of the team under it (TeamRoom.inChat).
struct TeamChatView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismiss) private var dismiss
    let chatKey: TeamChatKey
    @State private var draft = ""
    @State private var replyTo: TeamMessage?
    @State private var editing: TeamMessage?
    /// The bots and the people that the person picked with `@` in the draft (see BotMentions).
    @State private var mentions: [SelectedBotMention] = []
    @State private var deleting: TeamMessage?
    /// New messages pull the chat down while the reader is at its bottom. Only a move of the reader changes it.
    @State private var follows = true
    /// The chat shows its newest message. The read position moves only then.
    @State private var atBottom = true
    @State private var userScrolling = false
    @State private var isVisible = false
    @State private var rows = TeamRowsCache()
    /// The rows that the list builds (see TranscriptWindow).
    @State private var window = TranscriptWindow()
    @State private var growingAbove = false
    @State private var dayReference = Date()
    @State private var pinnedDay = PinnedDay()
    /// Who types, as the row under the newest message shows it. It changes with a fade.
    @State private var typing: [TeamTyping] = []
    /// The time of the last look at who types: the arrival of a list, or the end of a subject.
    @State private var typingClock = Date()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var composerFocused: Bool
    @ScaledMetric(relativeTo: .body) private var nameWidth = 150
    #if os(macOS)
    @Environment(\.openSettings) private var openSettings
    /// The title of the team floats over the top of the chat. It has two lines (the name and who is in the chat), so it
    /// is 10 points taller than the title of a bot (34).
    private static let titlePillHeight: CGFloat = 44
    /// The chat runs up under the toolbar of the window, where the title floats. The chat and the pinned day pill start
    /// below it with the gap of the bot chat: 8 points above the title and 10 below it.
    private static let titleHeight: CGFloat = 8 + titlePillHeight + 10
    #else
    private static let titleHeight: CGFloat = 0
    #endif

    /// The room as this chat shows it. Nil when the team, or the named chat, is no longer in the list.
    private var room: TeamRoom? { store.team?.rooms.first { $0.room == chatKey.room }?.inChat(chatKey.chat) }
    private var chat: TeamChat { store.teamChats[chatKey] ?? TeamChat() }
    /// The other person of a direct chat. Nil for the Team chat.
    private func peer(_ room: TeamRoom) -> TeamDirectPeer? { chatKey.member.map(room.directPeer) }
    private func currentRows() -> [TeamChatRow] { rows.current(chat, calendar: .current) }

    var body: some View {
        Group {
            if let room {
                chatBody(room)
            } else {
                QuietEmptyState(symbol: "person.2", title: "Chat unavailable",
                                message: "This Mac is no longer in this team.")
            }
        }
        .inlineNavigationTitle()
        #if os(iOS)
        // A named chat that the admin deleted closes, as the pushed Team screen of a team that goes does. On a Mac the
        // chat is the root of the detail of the window, where a dismiss would close the window: the sidebar selects
        // the Inbox in its place (MacSidebar, TeamRooms.has).
        .onChange(of: room == nil) { _, gone in if gone && chatKey.chat != nil { dismiss() } }
        #endif
    }

    @ViewBuilder
    private func chatBody(_ room: TeamRoom) -> some View {
        let chat = chat
        let allRows = currentRows()
        let ids = allRows.map(\.id)
        let shown = window.range(of: ids)
        // A new first row of the window makes a new list, so the list measures every row (see TranscriptWindow).
        let listId = window.isUsed && !shown.isEmpty ? ids[shown.lowerBound] : nil
        let readKey = [chat.newestFirst ?? 0, atBottom ? 1 : 0, isVisible && scenePhase == .active ? 1 : 0]
        ScrollViewReader { proxy in
            List {
                Group {
                    #if os(macOS)
                    ChatTitleSpace(titleHeight: Self.titleHeight)
                    #endif
                    header(room, chat: chat, empty: allRows.isEmpty)
                    if shown.lowerBound > 0 || chat.before != nil {
                        Button {
                            follows = false
                            let anchor = allRows[shown].first?.id
                            Task { await loadEarlier(anchor: anchor, proxy: proxy) }
                        } label: {
                            if chat.loadingEarlier || growingAbove { ProgressView() }
                            else { Text("Load earlier messages").font(.footnote) }
                        }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .disabled(chat.loadingEarlier || growingAbove)
                        .accessibilityIdentifier("load-earlier-messages")
                    }
                    ForEach(allRows[shown]) { row in
                        let date = row.createdAt.apiDate
                        VStack(spacing: 0) {
                            if row.startsDay, let date {
                                DaySeparatorRow(date: date, now: dayReference, row: row.id, pinned: pinnedDay)
                            }
                            TeamMessageRow(row: row, room: room, peer: peer(room),
                                           editedText: row.message.flatMap { chat.editing[$0.id] },
                                           canWrite: canWrite(room), actions: actions(proxy: proxy))
                        }
                        .pinnedDayRow(pinnedDay, id: row.id, date: date, startsDay: row.startsDay)
                        // Keep full text height when the list resizes or scrolls to a new message.
                        .fixedSize(horizontal: false, vertical: true)
                        .id(row.id)
                    }
                    // The last row: who types, under the newest message and above the composer, then the anchor of
                    // the bottom. The row is always there, so the list never adds or removes a row for typing while it
                    // scrolls to its bottom. A direct chat shows nobody. The row inset and the padding keep 12 points
                    // below the last message.
                    VStack(alignment: .leading, spacing: 0) {
                        if !typing.isEmpty {
                            TeamTypingRow(room: room, subjects: typing)
                                .padding(.top, BoardStyle.Space.xs)
                                .transition(.opacity)
                        }
                        Color.clear.frame(height: 1)
                    }
                    .padding(.bottom, BoardStyle.Space.s).id(Self.bottom)
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
            .accessibilityIdentifier("team-chat-transcript")
            .modifier(ConversationTimestamps())
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.bottom, for: .alignment)
            .scrollDismissesKeyboard(.interactively)
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 100
            } action: { _, value in
                atBottom = value
                if userScrolling { follows = value }
            }
            .onScrollGeometryChange(for: [CGFloat].self) {
                [$0.contentOffset.y, $0.contentSize.height, $0.containerSize.height, $0.contentInsets.top + $0.contentInsets.bottom]
            } action: { old, new in
                #if os(macOS)
                // A mouse wheel reports no scroll phase on a Mac, as in the bot chat. An upward move in an unchanged
                // viewport is the reader's, and a move that reaches the bottom follows new messages again.
                let sameViewport = abs(new[2] - old[2]) < 0.5 && abs(new[3] - old[3]) < 0.5
                if sameViewport && new[0] < old[0] - 0.5 && new[1] >= old[1] - 0.5 { follows = false }
                else if new[0] > old[0] + 0.5 && new[0] + new[2] >= new[1] - 4 { follows = true }
                #endif
                // New content, the keyboard and the composer move the bottom. The chat stays there while it follows.
                if follows && !userScrolling && (new[1] != old[1] || new[2] != old[2] || new[3] != old[3]) {
                    proxy.scrollTo(Self.bottom, anchor: .bottom)
                }
            }
            .onScrollPhaseChange { _, phase in
                #if !os(macOS)
                pinnedDay.scrolls(phase != .idle)
                #endif
                userScrolling = phase == .interacting || phase == .decelerating
            }
            .onChange(of: chat.pending.last?.id) { _, id in
                // The person sent a message: the chat shows it at the bottom.
                guard id != nil else { return }
                follows = true
                proxy.scrollTo(Self.bottom, anchor: .bottom)
            }
            .pinnedDayPill(pinnedDay, now: dayReference, below: Self.titleHeight)
            .background(BoardStyle.paper)
            // From the first page on, a new message does not push the first row out of the window.
            .onChange(of: chat.loaded, initial: true) { _, loaded in
                if loaded { window.keep(currentRows().map(\.id)) }
            }
            .onChange(of: readKey, initial: true) { _, _ in
                guard let first = TeamReadPosition.toPost(newest: chat.newestFirst, posted: store.teamReadMark(chatKey),
                                                           visible: isVisible && scenePhase == .active,
                                                           atBottom: atBottom) else { return }
                Task { await store.markTeamRead(chatKey, first: first) }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            footer(room)
                #if os(macOS)
                // The notice of the Mac window stands above the composer of the chat on the screen.
                .composerHeight(visible: isVisible)
                #endif
        }
        .task(id: store.connection) {
            // A load without a connection does nothing, so the chat loads again when the Mac connects.
            guard room.hasChat, store.connection == .connected || store.isDemo else { return }
            await store.loadTeamChat(chatKey)
        }
        .onAppear { isVisible = true; store.openTeamChat(chatKey) }
        // A chat that closes keeps its newest page only, so it opens again without stale older pages.
        .onDisappear { isVisible = false; composerFocused = false; store.closeTeamChat(chatKey) }
        .onReceive(DaySeparator.changes) { _ in dayReference = Date() }
        .onChange(of: chatKey.isDirect ? [] : room.typingNow(at: typingClock), initial: true) { _, value in
            withAnimation(BoardStyle.fade(reduceMotion: reduceMotion)) { typing = value }
        }
        .typingClock(room.typing, $typingClock)
        .onChange(of: draft) { _, value in
            // The person types: the Mac tells the team. Not for an edit of a sent message, and not in a direct chat.
            guard editing == nil, canWrite(room) else { return }
            store.teamDraftChanged(chatKey, draft: value)
        }
        .confirmationDialog(TeamMessageDeletion.title, isPresented: Binding(
            get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible,
                            presenting: deleting) { message in
            Button(TeamMessageDeletion.confirm, role: .destructive) { delete(message) }
                .accessibilityIdentifier("team-delete-confirm")
        }
        #if os(iOS)
        // An error takes its own space above the chat, so no message sits under it.
        .teamBanners(above: true)
        .toolbar {
            if #available(iOS 26.0, *) {
                ToolbarSpacer(.fixed, placement: .leadingBar)
            }
            ToolbarItem(placement: .leadingBar) {
                if let peer = peer(room) {
                    // The title of a direct chat names the person and the team. It opens nothing.
                    DirectTitle(peer: peer, team: room.name, faceSize: 28)
                        .frame(maxWidth: nameWidth + 36, alignment: .leading)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.leading, 4).padding(.trailing, BoardStyle.Space.m)
                        .frame(minHeight: 44)
                        .accessibilityElement(children: .combine)
                        .accessibilityIdentifier("direct-chat-title")
                } else {
                    // The title of the team opens its Team screen, as the title of a bot opens its settings.
                    NavigationLink(value: TeamScreenRoute(roomId: room.room)) {
                        TeamTitle(room: room, faceSize: 28)
                            .frame(maxWidth: nameWidth + 36, alignment: .leading)
                            .fixedSize(horizontal: true, vertical: false)
                        .padding(.leading, 4).padding(.trailing, BoardStyle.Space.m)
                        .frame(minHeight: 44).contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(TeamTitle.spoken(room))
                    .accessibilityIdentifier("team-chat-title")
                    .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
                }
            }
        }
        #else
        // The title floats over the top of the chat, as the title of a bot does. It opens the Team screen in the
        // Settings window.
        .overlay(alignment: .top) {
            if let peer = peer(room) {
                DirectTitle(peer: peer, team: room.name, faceSize: 24)
                    .padding(.leading, BoardStyle.Space.xs).padding(.trailing, 12)
                    .frame(height: Self.titlePillHeight)
                    .modifier(FloatingCapsule())
                    .padding(.top, 8)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("direct-chat-title")
            } else {
                Button { openSettings() } label: {
                    TeamTitle(room: room, faceSize: 24)
                    .padding(.leading, BoardStyle.Space.xs).padding(.trailing, 12)
                    .frame(height: Self.titlePillHeight)
                    .modifier(FloatingCapsule())
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .padding(.top, 8)
                .help("Team settings")
                .accessibilityLabel(TeamTitle.spoken(room))
                .accessibilityIdentifier("team-chat-title")
            }
        }
        #endif
    }

    private static let bottom = "team-chat-bottom"

    /// The first row of the chat: what the chat cannot show yet, or where a message goes. It is always a row, also an
    /// empty one, as the header of the bot chat (ConversationView.conversationHeader) is. So the first day pill has the
    /// same gap below the title in both chats.
    private func header(_ room: TeamRoom, chat: TeamChat, empty: Bool) -> some View {
        VStack(spacing: 0) { headerContent(room, chat: chat, empty: empty) }
            .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func headerContent(_ room: TeamRoom, chat: TeamChat, empty: Bool) -> some View {
        if !room.hasChat {
            EmptyView()
        } else if !chat.loaded {
            Group {
                if let error = chat.error { Text(error).font(.subheadline).foregroundStyle(.secondary) }
                else { ProgressView() }
            }
            .frame(maxWidth: .infinity).padding(.vertical, 45)
        } else if empty && chat.before == nil {
            // The structure of the empty bot chat (ConversationView.conversationHeader): face, title, one line.
            VStack(spacing: BoardStyle.Space.m) {
                if let peer = peer(room) {
                    PersonFace(member: peer.entry, memberId: peer.member, name: peer.name, size: 68)
                    Text(peer.name).font(.title3.weight(.semibold)).multilineTextAlignment(.center)
                } else {
                    TeamFace(room: room, size: 68)
                    Text(room.title).font(.title3.weight(.semibold)).multilineTextAlignment(.center)
                }
                Text(TeamChatEmptyLine.text(for: room, peer: peer(room)))
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 45)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("team-chat-empty")
        }
    }

    /// The composer, or one line that says why the person cannot write.
    @ViewBuilder
    private func footer(_ room: TeamRoom) -> some View {
        if let reason = closedReason(room) {
            Text(reason)
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 24).padding(.vertical, BoardStyle.Space.m)
                .modifier(ComposerBackdrop())
                .accessibilityIdentifier("team-chat-closed")
        } else {
            TeamChatComposer(placeholder: "Message " + (peer(room)?.name ?? room.title), targets: mentionTargets(room),
                             peer: peer(room)?.name, text: $draft, mentions: $mentions, replyTo: $replyTo,
                             editing: $editing, focused: $composerFocused, enabled: canWrite(room), send: send)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Why the person cannot write: the reason of the team, else for a direct chat a member that can no longer get a
    /// direct message. The chat then shows its history only.
    private func closedReason(_ room: TeamRoom) -> String? {
        if let reason = room.chatClosedReason { return reason }
        guard let peer = peer(room), !peer.canReceive else { return nil }
        return peer.closedLine
    }

    private func canWrite(_ room: TeamRoom) -> Bool {
        (store.hasPairing || store.isDemo) && closedReason(room) == nil
    }

    /// The rows that `@` lists. A direct chat is for two people, so it lists none.
    private func mentionTargets(_ room: TeamRoom) -> [TeamMentionTarget] {
        chatKey.isDirect ? [] : TeamMentions.targets(of: room)
    }

    private func actions(proxy: ScrollViewProxy) -> TeamMessageActions {
        TeamMessageActions(
            reply: { message in
                editing = nil
                replyTo = message
                composerFocused = true
            },
            edit: { message in
                replyTo = nil
                editing = message
                mentions = []
                draft = message.text
                composerFocused = true
            },
            delete: { deleting = $0 },
            showMessage: { id in show(id, proxy: proxy) },
            retry: { store.retryTeamMessage(chatKey, id: $0) },
            removeUnsent: { store.removeUnsentTeamMessage(chatKey, id: $0) })
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let room, canWrite(room), !text.isEmpty else { return }
        if let message = editing {
            draft = ""
            editing = nil
            Task {
                // A failure gives the edit back to the composer, unless the person typed something new.
                if await !store.editTeamMessage(chatKey, message, text: text), draft.isEmpty, editing == nil {
                    editing = message
                    draft = text
                }
            }
        } else {
            // The store saves the message first. When the save fails, the composer keeps the text.
            // The mentions count in the draft before the trim: their places are places in the draft.
            let named = BotMentions.recipients(in: draft, selected: mentions, bots: mentionTargets(room)).map(\.id)
            guard store.sendTeamMessage(chatKey, text: text, replyingTo: replyTo, mentions: named) else { return }
            draft = ""
            mentions = []
            replyTo = nil
        }
    }

    private func delete(_ message: TeamMessage) {
        if editing?.id == message.id { editing = nil; draft = "" }
        if replyTo?.id == message.id { replyTo = nil }
        Task { await store.deleteTeamMessage(chatKey, message) }
    }

    /// Shows a message that a quote names, when the chat has it.
    private func show(_ id: String, proxy: ScrollViewProxy) {
        let ids = currentRows().map(\.id)
        guard ids.contains(id) else { return }
        follows = false
        window.include(id, in: ids)
        Task {
            await Task.yield()
            proxy.scrollTo(id, anchor: .top)
        }
    }

    /// The control above the window. While the window lacks older rows that the app has, it adds a batch of them;
    /// when the window holds every row, it loads a page from the Mac first. Then the chat shows the row that was first.
    private func loadEarlier(anchor: String?, proxy: ScrollViewProxy) async {
        let ids = currentRows().map(\.id)
        switch window.press(ids) {
        case .loadPage:
            // The page from the Mac arrives above the window. Then the window takes a batch of it.
            window.keep(ids)
            await store.loadEarlierTeamMessages(chatKey)
        case .growWindow:
            growingAbove = true
            // A frame with the progress, then the new list.
            try? await Task.sleep(for: .milliseconds(16))
        }
        window.growTop(in: currentRows().map(\.id))
        await Task.yield()
        growingAbove = false
        if let anchor { proxy.scrollTo(anchor, anchor: .top) }
    }
}

extension TeamRoom {
    /// Why the person cannot write in the chat, in the words of the Team screen. Nil for an active team whose Mac
    /// serves the chat.
    var chatClosedReason: String? {
        guard hasChat else { return ServiceError.olderMac }
        return inactiveReason
    }
}

/// The rows of the chat change only with the chat and the calendar. A scroll redraws the chat often, so a redraw
/// reuses them.
@MainActor
private final class TeamRowsCache {
    private var chat: TeamChat?
    private var calendar: Calendar?
    private var value: [TeamChatRow] = []

    func current(_ chat: TeamChat, calendar: Calendar) -> [TeamChatRow] {
        if chat != self.chat || calendar != self.calendar {
            self.chat = chat
            self.calendar = calendar
            value = TeamChatRow.rows(chat, calendar: calendar)
        }
        return value
    }
}

/// What a message row asks of the chat.
struct TeamMessageActions {
    let reply: (TeamMessage) -> Void
    let edit: (TeamMessage) -> Void
    let delete: (TeamMessage) -> Void
    let showMessage: (String) -> Void
    let retry: (String) -> Void
    let removeUnsent: (String) -> Void
}

/// One message of the Team chat: the name of the writer above the first message of a run, then the bubble. A direct
/// chat has one other writer, so it shows no name.
struct TeamMessageRow: View {
    @Environment(\.showTeamChat) private var showTeamChat
    let row: TeamChatRow
    let room: TeamRoom
    /// The other person of a direct chat. Nil for the Team chat.
    let peer: TeamDirectPeer?
    /// The new text of an edit on its way.
    let editedText: String?
    let canWrite: Bool
    let actions: TeamMessageActions
    @State private var held = false

    private var own: Bool { row.writer == .you }
    private var message: TeamMessage? { row.message }
    private var pending: TeamPendingMessage? { if case .pending(let pending) = row.content { pending } else { nil } }
    private var text: String { (editedText ?? row.text).trimmingCharacters(in: .newlines) }
    /// The text that the bubble draws: the `@` name of this person in bold when the message names the person.
    private var shownText: String { TeamMentionEmphasis.text(text, message: message, room: room) }
    private var teamBot: TeamRoomBot? { message?.bot.flatMap { id in room.botList.first { $0.id == id } } }
    private var writerName: String {
        if let message, message.bot != nil { return teamBot?.name ?? message.botName ?? "Bot" }
        return message.map { TeamNames.author(of: $0, peer: peer?.name) } ?? "You"
    }
    /// Only a saved message of this person can change. A message of a bot of this Mac belongs to the bot.
    private var canChange: Bool { canWrite && message?.isOwn == true }
    /// An own message, and for the admin of the team also a message of another person or of a bot.
    private var canDelete: Bool {
        guard canWrite, let message else { return false }
        return TeamMessageDeletion.allows(message, in: room, direct: peer != nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            if row.startsRun && !own && peer == nil { writerLabel }
            bubbleRow.modifier(MessageTimestamp(createdAt: row.createdAt))
            // Under the bubble, on its side, in the style of the name above it. The bubble keeps the width of its text.
            if message?.isEdited == true || editedText != nil {
                Text("Edited").font(.footnote).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: own ? .trailing : .leading)
                    .padding(.horizontal, BoardStyle.Space.m)
                    .accessibilityHidden(true)
            }
        }
    }

    /// One form for a bot and a person: a small face, then the name. The name of a person that can get a direct
    /// message opens the direct chat with that person.
    @ViewBuilder private var writerLabel: some View {
        if let message, message.bot == nil, let member = room.directMember(message.member), let showTeamChat {
            Button { showTeamChat(TeamChatKey(room: room.room, member: member.member)) } label: { writerContent }
                .buttonStyle(.plain)
                .accessibilityLabel("Message " + member.name)
                .accessibilityIdentifier("team-writer-\(member.member)")
        } else {
            writerContent.accessibilityHidden(true)
        }
    }

    private var writerContent: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if message?.bot != nil {
                // The face and the color of the team bot, or a neutral face for a bot that the team no longer has.
                BotAvatar(symbol: teamBot?.avatar ?? "", color: teamBot?.color ?? "", size: 18,
                          identity: teamBot?.id ?? "", animated: false)
            } else {
                let memberId = message?.member ?? ""
                PersonFace(member: room.memberList.first { $0.member == memberId }, memberId: memberId,
                           name: message?.memberName, size: 18)
            }
            Text(writerName)
        }
        .font(.footnote).foregroundStyle(.secondary).lineLimit(1)
        // The name starts at the edge of the text in the bubble.
        .padding(.horizontal, BoardStyle.Space.m)
        .contentShape(Rectangle())
    }

    private var bubbleRow: some View {
        HStack {
            if own { Spacer(minLength: 36) }
            bubble
            if !own { Spacer(minLength: 18) }
        }
        .modifier(SwipeToReply(enabled: canWrite && message != nil) { if let message { actions.reply(message) } })
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let reference = quote {
                Button { if let id = message?.replyTo ?? pending?.replyTo { actions.showMessage(id) } } label: {
                    MessageQuote(reference: reference)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(reference.author.isEmpty ? reference.text : "View replied message from \(reference.author)")
                .accessibilityIdentifier("team-message-quote-\(row.id)")
            }
            MessageText(text: shownText, linkColor: own ? BoardStyle.userBubbleLink : .blue, allowsTextSelection: false)
                .accessibilityLabel(Text(spokenLabel))
                .accessibilityActions { accessibilityMenu }
                .accessibilityIdentifier("team-message-\(row.id)")
            if let pending, pending.failed {
                NotSentControls(id: pending.id, retry: { actions.retry(pending.id) },
                                delete: { actions.removeUnsent(pending.id) })
            }
        }
        .modifier(ChatBubbleBackground(own: own))
        #if os(iOS)
        .simultaneousGesture(LongPressGesture(minimumDuration: 0.45).onEnded { _ in
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            // The held message fades in over the chat, without the slide of a full-screen cover.
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { held = true }
        })
        .fullScreenCover(isPresented: $held) {
            HeldBubbleView(trailing: own, actions: menuActions, share: .text(row.text), afterShare: changeActions) { _ in
                EmptyView()
            } preview: { maxHeight in
                HeldTextPreview(text: text, own: own, maxHeight: maxHeight)
            }
        }
        #else
        // A Mac shows the actions of a message in the menu of a secondary click.
        .contextMenu {
            if let message, canWrite {
                Button("Reply", systemImage: "arrowshape.turn.up.left") { actions.reply(message) }
            }
            Button("Copy", systemImage: "doc.on.doc") { Pasteboard.copy(row.text) }
            if let message, canChange {
                Button("Edit", systemImage: "pencil") { actions.edit(message) }
            }
            if let message, canDelete {
                Button("Delete", systemImage: "trash", role: .destructive) { actions.delete(message) }
            }
        }
        #endif
    }

    #if os(iOS)
    /// Reply and Copy, in the order of the bot chat. Share follows them.
    private var menuActions: [HeldAction] {
        var items: [HeldAction] = []
        if let message, canWrite {
            items.append(HeldAction(title: "Reply", systemImage: "arrowshape.turn.up.left") { actions.reply(message) })
        }
        items.append(HeldAction(title: "Copy", systemImage: "doc.on.doc") { Pasteboard.copy(row.text) })
        return items
    }

    /// Edit of an own message and Delete, after Share. The destructive action is the last row.
    private var changeActions: [HeldAction] {
        guard let message else { return [] }
        var items: [HeldAction] = []
        if canChange { items.append(HeldAction(title: "Edit", systemImage: "pencil") { actions.edit(message) }) }
        if canDelete {
            items.append(HeldAction(title: "Delete", systemImage: "trash", destructive: true) { actions.delete(message) })
        }
        return items
    }
    #endif

    /// VoiceOver gets the same actions as the menu.
    @ViewBuilder private var accessibilityMenu: some View {
        if let message, canWrite { Button("Reply") { actions.reply(message) } }
        Button("Copy") { Pasteboard.copy(row.text) }
        if let message, canChange { Button("Edit") { actions.edit(message) } }
        if let message, canDelete { Button("Delete") { actions.delete(message) } }
    }

    /// VoiceOver reads the writer, the time and the text.
    private var spokenLabel: String {
        let time = row.createdAt.apiDate?.formatted(date: .abbreviated, time: .shortened)
        var parts = [writerName]
        if let time { parts.append(time) }
        parts.append(text)
        if message?.isEdited == true || editedText != nil { parts.append("Edited") }
        if pending?.failed == true { parts.append("Not sent") }
        if TeamMentionEmphasis.namesMe(message, room: room) { parts.append(TeamMentionEmphasis.spoken) }
        return parts.joined(separator: ", ")
    }

    /// The quote of an answered message. A removed message shows "Message removed".
    private var quote: MessageReference? {
        let replyTo = message?.replyTo ?? pending?.replyTo
        guard let replyTo else { return nil }
        guard let reply = message?.reply ?? pending?.reply else {
            return MessageReference(messageId: replyTo, text: "Message removed", author: "", role: "")
        }
        return MessageReference(messageId: replyTo, text: reply.text ?? "",
                                author: TeamNames.author(of: reply, ownMember: room.ownMember?.member, peer: peer?.name),
                                role: "")
    }
}

/// The composer of the Team chat: text only. A reply or an edit shows its message above the field, with a way to cancel.
/// `@` opens the list of the team bots and the people, as in the composer of a bot chat. An edit changes only the text,
/// so it has no list.
struct TeamChatComposer: View {
    /// "Message Example Team", or "Message Grace" in a direct chat.
    let placeholder: String
    /// The rows that `@` lists (TeamMentions.targets). None in a direct chat.
    let targets: [TeamMentionTarget]
    /// The name of the other person of a direct chat, for the quote of a reply. Nil for the Team chat.
    let peer: String?
    @Binding var text: String
    @Binding var mentions: [SelectedBotMention]
    @State private var selection: TextSelection?
    @Binding var replyTo: TeamMessage?
    @Binding var editing: TeamMessage?
    @FocusState.Binding var focused: Bool
    let enabled: Bool
    let send: () -> Void

    private var canSend: Bool { enabled && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        VStack(spacing: 0) {
            if let editing {
                ComposerQuoteBar(reference: MessageReference(messageId: editing.id, text: editing.text,
                                                             author: "Edit message", role: ""),
                                 cancelLabel: "Cancel edit") {
                    self.editing = nil
                    text = ""
                }
                .accessibilityIdentifier("edit-preview")
            } else if let replyTo {
                ComposerQuoteBar(reference: MessageReference(messageId: replyTo.id, text: replyTo.text,
                                                             author: TeamNames.author(of: replyTo, peer: peer), role: ""),
                                 cancelLabel: "Cancel reply") { self.replyTo = nil }
                    .accessibilityIdentifier("reply-preview")
            }
            if editing == nil && !targets.isEmpty {
                BotMentionControls(text: $text, selection: $selection, selected: $mentions,
                                   bots: targets, focused: focused) { focused = true }
            }
            HStack(alignment: .bottom, spacing: 4) {
                TextField(placeholder, text: $text, selection: $selection, axis: .vertical)
                    .focused($focused)
                    .modifier(ComposerFieldStyle(send: { if canSend { send() } }))
                    .accessibilityIdentifier("message-input")
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ComposerSendButton(symbol: editing == nil ? "arrow.up" : "checkmark", enabled: canSend,
                                       label: editing == nil ? "Send message" : "Save edit", action: send)
                }
            }
            .padding(.leading, BoardStyle.Space.m).padding(.trailing, BoardStyle.Space.xs)
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)))
            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .modifier(ComposerBackdrop())
        // A sent or cancelled draft leaves no caret in the old text.
        .onChange(of: text) { _, value in if value.isEmpty { selection = nil } }
    }
}

/// A round face with letters on a fill of `BoardStyle.faceColors`, or a symbol on the neutral surface. It has the size
/// of a bot face. Teams and people without a face of their own use it.
struct LetterFaceView: View {
    /// Nil shows the symbol on the neutral surface.
    let letters: String?
    /// The ID that picks the fill.
    let id: String
    let symbol: String
    let size: CGFloat

    var body: some View {
        let color = BoardStyle.faceColors[LetterFace.colorIndex(id)]
        Group {
            if let letters {
                Circle().fill(Color(hex: color.fill))
                    .overlay {
                        Text(letters)
                            .font(.system(size: size * (letters.count > 1 ? 0.4 : 0.5), weight: .semibold, design: .rounded))
                            .foregroundStyle(Color(hex: color.ink))
                            .minimumScaleFactor(0.5)
                    }
            } else {
                Circle().fill(BoardStyle.surface)
                    .overlay {
                        Image(systemName: symbol)
                            .font(.system(size: size * 0.36, weight: .medium))
                            .foregroundStyle(.secondary)
                    }
            }
        }
        .frame(width: size * BotAvatar.scale, height: size * BotAvatar.scale)
        .accessibilityHidden(true)
    }
}

/// The face of a team: the letters of its name on the fill of its room ID, so a new name keeps the fill.
struct TeamFace: View {
    let room: TeamRoom
    let size: CGFloat

    var body: some View {
        LetterFaceView(letters: LetterFace.letters(room.name), id: room.room, symbol: "person.2.fill", size: size)
    }
}

/// The face of a person in the chat: the face that the member chose, else the first letter of the name of the message
/// on the fill of its member ID (`LetterFace.person`). A member that left keeps that face, because both come from the
/// message. Only a writer whose name the Mac never learned ("Former member") has the neutral face.
struct PersonFace: View {
    let member: TeamMember?
    let memberId: String
    let name: String?
    let size: CGFloat

    var body: some View {
        if let member, let avatar = member.avatar, !avatar.isEmpty {
            BotAvatar(symbol: avatar, color: member.color ?? "", size: size, identity: memberId, animated: false)
        } else {
            LetterFaceView(letters: LetterFace.person(memberId: memberId, name: name).letters, id: memberId,
                           symbol: "person.fill", size: size)
        }
    }
}

/// The title of a team chat: its face, its name, and one quiet line that says who is in the chat. A named chat has its
/// name and the name of the team under it.
struct TeamTitle: View {
    let room: TeamRoom
    let faceSize: CGFloat

    /// What VoiceOver reads for the title: "Example Team team, 3 people", or "Design, Example Team" for a named chat.
    static func spoken(_ room: TeamRoom) -> String {
        if let chat = room.shownChat { return chat.name + ", " + room.name }
        return [room.name + " team", room.whoLine].compactMap { $0 }.joined(separator: ", ")
    }

    var body: some View {
        HStack(spacing: 8) {
            TeamFace(room: room, size: faceSize)
            VStack(alignment: .leading, spacing: 0) {
                Text(room.title).font(.body.weight(.semibold)).lineLimit(1).truncationMode(.tail)
                if room.shownChat != nil {
                    Text(room.name).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                        .accessibilityIdentifier("team-chat-who")
                } else if let line = room.whoLine {
                    Text(line).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                        .accessibilityIdentifier("team-chat-who")
                }
            }
        }
    }
}

/// The title of a direct chat: the face and the name of the person, and the name of the team under it, in the place
/// and the form of the title of a team.
struct DirectTitle: View {
    let peer: TeamDirectPeer
    let team: String
    let faceSize: CGFloat

    var body: some View {
        HStack(spacing: 8) {
            PersonFace(member: peer.entry, memberId: peer.member, name: peer.name, size: faceSize)
            VStack(alignment: .leading, spacing: 0) {
                Text(peer.name).font(.body.weight(.semibold)).lineLimit(1).truncationMode(.tail)
                Text(team).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}

extension View {
    /// Sets `clock` to now when a list of who types arrives, and again at each end of a subject. A view that shows who
    /// types at `clock` then hides a subject without a request.
    func typingClock(_ typing: TeamTypingList?, _ clock: Binding<Date>) -> some View {
        task(id: typing) {
            clock.wrappedValue = .now
            for end in typing?.ends ?? [] where end > clock.wrappedValue {
                try? await Task.sleep(for: .seconds(end.timeIntervalSinceNow))
                guard !Task.isCancelled else { return }
                clock.wrappedValue = .now
            }
        }
    }
}

/// Who types in the Team chat, in the form of the status row of a bot chat (BotStatusRow): the small face of each
/// subject, the text, and the dots. VoiceOver reads the row as one element.
struct TeamTypingRow: View {
    let room: TeamRoom
    let subjects: [TeamTyping]
    /// The height of one line of the text, so the faces stand at the middle of the first line.
    @ScaledMetric(relativeTo: .subheadline) private var lineHeight: CGFloat = 20
    /// The gap between the last word and the dots, as before an ellipsis.
    @ScaledMetric(relativeTo: .subheadline) private var dotsGap: CGFloat = 4

    private var text: String { TeamTypingText.text(subjects) ?? "" }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            HStack(spacing: 2) {
                ForEach(subjects.prefix(3)) { face($0) }
            }
            .frame(height: lineHeight)
            // The dots end the sentence: their bottom sits on the baseline of the last line of the text.
            HStack(alignment: .lastTextBaseline, spacing: dotsGap) {
                Text(text).foregroundStyle(.secondary).lineLimit(2)
                TypingDots().alignmentGuide(.lastTextBaseline) { $0[.bottom] }
            }
        }
        .font(.subheadline)
        .frame(minHeight: 32, alignment: .leading)
        // The text starts at the edge of the text in a bubble, as the name above a message.
        .padding(.leading, BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
        .accessibilityIdentifier("team-typing")
    }

    /// The face of a bot of the team, or of a person, as above a message of the chat.
    @ViewBuilder private func face(_ subject: TeamTyping) -> some View {
        if let id = subject.bot {
            let bot = room.botList.first { $0.id == id }
            BotAvatar(symbol: bot?.avatar ?? "", color: bot?.color ?? "", size: 18, identity: id, animated: false)
        } else {
            PersonFace(member: room.memberList.first { $0.member == subject.member }, memberId: subject.member,
                       name: subject.name, size: 18)
        }
    }
}

/// The row of a team in the Chats list, in the layout of a bot row: the face, the name with the unread count, the age
/// of the newest message, and one line of it. A Mac with an older service sends no chat, so the row shows the name only.
struct TeamContactRow: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let room: TeamRoom
    var avatarSize: CGFloat = BoardStyle.rowFace

    /// The time of the last look at who types: the arrival of a list, or the end of a subject.
    @State private var typingClock = Date()

    var body: some View {
        content(TeamChatPreview.line(room, at: typingClock))
            .typingClock(room.typing, $typingClock)
    }

    private func content(_ line: TeamChatPreview.Line?) -> some View {
        let unread = room.unread ?? 0
        let date = room.lastMessage?.createdAt?.apiDate
        // An older service sends no chat. The row then has the name only, centered on the face. It has no "@" mark,
        // because the mark stands on the second line.
        return ChatRowLayout(name: room.title, unread: unread, date: date,
                             mentioned: room.hasChat && room.showsMentionMark) {
            TeamFace(room: room, size: avatarSize)
        } subtitle: {
            Group {
                // Who types takes the place of the newest message, as a working phase does in the row of a bot.
                if let line, line.typing {
                    // The shimmer has one line. The largest text sizes need two for the whole sentence.
                    Group {
                        if dynamicTypeSize.isAccessibilitySize {
                            Text(line.text).foregroundStyle(.secondary).lineLimit(2)
                        } else {
                            ShimmerText(text: line.text)
                        }
                    }
                    .font(.subheadline)
                    .transition(.opacity)
                    .accessibilityIdentifier("team-row-typing-\(room.room)" + (room.shownChat.map { "-" + $0.id } ?? ""))
                } else if let line {
                    Text(line.text)
                        .font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                        .transition(.opacity)
                }
            }
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: line)
        }
        .accessibilityElement(children: .combine)
        // VoiceOver reads who types in place of the newest message, as the row shows it.
        .accessibilityLabel(TeamRowSpeech.label(room, preview: line?.text))
    }
}

/// The row of a direct chat in the Chats list, in the layout of a team row: the face and the name of the person, the
/// age and one line of the newest message, and the unread count. A member that left keeps the last name of the Mac.
struct DirectContactRow: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let room: TeamRoom
    let direct: TeamDirect
    var avatarSize: CGFloat = BoardStyle.rowFace

    var body: some View {
        let peer = room.directPeer(direct.member)
        let unread = direct.unread ?? 0
        let preview = TeamChatPreview.text(direct.lastMessage, direct: true)
        ChatRowLayout(name: peer.name, unread: unread, date: direct.lastMessage?.createdAt?.apiDate) {
            PersonFace(member: peer.entry, memberId: peer.member, name: peer.name, size: avatarSize)
        } subtitle: {
            Text(preview)
                .font(.subheadline).foregroundStyle(.secondary)
                .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel([peer.name, preview, unread > 0 ? UnreadBadge.spoken(unread) : nil].compactMap { $0 }
            .joined(separator: ", "))
    }
}

/// The row of a chat of a team in the Chats list and the Mac sidebar: a team or a direct chat.
struct PeopleRow: View {
    let item: ChatListItem
    var avatarSize: CGFloat = BoardStyle.rowFace

    var body: some View {
        switch item {
        case .team(let room): TeamContactRow(room: room, avatarSize: avatarSize)
        case .direct(let room, let direct): DirectContactRow(room: room, direct: direct, avatarSize: avatarSize)
        case .bot: EmptyView()
        }
    }
}
