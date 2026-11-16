import SwiftUI

/// The page of a chat of a team on the stack of the Chats tab. A chat can take the place of another chat at the same
/// depth (the name above a message, a tapped notification). The identity of the chat makes it a new page: it loads its
/// own messages, and the draft, the reply quote and the edit of the first chat stay behind. The Mac window does the
/// same with `.id(chat)`. A page that "Chat about this page" quotes waits in the store under the key of its chat, so it
/// shows only in that chat.
struct TeamChatDestination: View {
    let route: TeamChatRoute

    var body: some View {
        TeamChatView(chatKey: route.chat).id(route.chat)
    }
}

/// The chat of a team: a bot chat with more than one other party (docs/team-log.md, "Team chat"). It reuses the bubble,
/// the quote, the day pill, the times and the composer of the bot chat, and adds only what a group needs: the name of
/// the writer above a run, Edit and Delete for the person's own messages, and Delete for the admin of the team
/// (TeamMessageDeletion).
///
/// The same view is the direct chat with one person of the team ("Direct messages"): its title is the person, no name
/// shows above a message, and the composer has no list for `@`. A direct chat with a bot of the team ("Direct chat
/// with an agent") is the same chat with the bot as the other side, and it shows while the bot types. It is also each
/// named chat of the team ("Chats of a team"): its title is the name of the chat, with the name of the team under it
/// (TeamRoom.inChat).
struct TeamChatView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismiss) private var dismiss
    let chatKey: TeamChatKey
    /// The draft, and the reply, the page or the edit above the field.
    @State private var composer = TeamComposerState()
    /// The composer gets the keyboard a moment after "Chat about this page" opened the chat, as in the chat of an own
    /// bot.
    @State private var focusAfterOpen: UUID?
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
    #if os(macOS)
    /// The height of the notice of the window above the composer (ChatWindowNotice).
    @State private var noticeHeight: CGFloat = 0
    #endif
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
    /// The other side of a direct chat: a person or a bot. Nil for the Team chat and a named chat.
    private func peer(_ room: TeamRoom) -> TeamDirectPeer? { room.peer(of: chatKey) }
    private func currentRows() -> [TeamChatRow] { rows.current(chat, calendar: .current) }
    /// The space that the last row of the chat keeps for the notice of the Mac window.
    private var noticeSpace: CGFloat {
        #if os(macOS)
        noticeHeight
        #else
        0
        #endif
    }

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
                    let writable = canWrite(room)
                    ForEach(allRows[shown]) { row in
                        let date = row.createdAt.apiDate
                        VStack(spacing: 0) {
                            if row.startsDay, let date {
                                DaySeparatorRow(date: date, now: dayReference, row: row.id, pinned: pinnedDay)
                            }
                            TeamMessageRow(row: row, room: room, peer: peer(room),
                                           editedText: row.message.flatMap { chat.editing[$0.id] },
                                           canWrite: writable,
                                           canReact: TeamReactions.offered(on: row, in: chatKey, canWrite: writable),
                                           actions: actions(proxy: proxy))
                        }
                        .pinnedDayRow(pinnedDay, id: row.id, date: date, startsDay: row.startsDay)
                        // Keep full text height when the list resizes or scrolls to a new message.
                        .fixedSize(horizontal: false, vertical: true)
                        .id(row.id)
                    }
                    // The last row: who types, under the newest message and above the composer, then the anchor of
                    // the bottom. The row is always there, so the list never adds or removes a row for typing while it
                    // scrolls to its bottom. A direct chat with a person shows nobody. The row inset and the padding
                    // keep 12 points below the last message, and the space of the notice of the Mac window.
                    VStack(alignment: .leading, spacing: 0) {
                        if !typing.isEmpty {
                            TeamTypingRow(room: room, subjects: typing)
                                .padding(.top, BoardStyle.Space.xs)
                                .transition(.opacity)
                        }
                        Color.clear.frame(height: 1)
                    }
                    .padding(.bottom, BoardStyle.Space.s + noticeSpace).id(Self.bottom)
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
        #if os(iOS)
        // The notice stays below the header at each scroll position, over the messages only.
        .modifier(ChatErrorNotice())
        #endif
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                #if os(macOS)
                // In the Mac window the notice stands above the composer. The last row keeps its space (noticeSpace).
                ChatWindowNotice(height: $noticeHeight)
                #endif
                footer(room)
            }
        }
        .task(id: store.connection) {
            // A load without a connection does nothing, so the chat loads again when the Mac connects.
            guard room.hasChat, store.connection == .connected || store.isDemo else { return }
            await store.loadTeamChat(chatKey)
        }
        .onAppear { isVisible = true; store.openTeamChat(chatKey); takePageQuote() }
        // "Chat about this page" opens the chat with the page quoted above the text field.
        .onChange(of: store.teamPageQuotes[chatKey]) { _, _ in takePageQuote() }
        // The view cancels this task when it leaves the screen, and a new ID when the request changes.
        .task(id: focusAfterOpen) {
            guard focusAfterOpen != nil else { return }
            try? await Task.sleep(for: .milliseconds(450))
            guard !Task.isCancelled, focusAfterOpen != nil else { return }
            focusAfterOpen = nil
            composerFocused = true
        }
        // The person can no longer write, for example because the bot left the team: the closed line takes the place
        // of the composer, and the page goes with it.
        .onChange(of: closedReason(room) != nil) { _, closed in if closed { composer.close() } }
        // A chat that closes keeps its newest page only, so it opens again without stale older pages.
        .onDisappear { isVisible = false; composerFocused = false; store.closeTeamChat(chatKey) }
        .onReceive(DaySeparator.changes) { _ in dayReference = Date() }
        .onChange(of: room.typingNow(in: chatKey, at: typingClock), initial: true) { _, value in
            withAnimation(BoardStyle.fade(reduceMotion: reduceMotion)) { typing = value }
        }
        .typingClock(room.typingList(of: chatKey), $typingClock)
        .onChange(of: composer.draft) { _, value in
            // The person types: the Mac tells the team. Not for an edit of a sent message, and not in a direct chat.
            guard composer.editing == nil, canWrite(room) else { return }
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
                    DirectPeerFace(peer: peer, size: 68)
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
            TeamChatComposer(placeholder: TeamDirectText.message(peer(room)?.name ?? room.title),
                             targets: mentionTargets(room), peer: peer(room)?.name, state: $composer,
                             mentions: $mentions, focused: $composerFocused,
                             enabled: canWrite(room), previews: chat.offersPreviews,
                             files: TeamFileRules.offered(in: chatKey, chat) ? composerFiles : nil, send: send)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Why the person cannot write: the reason of the team, else for a direct chat a member or a bot that can no longer
    /// get a direct message. The chat then shows its history only.
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
                composer.reply(to: message)
                composerFocused = true
            },
            edit: { message in
                composer.edit(message)
                mentions = []
                composerFocused = true
            },
            delete: { deleting = $0 },
            react: { message, emoji in store.reactToTeamMessage(chatKey, message, emoji: emoji) },
            showMessage: { id in show(id, proxy: proxy) },
            retry: { store.retryTeamMessage(chatKey, id: $0) },
            removeUnsent: { store.removeUnsentTeamMessage(chatKey, id: $0) })
    }

    private var composerFiles: TeamComposerFiles {
        TeamComposerFiles(upload: { await store.uploadTeamFile(chatKey, $0) },
                          drop: { store.dropTeamFile(chatKey, $0) },
                          report: { store.error = $0 })
    }

    private func send() {
        let text = composer.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let room, canWrite(room), composer.canSend(enabled: true) else { return }
        if let message = composer.editing {
            composer.cancelEdit()
            Task {
                // A failure gives the edit back to the composer, unless the person typed something new.
                if await !store.editTeamMessage(chatKey, message, text: text) {
                    composer.restore(edit: message, text: text)
                }
            }
        } else {
            // The store saves the message first. When the save fails, the composer keeps the text.
            // The mentions count in the draft before the trim: their places are places in the draft.
            let named = BotMentions.recipients(in: composer.draft, selected: mentions, bots: mentionTargets(room))
                .map(\.id)
            guard store.sendTeamMessage(chatKey, from: composer, mentions: named) else { return }
            composer.sent()
            mentions = []
        }
    }

    /// Takes the page that "Chat about this page" left for this chat (AppStore.takeTeamPageQuote). The page takes the
    /// place of a reply and of an edit.
    private func takePageQuote() {
        guard let page = store.takeTeamPageQuote(chatKey, onScreen: isVisible,
                                                 canWrite: room.map(canWrite) ?? false) else { return }
        composer.ask(about: page)
        // The chat follows its newest message. The keyboard opens after the push, as in the chat of an own bot.
        follows = true
        focusAfterOpen = UUID()
    }

    private func delete(_ message: TeamMessage) {
        composer.removed(message.id)
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
    /// Sets or takes back the reaction of this person (AppStore.reactToTeamMessage).
    let react: (TeamMessage, String) -> Void
    let showMessage: (String) -> Void
    let retry: (String) -> Void
    let removeUnsent: (String) -> Void
}

