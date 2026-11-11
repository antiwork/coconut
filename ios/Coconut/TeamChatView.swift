import SwiftUI

/// The chat of a team: a bot chat with more than one other party (docs/team-log.md, "Team chat"). It reuses the bubble,
/// the quote, the day pill, the times and the composer of the bot chat, and adds only what a group needs: the name of
/// the writer above a run, and Edit and Delete for the person's own messages.
struct TeamChatView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    let roomId: String
    @State private var draft = ""
    @State private var replyTo: TeamMessage?
    @State private var editing: TeamMessage?
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
    @FocusState private var composerFocused: Bool
    @ScaledMetric(relativeTo: .headline) private var nameWidth = 150
    #if os(macOS)
    @Environment(\.openSettings) private var openSettings
    /// The chat runs up under the toolbar of the window, where the title of the team floats.
    private static let titleHeight: CGFloat = 52
    #else
    private static let titleHeight: CGFloat = 0
    #endif

    private var room: TeamRoom? { store.team?.rooms.first { $0.room == roomId } }
    private var chat: TeamChat { store.teamChats[roomId] ?? TeamChat() }
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
                    header(room, chat: chat, empty: allRows.isEmpty)
                    if shown.lowerBound > 0 || chat.before != nil {
                        Button {
                            follows = false
                            let anchor = allRows[shown].first?.id
                            Task { await loadEarlier(anchor: anchor, proxy: proxy) }
                        } label: {
                            if chat.loadingEarlier || growingAbove { ProgressView() }
                            else { Text("Load earlier messages").font(.caption) }
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
                            TeamMessageRow(row: row, room: room, editedText: row.message.flatMap { chat.editing[$0.id] },
                                           canWrite: canWrite(room), actions: actions(proxy: proxy))
                        }
                        .pinnedDayRow(pinnedDay, id: row.id, date: date, startsDay: row.startsDay)
                        // Keep full text height when the list resizes or scrolls to a new message.
                        .fixedSize(horizontal: false, vertical: true)
                        .id(row.id)
                    }
                    // The row inset and this padding keep 14 points below the last row.
                    Color.clear.frame(height: 1).padding(.bottom, 9).id(Self.bottom)
                }
                .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
            }
            .listStyle(.plain)
            .id(listId)
            #if os(macOS)
            .contentMargins(.top, Self.titleHeight, for: .scrollContent)
            #endif
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
                guard let first = TeamReadPosition.toPost(newest: chat.newestFirst, posted: store.teamReadMark(roomId),
                                                           visible: isVisible && scenePhase == .active,
                                                           atBottom: atBottom) else { return }
                Task { await store.markTeamRead(roomId, first: first) }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { footer(room) }
        .task(id: store.connection) {
            // A load without a connection does nothing, so the chat loads again when the Mac connects.
            guard room.hasChat, store.connection == .connected || store.isDemo else { return }
            await store.loadTeamChat(roomId)
        }
        .onAppear { isVisible = true }
        .onDisappear { isVisible = false; composerFocused = false }
        .onReceive(DaySeparator.changes) { _ in dayReference = Date() }
        .confirmationDialog("Delete this message for everyone?", isPresented: Binding(
            get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible,
                            presenting: deleting) { message in
            Button("Delete", role: .destructive) { delete(message) }
                .accessibilityIdentifier("team-delete-confirm")
        }
        #if os(iOS)
        .teamBanners()
        .toolbar {
            if #available(iOS 26.0, *) {
                ToolbarSpacer(.fixed, placement: .leadingBar)
            }
            ToolbarItem(placement: .leadingBar) {
                // The title of the team opens its Team screen, as the title of a bot opens its settings.
                NavigationLink(value: TeamScreenRoute(roomId: room.room)) {
                    HStack(spacing: 8) {
                        TeamFace(size: 28)
                        Text(room.name).font(.headline)
                            .lineLimit(1).truncationMode(.tail)
                            .frame(maxWidth: nameWidth, alignment: .leading)
                    }
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.leading, 4).padding(.trailing, 10)
                    .frame(minHeight: 44).contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(room.name) team")
                .accessibilityIdentifier("team-chat-title")
                .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
            }
        }
        #else
        // The title floats over the top of the chat, as the title of a bot does. It opens the Team screen in the
        // Settings window.
        .overlay(alignment: .top) {
            Button { openSettings() } label: {
                HStack(spacing: 8) {
                    TeamFace(size: 24)
                    Text(room.name).font(.headline).lineLimit(1).truncationMode(.tail)
                }
                .padding(.leading, 6).padding(.trailing, 12)
                .frame(height: 34)
                .modifier(FloatingCapsule())
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .padding(.top, 8)
            .help("Team settings")
            .accessibilityLabel("\(room.name) team")
            .accessibilityIdentifier("team-chat-title")
        }
        #endif
    }

    private static let bottom = "team-chat-bottom"

    /// The line at the start of the chat: what the chat cannot show yet, or where a message goes.
    @ViewBuilder
    private func header(_ room: TeamRoom, chat: TeamChat, empty: Bool) -> some View {
        if !room.hasChat {
            EmptyView()
        } else if !chat.loaded {
            Group {
                if let error = chat.error { Text(error).font(.subheadline).foregroundStyle(.secondary) }
                else { ProgressView() }
            }
            .frame(maxWidth: .infinity).padding(.vertical, 45)
        } else if empty && chat.before == nil {
            Text("Messages here go to each person in \(room.name).")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity).padding(.vertical, 45)
                .accessibilityIdentifier("team-chat-empty")
        }
    }

    /// The composer, or one line that says why the person cannot write.
    @ViewBuilder
    private func footer(_ room: TeamRoom) -> some View {
        if let reason = room.chatClosedReason {
            Text(reason)
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 24).padding(.vertical, 14)
                .modifier(ComposerBackdrop())
                .accessibilityIdentifier("team-chat-closed")
        } else {
            TeamChatComposer(room: room, text: $draft, replyTo: $replyTo, editing: $editing, focused: $composerFocused,
                             enabled: canWrite(room), send: send)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func canWrite(_ room: TeamRoom) -> Bool {
        (store.hasPairing || store.isDemo) && room.chatClosedReason == nil
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
                draft = message.text
                composerFocused = true
            },
            delete: { deleting = $0 },
            showMessage: { id in show(id, proxy: proxy) },
            retry: { store.retryTeamMessage(roomId, id: $0) },
            removeUnsent: { store.removeUnsentTeamMessage(roomId, id: $0) })
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let room, canWrite(room), !text.isEmpty else { return }
        if let message = editing {
            draft = ""
            editing = nil
            Task {
                // A failure gives the edit back to the composer, unless the person typed something new.
                if await !store.editTeamMessage(roomId, message, text: text), draft.isEmpty, editing == nil {
                    editing = message
                    draft = text
                }
            }
        } else {
            store.sendTeamMessage(roomId, text: text, replyingTo: replyTo)
            draft = ""
            replyTo = nil
        }
    }

    private func delete(_ message: TeamMessage) {
        if editing?.id == message.id { editing = nil; draft = "" }
        if replyTo?.id == message.id { replyTo = nil }
        Task { await store.deleteTeamMessage(roomId, message) }
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
            await store.loadEarlierTeamMessages(roomId)
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

/// One message of the Team chat: the name of the writer above the first message of a run, then the bubble.
struct TeamMessageRow: View {
    let row: TeamChatRow
    let room: TeamRoom
    /// The new text of an edit on its way.
    let editedText: String?
    let canWrite: Bool
    let actions: TeamMessageActions
    @State private var held = false

    private var own: Bool { row.writer == .you }
    private var message: TeamMessage? { row.message }
    private var pending: TeamPendingMessage? { if case .pending(let pending) = row.content { pending } else { nil } }
    private var text: String { (editedText ?? row.text).trimmingCharacters(in: .newlines) }
    private var teamBot: TeamRoomBot? { message?.bot.flatMap { id in room.botList.first { $0.id == id } } }
    private var writerName: String {
        if let message, message.bot != nil { return teamBot?.name ?? message.botName ?? "Bot" }
        return message.map(TeamNames.author(of:)) ?? "You"
    }
    /// Only a saved message of this person can change. A message of a bot of this Mac belongs to the bot.
    private var canChange: Bool { canWrite && message?.isOwn == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if row.startsRun && !own { writerLabel }
            bubbleRow.modifier(MessageTimestamp(createdAt: row.createdAt))
            // Under the bubble, on its side, in the style of the name above it. The bubble keeps the width of its text.
            if message?.isEdited == true || editedText != nil {
                Text("Edited").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: own ? .trailing : .leading)
                    .padding(.horizontal, 10)
                    .accessibilityHidden(true)
            }
        }
    }

    @ViewBuilder private var writerLabel: some View {
        Group {
            if message?.bot != nil {
                HStack(spacing: 5) {
                    // The face and the color of the team bot, or a neutral face for a bot that the team no longer has.
                    BotAvatar(symbol: teamBot?.avatar ?? "", color: teamBot?.color ?? "", size: 18,
                              identity: teamBot?.id ?? "", animated: false)
                    Text(writerName)
                }
            } else {
                Text(writerName)
            }
        }
        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
        .padding(.horizontal, 10)
        .accessibilityHidden(true)
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
            MessageText(text: text, linkColor: own ? BoardStyle.userBubbleLink : .blue, allowsTextSelection: false)
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
            HeldBubbleView(trailing: own, actions: menuActions, share: .text(row.text)) { _ in
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
                Button("Delete", systemImage: "trash", role: .destructive) { actions.delete(message) }
            }
        }
        #endif
    }

    #if os(iOS)
    private var menuActions: [HeldAction] {
        var items: [HeldAction] = []
        if let message, canWrite {
            items.append(HeldAction(title: "Reply", systemImage: "arrowshape.turn.up.left") { actions.reply(message) })
        }
        items.append(HeldAction(title: "Copy", systemImage: "doc.on.doc") { Pasteboard.copy(row.text) })
        if let message, canChange {
            items.append(HeldAction(title: "Edit", systemImage: "pencil") { actions.edit(message) })
            items.append(HeldAction(title: "Delete", systemImage: "trash", destructive: true) { actions.delete(message) })
        }
        return items
    }
    #endif

    /// VoiceOver gets the same actions as the menu.
    @ViewBuilder private var accessibilityMenu: some View {
        if let message, canWrite { Button("Reply") { actions.reply(message) } }
        Button("Copy") { Pasteboard.copy(row.text) }
        if let message, canChange {
            Button("Edit") { actions.edit(message) }
            Button("Delete") { actions.delete(message) }
        }
    }

    /// VoiceOver reads the writer, the time and the text.
    private var spokenLabel: String {
        let time = row.createdAt.apiDate?.formatted(date: .abbreviated, time: .shortened)
        var parts = [writerName]
        if let time { parts.append(time) }
        parts.append(text)
        if message?.isEdited == true || editedText != nil { parts.append("Edited") }
        if pending?.failed == true { parts.append("Not sent") }
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
                                author: TeamNames.author(of: reply, ownMember: room.ownMember?.member), role: "")
    }
}

