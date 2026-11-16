import SwiftUI

/// The Notices chat: the operational notices of the tooling of the Mac, as the messages of one chat. The oldest notice
/// is at the top and the newest at the bottom, with the day pill of a chat between two days and the name of the
/// producer above its run of notices. The chat has no composer: only the tooling writes here. The person reads a
/// notice when the chat is open, as in each other chat.
struct NoticesView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.scenePhase) private var scenePhase
    /// The moment that "Today" counts from. It moves on when the day or the time zone changes.
    @State private var now = Date()
    @State private var pinnedDay = PinnedDay()
    private static let bottom = "notices-bottom"

    var body: some View {
        let rows = NoticeRow.rows(of: store.alertFeed.items)
        Group {
            if rows.isEmpty {
                Text("No notices yet.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityIdentifier("notices-empty")
            } else {
                transcript(rows)
            }
        }
        .background(BoardStyle.paper)
        .navigationTitle("")
        .inlineNavigationTitle()
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: BoardStyle.Space.s) {
                    NoticesFace(size: 28)
                    Text("Notices").font(.body.weight(.semibold)).lineLimit(1)
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("notices-title")
            }
        }
        .onReceive(DaySeparator.changes) { now = Date() }
        // A notice that arrives while the chat is open is read too. A chat behind a locked phone reads nothing.
        .task(id: [store.unreadAlertCount, scenePhase == .active ? 1 : 0]) {
            if scenePhase == .active { await store.markAllAlertsRead() }
        }
    }

    private func transcript(_ rows: [NoticeRow]) -> some View {
        ScrollViewReader { proxy in
            List {
                Group {
                    ForEach(rows) { row in
                        let date = row.alert.createdAt.apiDate
                        VStack(spacing: 0) {
                            if row.startsDay, let date {
                                DaySeparatorRow(date: date, now: now, row: row.id, pinned: pinnedDay)
                            }
                            NoticeMessageRow(row: row, now: now)
                        }
                        .pinnedDayRow(pinnedDay, id: row.id, date: date, startsDay: row.startsDay)
                        .fixedSize(horizontal: false, vertical: true)
                    }
                    Color.clear.frame(height: 1).padding(.bottom, BoardStyle.Space.s).id(Self.bottom)
                }
                .listRowInsets(EdgeInsets(top: BoardStyle.Space.xs, leading: BoardStyle.Space.l,
                                          bottom: BoardStyle.Space.xs, trailing: BoardStyle.Space.l))
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
            }
            .listStyle(.plain)
            .buttonStyle(.borderless)
            .scrollContentBackground(.hidden)
            .environment(\.defaultMinListRowHeight, 1)
            .accessibilityIdentifier("notices-transcript")
            .modifier(ConversationTimestamps())
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.bottom, for: .alignment)
            .pinnedDayPill(pinnedDay, now: now)
            #if !os(macOS)
            .onScrollPhaseChange { _, phase in pinnedDay.scrolls(phase != .idle) }
            #endif
            // A new notice shows at the bottom, as a new message of a chat.
            .onChange(of: rows.last?.id) { _, _ in proxy.scrollTo(Self.bottom, anchor: .bottom) }
        }
    }
}

/// The face of the Notices chat: a bell on the neutral surface, in the size of the face of a team.
struct NoticesFace: View {
    let size: CGFloat

    var body: some View {
        LetterFaceView(letters: nil, id: "notices", symbol: "bell.fill", size: size)
    }
}

/// The row of the Notices chat in the Chats list: the title of the newest notice under the name, its age, and the
/// count of the unread notices, in the layout of each other chat.
struct NoticesContactRow: View {
    @Environment(AppStore.self) private var store
    var avatarSize: CGFloat = BoardStyle.rowFace

    var body: some View {
        let newest = store.alertFeed.newest
        let preview = ChatRowText.line(newest?.title ?? "")
        let unread = store.unreadAlertCount
        ChatRowLayout(name: "Notices", unread: unread, date: newest?.createdAt.apiDate) {
            NoticesFace(size: avatarSize)
        } subtitle: {
            Text(preview).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel((["Notices", preview] + (unread > 0 ? [UnreadBadge.spoken(unread)] : [])).joined(separator: ", "))
    }
}

/// One notice of the chat with what the chat shows around it.
struct NoticeRow: Identifiable, Equatable {
    let alert: FeedAlert
    /// The first notice of a calendar day: the day pill stands above it.
    let startsDay: Bool
    /// The first notice of a run of one producer: the name of the producer stands above it.
    let startsRun: Bool

    var id: String { alert.id }