/// One message of the Team chat: the name of the writer above the first message of a run, then the bubble. A direct
/// chat has one other writer, so it shows no name.
struct TeamMessageRow: View {
    @Environment(\.showTeamChat) private var showTeamChat
    @Environment(\.openDocument) private var openDocument
    @Environment(\.pushRoute) private var pushRoute
    let row: TeamChatRow
    let room: TeamRoom
    /// The other side of a direct chat. Nil for the Team chat.
    let peer: TeamDirectPeer?
    /// The new text of an edit on its way.
    let editedText: String?
    let canWrite: Bool
    /// The chat offers reactions on this message (TeamReactions.offered).
    let canReact: Bool
    let actions: TeamMessageActions
    @State private var held = false
    @State private var showsReactions = false
    /// The file whose held view shows, on an iPhone.
    @State private var heldFile: TeamFile?

    private var own: Bool { row.writer == .you }
    private var message: TeamMessage? { row.message }
    private var pending: TeamPendingMessage? { if case .pending(let pending) = row.content { pending } else { nil } }
    private var text: String { (editedText ?? row.text).trimmingCharacters(in: .newlines) }
    /// The text that the bubble draws: the `@` name of this person in bold when the message names the person.
    private var shownText: String { TeamMentionEmphasis.text(text, message: message, room: room) }
    private var teamBot: TeamRoomBot? { message?.bot.flatMap { id in room.botList.first { $0.id == id } } }
    private var writerName: String {
        if let message, message.bot != nil { return teamBot?.name ?? message.botName ?? "Agent" }
        return message.map { TeamNames.author(of: $0, peer: peer?.name) } ?? "You"
    }
    /// Only a saved message of this person can change. A message of a bot of this Mac belongs to the bot.
    private var canChange: Bool { canWrite && message?.isOwn == true }
    /// An own message, and for the admin of the team also a message of another person or of a bot.
    private var canDelete: Bool {
        guard canWrite, let message else { return false }
        return TeamMessageDeletion.allows(message, in: room, direct: peer != nil)
    }

