import SwiftUI

// An inbox chat of a team (docs/team-log.md, "Inbox chats"): a named chat that agents deliver their alerts and reports to,
// such as Notices. It is a list of notices, as an inbox of mail, not one long chat. Each notice is a thread of the chat:
// its first message with the answers to it. A tap opens the notice as a chat of its own, which reads it, and a read
// notice leaves the top of the list for Read. An answer in a notice brings it back to the top, unread, as a thread of
// mail. The person asked for this: "Notices shouldn't be like a chat group but each one its own chat and then you can tap
// in and it'll mark as read and go away. Similar more to email than slack channel."

/// One notice of an inbox chat: a thread, with its first and its newest message, and its counts.
struct TeamThread: Decodable, Identifiable, Equatable, Sendable {
    var id: String
    /// The messages of the thread, the first one too.
    var count: Int
    /// The messages of the thread that the person did not read.
    var unread: Int
    var root: TeamMessage
    var latest: TeamMessage

    /// The answers to the notice.
    var replies: Int { max(0, count - 1) }
}

/// One answer of the Mac with notices of an inbox chat: on the first page each unread notice, then the newest read ones.
/// `before` asks for the next page of read notices, nil after the oldest. `total` counts each notice of the chat.
struct TeamThreadPage: Decodable, Sendable {
    var threads: [TeamThread]
    var before: Int?
    var total: Int?

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        threads = (try container.decodeIfPresent([LossyThread].self, forKey: .threads) ?? []).compactMap(\.value)
        before = try? container.decodeIfPresent(Int.self, forKey: .before)
        total = try? container.decodeIfPresent(Int.self, forKey: .total)
    }

    private enum CodingKeys: String, CodingKey { case threads, before, total }

    /// A notice that the app cannot read drops only that notice.
    private struct LossyThread: Decodable, Sendable {
        let value: TeamThread?
        init(from decoder: Decoder) throws { value = try? TeamThread(from: decoder) }
    }
}

/// The notices of one inbox chat in the app.
struct TeamThreadList: Equatable, Sendable {
    var threads: [TeamThread] = []
    /// The `before` of the next page of read notices, or nil after the oldest.
    var before: Int?
    var loaded = false
    var loadingMore = false
    /// The text of the last failed load. The list shows it only while it has no page.
    var error: String?
    /// The notices of the chat on the Mac, read and unread.
    var total: Int?

    /// The notices that wait for the person, the newest answer first.
    var unread: [TeamThread] { threads.filter { $0.unread > 0 }.sorted { $0.latest.first > $1.latest.first } }
    /// The notices that the person read, the newest answer first.
    var read: [TeamThread] { threads.filter { $0.unread == 0 }.sorted { $0.latest.first > $1.latest.first } }
    /// How many notices Read holds: from the count of the Mac, else the ones in the app.
    var readCount: Int { max((total ?? 0) - unread.count, read.count) }
    /// The newest message of the chat that the list has. Read all reads through it.
    var newestFirst: Int? { threads.map(\.latest.first).max() }

    /// Takes the first page of the Mac. The read notices that the person loaded below that page stay, so a new message
    /// does not fold the list back while the person reads further down.
    mutating func take(_ page: TeamThreadPage) {
        let fresh = Set(page.threads.map(\.id))
        let floor = page.threads.filter { $0.unread == 0 }.map(\.latest.first).min()
        let kept = threads.filter { thread in
            !fresh.contains(thread.id) && thread.unread == 0 && floor.map { thread.latest.first < $0 } == true
        }
        threads = page.threads + kept
        if kept.isEmpty { before = page.before }
        total = page.total
        loaded = true
        error = nil
    }

    /// Adds an older page of read notices.
    mutating func append(_ page: TeamThreadPage) {
        let known = Set(threads.map(\.id))
        threads += page.threads.filter { !known.contains($0.id) }
        before = page.before
        loadingMore = false
    }

    /// The person read a notice through the message with this `first`: it goes to Read at once.
    mutating func markRead(_ id: String, through first: Int) {
        guard let index = threads.firstIndex(where: { $0.id == id }), threads[index].latest.first <= first,
              threads[index].unread > 0 else { return }
        threads[index].unread = 0
    }