/// The composer of the Team chat: text only. A reply or an edit shows its message above the field, with a way to cancel.
struct TeamChatComposer: View {
    let room: TeamRoom
    @Binding var text: String
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
                                                             author: TeamNames.author(of: replyTo), role: ""),
                                 cancelLabel: "Cancel reply") { self.replyTo = nil }
                    .accessibilityIdentifier("reply-preview")
            }
            HStack(alignment: .bottom, spacing: 4) {
                TextField("Message \(room.name)", text: $text, axis: .vertical)
                    .focused($focused)
                    .modifier(ComposerFieldStyle(send: { if canSend { send() } }))
                    .accessibilityIdentifier("message-input")
                if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ComposerSendButton(symbol: editing == nil ? "arrow.up" : "checkmark", enabled: canSend,
                                       label: editing == nil ? "Send message" : "Save edit", action: send)
                }
            }
            .padding(.leading, 14).padding(.trailing, 4)
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: 23, style: .continuous)))
            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, 16)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .modifier(ComposerBackdrop())
    }
}

/// The face of a team: a plain circle with two people, in the colors of the theme. It has the size of a bot face.
struct TeamFace: View {
    let size: CGFloat

    var body: some View {
        Circle().fill(BoardStyle.surface)
            .overlay {
                Image(systemName: "person.2.fill")
                    .font(.system(size: size * 0.36, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .frame(width: size * BotAvatar.scale, height: size * BotAvatar.scale)
            .accessibilityHidden(true)
    }
}

/// The row of a team in the Chats list, in the layout of a bot row: the face, the name with the unread count, the age
/// of the newest message, and one line of it. A Mac with an older service sends no chat, so the row shows the name only.
struct TeamContactRow: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let room: TeamRoom
    var avatarSize: CGFloat = 46

    var body: some View {
        let unread = room.unread ?? 0
        let preview = room.hasChat ? TeamChatPreview.text(room.lastMessage, ownName: room.ownMember?.name) : nil
        let date = room.lastMessage?.createdAt?.apiDate
        HStack(spacing: 14) {
            TeamFace(size: avatarSize)
            VStack(alignment: .leading, spacing: 5) {
                if dynamicTypeSize.isAccessibilitySize {
                    name(unread)
                    if let date { CompactAgeLabel(date: date).font(.caption).foregroundStyle(.secondary) }
                } else {
                    HStack(alignment: .firstTextBaseline) {
                        name(unread)
                        Spacer(minLength: 8)
                        if let date { CompactAgeLabel(date: date).font(.caption).foregroundStyle(.secondary).fixedSize() }
                    }
                }
                if let preview {
                    Text(preview)
                        .font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(dynamicTypeSize.isAccessibilitySize ? 2 : 1)
                }
            }
        }
        .frame(minHeight: 52)
        .accessibilityElement(children: .combine)
        .accessibilityLabel([room.name, preview, unread > 0 ? "\(unread) unread" : nil].compactMap { $0 }
            .joined(separator: ", "))
    }

    private func name(_ unread: Int) -> some View {
        HStack(spacing: 5) {
            Text(room.name).font(.body.weight(unread > 0 ? .semibold : .medium)).lineLimit(1)
            if unread > 0 { UnreadMarker(count: unread) }
        }
    }
}