    /// The files stand above the bubble, in the components of the agent chat: the images in one tile row, each other
    /// file as a card. The text keeps its bubble under them. A message without a text and without a quote has no
    /// bubble.
    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            if row.startsRun && !own && peer == nil { writerLabel }
            if row.files.isEmpty {
                bubbleRow.modifier(MessageTimestamp(createdAt: row.createdAt))
            } else {
                VStack(alignment: own ? .trailing : .leading, spacing: BoardStyle.Space.xs) {
                    let photos = row.photos(room: room.room)
                    if !photos.isEmpty { photoRow(photos) }
                    ForEach(row.documents) { file in fileCard(file) }
                    if hasBubble {
                        bubbleRow
                    } else if let pending, pending.failed {
                        NotSentControls(id: pending.id, retry: { actions.retry(pending.id) },
                                        delete: { actions.removeUnsent(pending.id) })
                    }
                }
                .frame(maxWidth: .infinity, alignment: own ? .trailing : .leading)
                .modifier(MessageTimestamp(createdAt: row.createdAt))
                #if os(iOS)
                .fullScreenCover(item: $heldFile) { file in
                    if let source = row.address(of: file, room: room.room) { heldView(file, source: source) }
                }
                #endif
            }
            // Under the bubble, on its side, in the style of the name above it: the reactions, then "Edited", in one
            // line. The bubble keeps the width of its text.
            let edited = message?.isEdited == true || editedText != nil
            let summary = TeamReactionSummary(message?.reactions)
            if edited || summary != nil {
                HStack(spacing: BoardStyle.Space.s) {
                    if own, edited { editedLabel }
                    if let summary { reactionPill(summary) }
                    if !own, edited { editedLabel }
                }
                .frame(maxWidth: .infinity, alignment: own ? .trailing : .leading)
                // The first word starts at the edge of the text in the bubble: "Edited", or the first emoji in the
                // capsule.
                .padding(.horizontal, summary == nil ? BoardStyle.Space.m : BoardStyle.Space.xs)
            }
        }
    }

    /// The text, a quote or a link preview needs the bubble.
    private var hasBubble: Bool {
        !text.isEmpty || TeamRowQuote(message: message, pending: pending) != nil || row.linkPreview != nil
    }

    /// The images of the message in the tile row of the agent chat, on the side of the writer.
    private func photoRow(_ photos: [MessagePhoto]) -> some View {
        let images = row.files.filter(\.isImage)
        return PhotoTileRow(photos: photos, trailing: own,
                            hold: { index in if index < images.count { holdFile(images[index]) } },
                            identifier: "team-photo-\(row.id)", menuOnMac: true) { index in
            if index < images.count, let source = row.address(of: images[index], room: room.room) {
                fileActions(images[index], source: source)
            }
        } below: { _ in EmptyView() }
    }

    /// A file that is no image, in the form of a file card of the agent chat, on the side of the writer.
    private func fileCard(_ file: TeamFile) -> some View {
        let source = row.address(of: file, room: room.room)
        return ChatFileCard(file: file, source: source)
            .frame(maxWidth: ChatFileCard.width)
            #if os(iOS)
            .simultaneousGesture(LongPressGesture(minimumDuration: 0.45).onEnded { _ in holdFile(file) })
            #else
            .contextMenu { if let source { fileActions(file, source: source) } }
            #endif
            .accessibilityActions { if let source { fileActions(file, source: source) } }
            .accessibilityIdentifier("team-file-\(row.id)-\(file.id)")
    }

    private func holdFile(_ file: TeamFile) {
        #if os(iOS)
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        // The held message fades in over the chat, without the slide of a full-screen cover.
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { heldFile = file }
        #endif
    }

    /// The team actions of a text message for a file: reactions, Reply, Copy, Edit and Delete. Copy takes the image or
    /// the file, not a link.
    @ViewBuilder private func fileActions(_ file: TeamFile, source: URL) -> some View {
        #if os(macOS)
        if canReact, let message {
            ReactionMenuRow(emoji: TeamReactions.all, marked: { message.ownReaction == $0 },
                            label: { TeamReactions.action($0, on: message) }) { actions.react(message, $0) }
        }
        if let message, canWrite { Button("Reply", systemImage: "arrowshape.turn.up.left") { actions.reply(message) } }
        #else
        if let message, canWrite { Button("Reply") { actions.reply(message) } }
        if canReact, let message {
            ForEach(TeamReactions.all, id: \.self) { emoji in
                Button(TeamReactions.action(emoji, on: message)) { actions.react(message, emoji) }
            }
        }
        #endif
        if file.isReady { Button("Copy", systemImage: "doc.on.doc") { TeamFileCopy.copy(file, source: source) } }
        if let message, canChange { Button("Edit", systemImage: "pencil") { actions.edit(message) } }
        if let message, canDelete {
            Button("Delete", systemImage: "trash", role: .destructive) { actions.delete(message) }
        }
    }

    #if os(iOS)
    /// A held image or file: the reactions above, the image or the card, then Reply, Copy, Share, and Edit and Delete
    /// as a held text message has them. Copy and Share take the image or the file.
    private func heldView(_ file: TeamFile, source: URL) -> some View {
        HeldTeamFile(file: file, source: source, own: own,
                     reply: message.flatMap { message in
                         canWrite ? HeldAction(title: "Reply", systemImage: "arrowshape.turn.up.left") { actions.reply(message) } : nil
                     },
                     afterShare: changeActions) { close in
            if canReact, let message {
                HeldReactionRow(emoji: TeamReactions.all, marked: { message.ownReaction == $0 },
                                label: { TeamReactions.action($0, on: message) }) { emoji in
                    close { actions.react(message, emoji) }
                }
            }
        }
    }
    #endif

    private var editedLabel: some View {
        Text("Edited").font(.footnote).foregroundStyle(.secondary).accessibilityHidden(true)
    }

    /// The reactions on the message. A tap opens the list of the people: a sheet on the iPhone, a popover on a Mac.
    private func reactionPill(_ summary: TeamReactionSummary) -> some View {
        Button { showsReactions = true } label: {
            (Text(summary.emoji.joined())
             + Text(summary.count > 1 ? " \(summary.count)" : "").foregroundStyle(.secondary))
                .font(.footnote).monospacedDigit()
                .padding(.horizontal, BoardStyle.Space.xs).padding(.vertical, BoardStyle.Space.line)
                .modifier(ReactionCapsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(summary.spoken)
        .accessibilityIdentifier("team-reactions-\(row.id)")
        #if os(iOS)
        .sheet(isPresented: $showsReactions) {
            reactionList.presentationDetents([.medium]).presentationDragIndicator(.visible)
        }
        #else
        .popover(isPresented: $showsReactions, arrowEdge: .bottom) { reactionList.frame(width: 260) }
        #endif
    }

    private var reactionList: some View {
        TeamReactionList(people: TeamReactionPerson.list(message?.reactions, room: room, peer: peer),
                         canRemove: canReact) { emoji in
            if let message { actions.react(message, emoji) }
        }
    }

    /// One form for a bot and a person: a small face, then the name. The name of a person or of a bot that can get a
    /// direct message opens the direct chat with it.
    @ViewBuilder private var writerLabel: some View {
        if let message, message.bot == nil, let member = room.directMember(message.member), let showTeamChat {
            Button { showTeamChat(TeamChatKey(room: room.room, member: member.member)) } label: { writerContent }
                .buttonStyle(.plain)
                .accessibilityLabel(TeamDirectText.message(member.name))
                .accessibilityIdentifier("team-writer-\(member.member)")
        } else if let bot = room.directBot(message?.bot), let showTeamChat {
            Button { showTeamChat(TeamChatKey(room: room.room, bot: bot.id)) } label: { writerContent }
                .buttonStyle(.plain)
                .accessibilityLabel(TeamDirectText.message(bot.name))
                .accessibilityIdentifier("team-writer-\(bot.id)")
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
        BubbleStack(spacing: 4) {
            switch TeamRowQuote(message: message, pending: pending) {
            case .page(let page): pageQuote(page)
            case .reply(let id):
                let reference = quote(of: id)
                Button { actions.showMessage(id) } label: { MessageQuote(reference: reference) }
                    .buttonStyle(.plain)
                    .accessibilityLabel(reference.author.isEmpty ? reference.text
                                        : "View replied message from \(reference.author)")
                    .accessibilityIdentifier("team-message-quote-\(row.id)")
            case nil: EmptyView()
            }
            if let preview = row.linkPreview {
                TeamLinkCard(preview: preview).layoutValue(key: BubbleStack.CardWidth.self, value: TeamLinkCard.maxWidth)
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
            HeldBubbleView(trailing: own, actions: menuActions, share: .text(row.text), afterShare: changeActions) { close in
                if canReact, let message {
                    HeldReactionRow(emoji: TeamReactions.all, marked: { message.ownReaction == $0 },
                                    label: { TeamReactions.action($0, on: message) }) { emoji in
                        close { actions.react(message, emoji) }
                    }
                }
            } preview: { maxHeight in
                HeldTextPreview(text: text, own: own, maxHeight: maxHeight)
            }
        }
        #else
        // A Mac shows the actions of a message in the menu of a secondary click.
        .contextMenu {
            if canReact, let message {
                ReactionMenuRow(emoji: TeamReactions.all, marked: { message.ownReaction == $0 },
                                label: { TeamReactions.action($0, on: message) }) { actions.react(message, $0) }
            }
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
        if canReact, let message {
            ForEach(TeamReactions.all, id: \.self) { emoji in
                Button(TeamReactions.action(emoji, on: message)) { actions.react(message, emoji) }
            }
        }
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

    /// The Library page that the message is about, as the chat of an own bot shows it. A tap opens the page in the
    /// Library. A page that is gone says so and opens nothing.
    @ViewBuilder private func pageQuote(_ page: TeamAboutPage) -> some View {
        if let open = page.open(openDocument: openDocument, pushRoute: pushRoute) {
            Button(action: open) { MessageQuote(reference: page.quote) }
            .buttonStyle(.plain)
            .accessibilityLabel(PageQuoteText.open(page.title ?? ""))
            .accessibilityIdentifier("team-message-page-\(row.id)")
        } else {
            MessageQuote(reference: page.quote)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("team-message-page-\(row.id)")
        }
    }

    /// The quote of an answered message. A removed message shows "Message removed".
    private func quote(of replyTo: String) -> MessageReference {
        guard let reply = message?.reply ?? pending?.reply else {
            return MessageReference(messageId: replyTo, text: "Message removed", author: "", role: "")
        }
        return MessageReference(messageId: replyTo, text: reply.text ?? "",
                                author: TeamNames.author(of: reply, ownMember: room.ownMember?.member, peer: peer?.name),
                                role: "")
    }
}

/// The people that reacted to a message: the emoji and the name of each. The own row is first and takes the reaction
/// back with a tap, then the list closes.
struct TeamReactionList: View {
    @Environment(\.dismiss) private var dismiss
    let people: [TeamReactionPerson]
    /// False where the person cannot change a reaction, for example in a team that ended.
    let canRemove: Bool
    /// Takes back the reaction of this person: the same emoji again.
    let remove: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Reactions").font(.headline)
                .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.m)
            // A short list takes its own height, so a popover on a Mac fits it. A long list scrolls.
            ViewThatFits(in: .vertical) {
                rows
                ScrollView { rows }
            }
        }
        .padding(.top, BoardStyle.Space.s)
        // The list starts at the top of the sheet.
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(people) { person in
                if person.own && canRemove {
                    Button {
                        dismiss()
                        remove(person.emoji)
                    } label: { row(person) }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("team-reaction-remove")
                } else {
                    row(person)
                }
            }
        }
    }

    private func row(_ person: TeamReactionPerson) -> some View {
        HStack(spacing: BoardStyle.Space.m) {
            Text(person.emoji).font(.title3)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(person.name).font(.body)
                if person.own && canRemove {
                    Text("Tap to remove").font(.footnote).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, BoardStyle.Space.l)
        .frame(minHeight: 44)
        .padding(.vertical, BoardStyle.Space.xs)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

/// What the composer of a team chat does with files: the chat uploads a picked file as a draft, and drops a removed
/// one. Nil where the chat takes no file (TeamFileRules.offered).
struct TeamComposerFiles {
    let upload: (PickedFile) async -> TeamFile?
    let drop: (TeamFile) -> Void
    /// A text for the error banner.
    let report: (String) -> Void
}

/// The composer of the Team chat: the text, and up to four files where the Mac takes them. A reply or an edit shows its
/// message above the field, with a way to cancel. In a direct chat with a bot, a Library page shows there in the same
/// form. `@` opens the list of the team bots and the people, as in the composer of a bot chat. An edit changes only the
/// text, so it has no list and no plus control.
struct TeamChatComposer: View {
    /// "Message Example Team", or "Message Grace" in a direct chat.
    let placeholder: String
    /// The rows that `@` lists (TeamMentions.targets). None in a direct chat.
    let targets: [TeamMentionTarget]
    /// The name of the other person of a direct chat, for the quote of a reply. Nil for the Team chat.
    let peer: String?
    /// The draft, and the reply, the page or the edit above the field.
    @Binding var state: TeamComposerState
    @Binding var mentions: [SelectedBotMention]
    @State private var selection: TextSelection?
    @FocusState.Binding var focused: Bool
    let enabled: Bool
    /// The Mac of the chat takes link previews (TeamChat.offersPreviews). Without it the composer reads no link.
    var previews = false
    var files: TeamComposerFiles? = nil
    let send: () -> Void
    @State private var importing = false
    @State private var pickingPhotos = false

    private var text: String { state.draft }
    /// The link whose preview the composer shows: the first link of a new message, while the Mac takes previews.
    private var previewLink: TeamDraftLink? { previews && editing == nil ? TeamDraftLink.first(in: text) : nil }
    private var editing: TeamMessage? { state.editing }
    private var canSend: Bool { state.canSend(enabled: enabled) }
    /// Send shows with a text, or with a file of a new message.
    private var showsSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || (editing == nil && !state.files.isEmpty)
            || (editing.map { !$0.files.isEmpty } ?? false)
    }

    var body: some View {
        VStack(spacing: 0) {
            if let editing {
                ComposerQuoteBar(reference: MessageReference(messageId: editing.id, text: editing.shortText,
                                                             author: "Edit message", role: ""),
                                 cancelLabel: "Cancel edit") { state.cancelEdit() }
                .accessibilityIdentifier("edit-preview")
            } else if let replyTo = state.quote?.reply {
                ComposerQuoteBar(reference: MessageReference(messageId: replyTo.id, text: replyTo.shortText,
                                                             author: TeamNames.author(of: replyTo, peer: peer), role: ""),
                                 cancelLabel: "Cancel reply") { state.removeQuote() }
                    .accessibilityIdentifier("reply-preview")
            } else if let page = state.quote?.page {
                ComposerQuoteBar(reference: page.quote, cancelLabel: PageQuoteText.remove) { state.removeQuote() }
                    .accessibilityIdentifier("reply-preview")
            }
            if editing == nil, let preview = state.linkPreview {
                ComposerLinkBar(preview: preview) { state.removePreview() }
            }
            if editing == nil && !targets.isEmpty {
                BotMentionControls(text: $state.draft, selection: $selection, selected: $mentions,
                                   bots: targets, focused: focused) { focused = true }
            }
            if editing == nil {
                ComposerAttachmentChips(chips: state.files.map { ComposerChip(id: $0.id, title: $0.name) }) { id in
                    if let file = state.removeFile(id) { files?.drop(file) }
                }
            }
            HStack(alignment: .bottom, spacing: BoardStyle.Space.s) {
                if files != nil && editing == nil {
                    ComposerPlusMenu(uploading: state.uploading > 0, enabled: enabled) {
                        AttachMenuItems(importing: $importing, pickingPhotos: $pickingPhotos)
                    }
                }
                HStack(alignment: .bottom, spacing: 4) {
                    TextField(placeholder, text: $state.draft, selection: $selection, axis: .vertical)
                        .focused($focused)
                        .modifier(ComposerFieldStyle(send: { if canSend { send() } }))
                        .accessibilityIdentifier("message-input")
                    if showsSend {
                        ComposerSendButton(symbol: editing == nil ? "arrow.up" : "checkmark", enabled: canSend,
                                           label: editing == nil ? "Send message" : "Save edit", action: send)
                    }
                }
                .padding(.leading, BoardStyle.Space.m).padding(.trailing, BoardStyle.Space.xs)
                .modifier(ComposerClickArea(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)) {
                    focused = true
                })
                .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)))
            }
            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .modifier(ComposerBackdrop())
        .modifier(ComposerAttachmentSources(importing: $importing, pickingPhotos: $pickingPhotos,
                                            photoLimit: TeamFileRules.limit - state.files.count - state.uploading,
                                            pasteEnabled: files != nil && editing == nil && focused && enabled) { url in
            guard let picked = PickedFile.read(url) else { return report(TeamFileRules.sizeText) }
            add { picked }
        } photo: { add($0) } paste: { picked in add { picked } } report: { report($0) })
        // A sent or cancelled draft leaves no caret in the old text.
        .onChange(of: text) { _, value in if value.isEmpty { selection = nil } }
        // A new first link drops the old preview at once, and its page loads after a pause in typing. The send never
        // waits for it.
        .task(id: previewLink) {
            let link = previewLink
            state.linkChanged(to: link?.text)
            guard let link, state.removedLink != link.text, state.linkPreview == nil else { return }
            let preview = await TeamComposerPreview.load(link) { await LinkPreviewLoader.shared.preview($0) }
            guard !Task.isCancelled else { return }
            state.previewLoaded(preview, for: link.text)
        }
    }

    /// A picked file counts as on its way from the pick, and goes to the Mac as a draft when it has loaded. Its chip
    /// shows when the Mac has it and each earlier pick is done, so the chips keep the order of the picks. The uploads
    /// run side by side. A fifth file says why it does not fit.
    private func add(_ load: @escaping @MainActor () async -> PickedFile?) {
        guard let files else { return }
        guard let id = state.startUpload() else { return report(TeamFileRules.limitText) }
        Task {
            let file: TeamFile? = if let picked = await load() { await files.upload(picked) } else { nil }
            state.finishUpload(id, file)
        }
    }

    private func report(_ text: String) { files?.report(text) }
}