    /// The notices in the order of a chat: oldest first. The Mac sends the open ones before the resolved ones, each
    /// part newest first, so two notices of one moment keep the reverse of that order.
    static func rows(of alerts: [FeedAlert], calendar: Calendar = .current) -> [NoticeRow] {
        let ordered = alerts.reversed().enumerated().sorted { first, second in
            let (left, right) = (first.element.createdAt.apiDate ?? .distantPast,
                                 second.element.createdAt.apiDate ?? .distantPast)
            return left != right ? left < right : first.offset < second.offset
        }.map(\.element)
        return ordered.enumerated().map { index, alert in
            let before = index > 0 ? ordered[index - 1] : nil
            let startsDay = before.map { !DaySeparator.sameDay($0.createdAt, alert.createdAt, calendar: calendar) } ?? true
            return NoticeRow(alert: alert, startsDay: startsDay, startsRun: startsDay || before?.source != alert.source)
        }
    }
}

/// One notice as a message of the chat: the name of its producer above a run, then the bubble of a message of the
/// other side with the title, the body and the link. A critical notice says "Critical" in the destructive tint, so its
/// level does not depend on a color alone. A resolved notice says "Resolved" under its bubble, where a chat says
/// "Edited". A hold on the bubble offers Resolve and Copy.
struct NoticeMessageRow: View {
    @Environment(AppStore.self) private var store
    let row: NoticeRow
    let now: Date
    @State private var held = false

    private var alert: FeedAlert { row.alert }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            if row.startsRun {
                Text(alert.source)
                    .font(.footnote).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    // The name starts at the edge of the text in the bubble.
                    .padding(.horizontal, BoardStyle.Space.m)
                    .padding(.top, row.startsDay ? 0 : BoardStyle.Space.s)
                    .accessibilityHidden(true)
            }
            HStack {
                bubble
                Spacer(minLength: 18)
            }
            .modifier(MessageTimestamp(createdAt: alert.createdAt))
            if alert.isResolved {
                Text("Resolved")
                    .font(.footnote).foregroundStyle(.secondary)
                    .padding(.horizontal, BoardStyle.Space.m)
                    .accessibilityHidden(true)
            }
        }
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                if alert.severity == .critical {
                    Text(alert.severity.label)
                        .font(.footnote.weight(.semibold)).foregroundStyle(BoardStyle.destructive)
                }
                Text(alert.title).font(.body.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                if !alert.body.isEmpty {
                    MessageText(text: alert.body, allowsTextSelection: false)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(NoticeSpeech.row(alert, now: now))
            .accessibilityValue(alert.isResolved ? "Resolved" : "")
            .accessibilityActions { menu }
            .accessibilityIdentifier("notice-\(alert.id)")
            if let link = alert.link {
                Link(destination: link) {
                    Text("Open \(link.host() ?? "link")").lineLimit(1).truncationMode(.middle)
                }
                .tint(.blue)
                .accessibilityHint("Opens in the browser.")
                .accessibilityIdentifier("notice-link-\(alert.id)")
            }
        }
        .opacity(alert.isResolved ? 0.6 : 1)
        .modifier(ChatBubbleBackground(own: false))
        #if os(iOS)
        .simultaneousGesture(LongPressGesture(minimumDuration: 0.45).onEnded { _ in
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            // The held notice fades in over the chat, as a held message does.
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { held = true }
        })
        .fullScreenCover(isPresented: $held) {
            HeldBubbleView(actions: heldActions, share: .text(NoticeSpeech.copy(alert))) { _ in
                EmptyView()
            } preview: { maxHeight in
                HeldTextPreview(text: NoticeSpeech.copy(alert), own: false, maxHeight: maxHeight)
            }
        }
        #else
        .contextMenu { menu }
        #endif
    }

    #if os(iOS)
    private var heldActions: [HeldAction] {
        var items: [HeldAction] = []
        if !alert.isResolved {
            items.append(HeldAction(title: "Resolve", systemImage: "checkmark.circle") {
                Task { await store.resolveAlert(alert) }
            })
        }
        items.append(HeldAction(title: "Copy", systemImage: "doc.on.doc") { Pasteboard.copy(NoticeSpeech.copy(alert)) })
        return items
    }
    #endif

    @ViewBuilder private var menu: some View {
        if !alert.isResolved {
            Button("Resolve", systemImage: "checkmark.circle") { Task { await store.resolveAlert(alert) } }
        }
        Button("Copy", systemImage: "doc.on.doc") { Pasteboard.copy(NoticeSpeech.copy(alert)) }
    }
}

/// The words of a notice outside its bubble.
enum NoticeSpeech {
    /// What VoiceOver says: "critical, <title>, <body>, <source>, <relative time>".
    static func row(_ alert: FeedAlert, now: Date = .now) -> String {
        var parts = [alert.severity.rawValue, alert.title]
        if !alert.body.isEmpty { parts.append(alert.body) }
        parts.append(alert.source)
        if let date = alert.createdAt.apiDate { parts.append(relative(date, now: now)) }
        return parts.joined(separator: ", ")
    }

    /// The age of a notice in words, for example "5 minutes ago".
    static func relative(_ date: Date, now: Date = .now) -> String {
        // The clock of the Mac can be a little ahead of the phone. A notice never reads as "in 2 seconds".
        RelativeDateTimeFormatter().localizedString(for: min(date, now), relativeTo: now)
    }

    /// What Copy and Share take: the title, then the body.
    static func copy(_ alert: FeedAlert) -> String {
        alert.body.isEmpty ? alert.title : alert.title + "\n\n" + alert.body
    }
}