    /// The person read the whole chat: each notice goes to Read.
    mutating func markAllRead() {
        for index in threads.indices { threads[index].unread = 0 }
    }
}

/// The words of a notice for its row: a title and the rest. An agent writes the headline of a notice as a bold first line
/// ("**A customer has waited 2 hours for a reply**"), so that line is the title; else the first line is. The marks of
/// Markdown go, so the row shows plain words.
enum TeamNoticeText {
    static func split(_ text: String) -> (title: String, rest: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        var title: Substring
        var rest: Substring
        if trimmed.hasPrefix("**"), let end = trimmed.dropFirst(2).range(of: "**") {
            title = trimmed[trimmed.index(trimmed.startIndex, offsetBy: 2)..<end.lowerBound]
            rest = trimmed[end.upperBound...]
        } else if let line = trimmed.firstIndex(of: "\n") {
            title = trimmed[..<line]
            rest = trimmed[trimmed.index(after: line)...]
        } else {
            title = trimmed[...]
            rest = ""
        }
        let plainTitle = plain(String(title))
        let plainRest = plain(String(rest))
        return plainTitle.isEmpty ? (plainRest, "") : (plainTitle, plainRest)
    }

    /// The text without the marks of Markdown, on one line: a link keeps its label, and a heading, a quote or an item of a
    /// list loses its mark.
    static func plain(_ text: String) -> String {
        var value = text.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
        value = value.replacingOccurrences(of: #"(?m)^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+)"#, with: "",
                                           options: .regularExpression)
        for mark in ["**", "__", "~~", "`"] { value = value.replacingOccurrences(of: mark, with: "") }
        return value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}

/// The list of the notices of an inbox chat: the unread ones at the top, then Read, folded, with its count on the right
/// as Archived has in Chats. A row opens its notice as a chat (TeamChatView with the thread in its key).
struct TeamInboxView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    #if os(macOS)
    @Environment(\.showTeamScreen) private var showTeamScreen
    #endif
    let chatKey: TeamChatKey
    /// Read is folded until the person opens it. The choice stays on this device.
    @AppStorage("notice-read-open") private var readOpen = false
    #if os(macOS)
    /// The title floats over the top of the list, as in a chat of a team (TeamChatView).
    private static let titlePillHeight: CGFloat = 44
    private static let titleHeight: CGFloat = 8 + titlePillHeight + 10
    #endif

    private var room: TeamRoom? { store.team?.rooms.first { $0.room == chatKey.room }?.inChat(chatKey.chat) }
    private var list: TeamThreadList { store.teamThreads[chatKey] ?? TeamThreadList() }

    var body: some View {
        Group {
            if let room {
                inbox(room)
            } else {
                QuietEmptyState(symbol: "tray", title: "Notices unavailable", message: "This Mac is no longer in this team.")
            }
        }
        .inlineNavigationTitle()
        .task(id: store.connection) {
            guard store.connection == .connected || store.isDemo else { return }
            await store.loadTeamThreads(chatKey)
        }
    }