/// The link preview of the draft above the field, in the form of the reply bar: the title and the site of the page,
/// and a control that removes the preview from the message.
struct ComposerLinkBar: View {
    let preview: TeamLinkPreview
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            HStack(spacing: 8) {
                Capsule().fill(.secondary).frame(width: 3)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(preview.title).font(.footnote.weight(.semibold)).lineLimit(1)
                    Text(preview.site).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .fixedSize(horizontal: false, vertical: true).padding(.vertical, BoardStyle.Space.xs)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(preview.spoken)
            .accessibilityIdentifier("link-preview-bar")
            Button(action: remove) {
                Image(systemName: "xmark").foregroundStyle(.secondary).frame(minWidth: 44, minHeight: 44)
            }
            .accessibilityLabel("Remove preview")
            .accessibilityIdentifier("link-preview-remove")
        }
        .padding(.leading, BoardStyle.Space.m).padding(.trailing, BoardStyle.Space.xs).padding(.vertical, BoardStyle.Space.xs)
        .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous)))
        .padding(.horizontal, BoardStyle.Space.m).padding(.bottom, BoardStyle.Space.s)
    }
}

/// The link preview of a message, at the top of its bubble: the title and the site that the app of the writer sent,
/// text only. It loads nothing. A tap opens the address with the system.
struct TeamLinkCard: View {
    /// The widest card, as the card of the agent chat. A card never makes a bubble wider than a text of this width.
    static let maxWidth: CGFloat = 280
    @Environment(\.openURL) private var openURL
    let preview: TeamLinkPreview