    @ViewBuilder
    private func inbox(_ room: TeamRoom) -> some View {
        let list = list
        let unread = list.unread
        List {
            #if os(macOS)
            ChatTitleSpace(titleHeight: Self.titleHeight).homeRow()
            #endif
            if !list.loaded {
                Group {
                    if let error = list.error { Text(error).font(.subheadline).foregroundStyle(.secondary) }
                    else { CoconutLoader(.page) }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 45)
                .homeRow()
            } else {
                if unread.isEmpty {
                    Text("No new notices.")
                        .font(.subheadline).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, BoardStyle.Space.l)
                        .homeRow()
                        .accessibilityIdentifier("notice-inbox-empty")
                }
                ForEach(unread) { row($0, room: room) }
                if list.readCount > 0 {
                    NoticeFoldHeader(title: "Read", count: list.readCount, open: readOpen) { readOpen.toggle() }
                        .homeRow()
                    if readOpen {
                        ForEach(list.read) { row($0, room: room) }
                        if list.before != nil {
                            Button {
                                Task { await store.loadOlderTeamThreads(chatKey) }
                            } label: {
                                if list.loadingMore {
                                    CoconutLoader(.row, label: "Loading earlier notices")
                                } else {
                                    Text("Load earlier notices").font(.footnote)
                                }
                            }
                            .buttonStyle(.borderless)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .disabled(list.loadingMore)
                            .homeRow()
                            .accessibilityIdentifier("notice-inbox-more")
                        }
                    }
                }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.defaultMinListRowHeight, 1)
        .background(BoardStyle.paper)
        .accessibilityIdentifier("notice-inbox")
        #if os(iOS)
        .toolbar {
            if #available(iOS 26.0, *) {
                ToolbarSpacer(.fixed, placement: .leadingBar)
            }
            ToolbarItem(placement: .leadingBar) {
                // The title of the chat opens the Team screen of its team, as in the chat.
                NavigationLink(value: TeamScreenRoute(roomId: room.room)) {
                    TeamTitle(room: room, faceSize: 28)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.leading, 4).padding(.trailing, BoardStyle.Space.m)
                        .frame(minHeight: 44).contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(TeamTitle.spoken(room))
                .accessibilityIdentifier("team-chat-title")
            }
            if !unread.isEmpty {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Read all", systemImage: "checkmark.circle") { readAll() }
                        .accessibilityIdentifier("notice-inbox-read-all")
                }
            }
        }
        #else
        .overlay(alignment: .top) { WindowDragStrip() }
        .overlay(alignment: .top) {
            Button { showTeamScreen?(room.room) } label: {
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
        .overlay(alignment: .topTrailing) {
            if !unread.isEmpty {
                Button { readAll() } label: {
                    Label("Read all", systemImage: "checkmark.circle")
                        .padding(.horizontal, 14)
                        .frame(height: Self.titlePillHeight)
                        .modifier(FloatingCapsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .padding(.top, 8).padding(.trailing, 12)
                .help("Mark each notice as read")
                .accessibilityIdentifier("notice-inbox-read-all")
            }
        }
        #endif
    }

    private func row(_ thread: TeamThread, room: TeamRoom) -> some View {
        Button {
            pushRoute?(TeamChatRoute(chat: chatKey.inThread(thread.id)))
        } label: {
            TeamThreadRow(thread: thread)
                .background(BoardStyle.paper)
                .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .contextMenu {
            if thread.unread > 0 {
                Button("Mark as Read", systemImage: "checkmark.circle") { read(thread) }
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            if thread.unread > 0 {
                Button { read(thread) } label: { Label("Read", systemImage: "checkmark.circle") }
                    .tint(BoardStyle.control)
            }
        }
        .homeRow()
        .accessibilityIdentifier("notice-\(thread.id)")
    }

    /// Reads one notice without opening it, as a swipe in Mail does.
    private func read(_ thread: TeamThread) {
        Task { await store.markTeamRead(chatKey.inThread(thread.id), first: thread.latest.first) }
    }

    /// Reads each notice of the chat: the read position of the chat moves through its newest message.
    private func readAll() {
        guard let newest = list.newestFirst else { return }
        Task { await store.markTeamRead(chatKey, first: newest) }
    }
}

/// One notice in the list: its title, its age, and under it the kind of the alert and the rest of the text, or the
/// newest answer. An unread notice has a bold title and the green count of its unread messages, as a chat in Chats.
struct TeamThreadRow: View {
    let thread: TeamThread

    var body: some View {
        let words = TeamNoticeText.split(thread.root.text)
        let detail = detailText(rest: words.rest)
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) {
                Text(words.title.isEmpty ? "Notice" : words.title)
                    .font(.body.weight(thread.unread > 0 ? .semibold : .regular))
                    .foregroundStyle(.primary)
                    .lineLimit(detail == nil ? 3 : 2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let date = thread.latest.createdAt.apiDate {
                    CompactAgeLabel(date: date)
                        .font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                        .fixedSize()
                }
            }
            if let detail {
                HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) {
                    detail
                        .font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if thread.unread > 0 { UnreadBadge(count: thread.unread) }
                }
            } else if thread.unread > 0 {
                HStack { Spacer(minLength: 0); UnreadBadge(count: thread.unread) }
            }
        }
        .padding(.vertical, BoardStyle.Space.m)
        .accessibilityElement(children: .combine)
        .accessibilityValue(thread.unread > 0 ? "Unread" : "")
    }

    /// The line under the title: a critical alert says so first, then the kind of the alert, then the newest answer
    /// with its writer when the notice has answers, else the rest of the text. Nil when there is nothing to say.
    private func detailText(rest: String) -> Text? {
        var parts: [Text] = []
        if let alert = thread.root.alert {
            if alert.level == .critical {
                parts.append(Text("Critical").fontWeight(.bold).foregroundStyle(BoardStyle.destructive))
            }
            parts.append(Text(alert.type).fontWeight(.semibold))
        }
        if thread.root.bot == nil {
            parts.append(Text(thread.root.isOwn ? "You" : thread.root.memberName ?? TeamNames.formerMember)
                .fontWeight(.semibold))
        }
        if thread.replies > 0 {
            parts.append(Text(thread.replies == 1 ? "1 reply" : "\(thread.replies) replies"))
            let writer = thread.latest.bot != nil ? thread.latest.botName ?? "Agent"
                : thread.latest.isOwn ? "You" : thread.latest.memberName ?? TeamNames.formerMember
            let text = TeamNoticeText.plain(thread.latest.text)
            if !text.isEmpty { parts.append(Text(writer + ": " + text)) }
        } else if !rest.isEmpty {
            parts.append(Text(rest))
        }
        guard let first = parts.first else { return nil }
        return parts.dropFirst().reduce(first) { $0 + Text(" · ") + $1 }
    }
}

/// The title of Read in the list of notices, as a folding section of Slack: a chevron that turns down while it is open,
/// the title, and the count on the right.
struct NoticeFoldHeader: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var chevron: CGFloat = 14
    let title: String
    let count: Int
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: BoardStyle.Space.s) {
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(open ? 90 : 0))
                    .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: open)
                    .frame(width: chevron)
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Text(count, format: .number)
                    .font(.subheadline).monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .padding(.top, BoardStyle.Space.l)
            .padding(.bottom, BoardStyle.Space.s)
            .background(BoardStyle.paper)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .accessibilityValue(open ? "Expanded" : "Collapsed")
        .accessibilityIdentifier("notice-inbox-read")
    }
}