    var body: some View {
        Button { if let address = preview.address { openURL(address) } } label: {
            // The line fill reads on each bubble, and the corner follows the corner of the bubble.
            LinkPreviewText(title: preview.title, site: preview.site)
                .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
                .background(BoardStyle.line, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(preview.spoken)
        .accessibilityHint("Opens the link")
        .accessibilityAddTraits(.isLink)
        .accessibilityIdentifier("team-link-preview")
    }
}

/// The content of a bubble, from top to bottom, as a leading VStack: each part takes its own width, and the stack takes
/// the width of the widest part. A part with a `CardWidth` (the link card) takes its own width up to that value, then
/// the width of the stack. So the card ends at the edge of a wider text, and a short text keeps the card at its own
/// width. The sizes come from the proposal only, so a row has the same height at each layout.
struct BubbleStack: Layout {
    /// The most width of a part that grows to the width of the stack.
    struct CardWidth: LayoutValueKey {
        static let defaultValue: CGFloat? = nil
    }

    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let sizes = sizes(proposal, subviews)
        let height = sizes.reduce(0) { $0 + $1.height } + spacing * CGFloat(max(subviews.count - 1, 0))
        return CGSize(width: sizes.map(\.width).max() ?? 0, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for (subview, size) in zip(subviews, sizes(proposal, subviews)) {
            subview.place(at: CGPoint(x: bounds.minX, y: y), anchor: .topLeading, proposal: ProposedViewSize(size))
            y += size.height + spacing
        }
    }

    /// The size of each part: a part measures at the width of the proposal; a card at its ideal width, at most its
    /// `CardWidth` and the proposal, and then at the width of the widest part.
    private func sizes(_ proposal: ProposedViewSize, _ subviews: Subviews) -> [CGSize] {
        let measured: [CGSize?] = subviews.map { subview in
            subview[CardWidth.self] == nil ? subview.sizeThatFits(ProposedViewSize(width: proposal.width, height: nil)) : nil
        }
        let cardWidths: [CGFloat] = subviews.compactMap { subview in
            subview[CardWidth.self].map { limit in
                min(subview.sizeThatFits(.unspecified).width, limit, proposal.width ?? .infinity)
            }
        }
        let width: CGFloat = (measured.compactMap { $0?.width } + cardWidths).max() ?? 0
        return zip(subviews, measured).map { subview, size in
            size ?? CGSize(width: width, height: subview.sizeThatFits(ProposedViewSize(width: width, height: nil)).height)
        }
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

/// The title of a direct chat: the face and the name of the person or the bot, and the name of the team under it, in
/// the place and the form of the title of a team. It never names the Mac of a bot.
struct DirectTitle: View {
    let peer: TeamDirectPeer
    let team: String
    let faceSize: CGFloat

    var body: some View {
        HStack(spacing: 8) {
            DirectPeerFace(peer: peer, size: faceSize)
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

/// The row of a direct chat in the Chats list, in the layout of a team row: the face and the name of the person or the
/// bot, the age and one line of the newest message, and the unread count. A member or a bot that left keeps the last
/// name of the Mac.
struct DirectContactRow: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let peer: TeamDirectPeer
    let unread: Int
    let lastMessage: TeamLastMessage?
    var avatarSize: CGFloat = BoardStyle.rowFace

    var body: some View {
        let preview = TeamChatPreview.text(lastMessage, direct: true)
        ChatRowLayout(name: peer.name, unread: unread, date: lastMessage?.createdAt?.apiDate) {
            DirectPeerFace(peer: peer, size: avatarSize)
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

/// The face of the other side of a direct chat: the face of the bot, or the face of the person (PersonFace).
struct DirectPeerFace: View {
    let peer: TeamDirectPeer
    let size: CGFloat

    var body: some View {
        if peer.isBot {
            // A bot that left has the neutral face, as above its message in the Team chat.
            BotAvatar(symbol: peer.botEntry?.avatar ?? "", color: peer.botEntry?.color ?? "", size: size,
                      identity: peer.member, animated: false)
        } else {
            PersonFace(member: peer.entry, memberId: peer.member, name: peer.name, size: size)
        }
    }
}

/// The row of a chat of a team in the Chats list and the Mac sidebar: a team, a direct chat, or a direct chat with a
/// bot.
struct PeopleRow: View {
    let item: ChatListItem
    var avatarSize: CGFloat = BoardStyle.rowFace

    var body: some View {
        switch item {
        case .team(let room): TeamContactRow(room: room, avatarSize: avatarSize)
        case .direct(let room, let direct):
            DirectContactRow(peer: room.directPeer(direct.member), unread: direct.unread ?? 0,
                             lastMessage: direct.lastMessage, avatarSize: avatarSize)
        case .botDirect(let room, let direct):
            DirectContactRow(peer: room.botPeer(direct.bot), unread: direct.unread ?? 0,
                             lastMessage: direct.lastMessage, avatarSize: avatarSize)
        case .bot: EmptyView()
        }
    }
}