// The demo has this on the simulator (applyTeamPreview) and in a local render of a Mac view, never in a release build.
#if DEBUG
extension AppStore {
    /// The named chat Notices of the team preview, as the Mac sends it: an inbox chat with three unread notices.
    static let previewNoticesChat = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    /// The notice with answers of the preview: a payout to a seller that the bank rejected.
    static let previewNoticeThread = "c0ffee00c0ffee00c0ffee00c0ffee03"

    /// Gives the team preview its Notices: the row in Chats, the list of notices, and the messages of the notice with
    /// answers. The data goes through the decoders of the answers of the Mac, as a live list does.
    func applyInboxPreview(room: String, now: Date = Date()) {
        let formatter = ISO8601DateFormatter()
        func ago(_ minutes: Double) -> String { formatter.string(from: now.addingTimeInterval(-minutes * 60)) }
        let claw: [String: Any] = ["member": "m4", "member_name": "Gumclaw", "bot": "t-\(room)-gumclaw", "bot_name": "Gumclaw"]
        func message(_ id: String, _ first: Int, _ text: String, minutes: Double, alert: (String, String)? = nil,
                     thread: String, replyTo: String? = nil, own: Bool = false) -> [String: Any] {
            var fields: [String: Any] = own ? ["member": "m1", "member_name": "Ada", "this_mac": true] : claw
            fields.merge(["id": id, "first": first, "text": text, "created_at": ago(minutes), "thread": thread,
                          "chat": Self.previewNoticesChat]) { _, new in new }
            if let alert { fields["alert"] = ["type": alert.0, "level": alert.1] }
            if let replyTo { fields["reply_to"] = replyTo }
            return fields
        }
        let n1 = "c0ffee00c0ffee00c0ffee00c0ffee01", n2 = "c0ffee00c0ffee00c0ffee00c0ffee02"
        let n3 = Self.previewNoticeThread, n4 = "c0ffee00c0ffee00c0ffee00c0ffee04"
        let n5 = "c0ffee00c0ffee00c0ffee00c0ffee05", n6 = "c0ffee00c0ffee00c0ffee00c0ffee06"
        let n3a = "c0ffee00c0ffee00c0ffee00c0ffe3a1", n3b = "c0ffee00c0ffee00c0ffee00c0ffe3b2"
        let root1 = message(n1, 120, "**Three chargebacks in an hour on one seller**\n\nAll three are on the $49 Figma course "
                            + "from new buyers. I held the seller's next payout until a person looks.", minutes: 4,
                            alert: ("risk", "critical"), thread: n1)
        let root2 = message(n2, 112, "**A customer has waited 2 hours for a reply**\n\nThe oldest has waited 2 hours, past "
                            + "our 15-minute reply target. One more arrived since the last check.", minutes: 18,
                            alert: ("support", "notice"), thread: n2)
        let root3 = message(n3, 100, "**The payout to a seller in Brazil bounced**\n\nThe bank rejected the transfer of "
                            + "$1,284.10, so the balance stays on hold.", minutes: 95, alert: ("payouts", "notice"), thread: n3)
        let reply3 = message(n3a, 105, "Why did it bounce?", minutes: 60, thread: n3, replyTo: n3, own: true)
        let answer3 = message(n3b, 116, "The bank says the account number does not match the CPF on file. I emailed the "
                              + "seller to update it in Settings > Payments; the payout goes again on Friday.",
                              minutes: 9, thread: n3, replyTo: n3a)
        let root4 = message(n4, 90, "Sentry alert: production error spike on checkout (NoMethodError in "
                            + "PurchasesController#create).", minutes: 130, alert: ("infra", "notice"), thread: n4)
        let root5 = message(n5, 80, "**Daily support report**\n\n41 answered, 3 waiting. CSAT 62, level with the week.",
                            minutes: 5 * 60, thread: n5)
        let root6 = message(n6, 60, "**Friday's payout batch went out**\n\n1,284 sellers, $412,903. Two failed and are "
                            + "on hold for new bank details.", minutes: 26 * 60, alert: ("finance", "info"), thread: n6)
        let threads: [[String: Any]] = [
            ["id": n1, "count": 1, "unread": 1, "root": root1, "latest": root1],
            ["id": n2, "count": 1, "unread": 1, "root": root2, "latest": root2],
            ["id": n3, "count": 3, "unread": 1, "root": root3, "latest": answer3],
            ["id": n4, "count": 1, "unread": 0, "root": root4, "latest": root4],
            ["id": n5, "count": 1, "unread": 0, "root": root5, "latest": root5],
            ["id": n6, "count": 1, "unread": 0, "root": root6, "latest": root6],
        ]
        let decoder = APIJSON.decoder()
        func decode<Value: Decodable>(_ object: Any, as type: Value.Type) -> Value? {
            guard let data = try? JSONSerialization.data(withJSONObject: object) else { return nil }
            return try? decoder.decode(type, from: data)
        }
        guard let page = decode(["threads": threads, "before": NSNull(), "total": 6], as: TeamThreadPage.self),
              let messages = decode(["messages": [answer3, reply3, root3], "before": NSNull()], as: TeamMessagePage.self),
              let index = team?.rooms.firstIndex(where: { $0.room == room }) else { return }
        let chat = TeamNamedChat(id: Self.previewNoticesChat, name: "Notices", createdAt: ago(20 * 1440),
                                 mine: false, unread: 3,
                                 lastMessage: TeamLastMessage(bot: "t-\(room)-gumclaw", botName: "Gumclaw",
                                                              text: "Three chargebacks in an hour on one seller",
                                                              createdAt: ago(4)),
                                 notifications: true, pinned: false, inbox: true)
        // The new list of chats comes first: an optional chain that assigns reads its right side inside its access to
        // `team`, and a read of `team` there breaks exclusive access.
        let chats = (team?.rooms[index].chats ?? []) + [chat]
        team?.rooms[index].chats = chats
        let key = TeamChatKey(room: room, chat: Self.previewNoticesChat)
        var list = TeamThreadList()
        list.take(page)
        teamThreads[key] = list
        teamChats[key.inThread(n3)] = TeamChat(messages: messages.messages.reversed(), loaded: true)
        teamChats[key.inThread(n1)] = TeamChat(messages: [page.threads[0].root], loaded: true)
    }
}
#endif
