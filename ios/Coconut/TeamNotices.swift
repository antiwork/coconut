import SwiftUI

/// The style of one kind of notice: its color. The symbol and the label come from NoticeKind.
extension NoticeKind {
    var tint: Color {
        switch self {
        case .announcement: .blue
        case .status: .orange
        case .rule: .purple
        case .deadline: .red
        }
    }
}

/// The notice board of a team: the active notices first, then the ones that ended. Each member posts a notice; its
/// author closes or removes it. The board tells the team which notices this person has seen when it shows.
struct NoticeBoardView: View {
    @Environment(AppStore.self) private var store
    let roomId: String
    @State private var composing = false

    private var room: TeamRoom? { store.team?.rooms.first { $0.room == roomId } }

    var body: some View {
        Group {
            if let room {
                board(room)
            } else {
                QuietEmptyState(symbol: "megaphone", title: "Board unavailable", message: "This Mac is no longer in this team.")
            }
        }
        .navigationTitle(room.map { "Notices · \($0.name)" } ?? "Notices")
        .toolbar {
            if room?.status == .active {
                ToolbarItem(placement: .primaryAction) {
                    Button { composing = true } label: { Label("New Notice", systemImage: "plus") }
                        .keyboardShortcut("n", modifiers: [.command, .shift])
                        .accessibilityIdentifier("notice-new")
                }
            }
        }
        .sheet(isPresented: $composing) {
            if let room { NoticeComposer(room: room) }
        }
        // The board shows, so the person has seen each active notice on it.
        .task(id: room?.noticeList.filter { $0.active && !$0.seen }.map(\.id)) {
            if let room { await store.markTeamNoticesSeen(room, notices: room.noticeList.filter(\.active)) }
        }
    }

    @ViewBuilder
    private func board(_ room: TeamRoom) -> some View {
        let active = room.noticeList.filter(\.active)
        let ended = room.noticeList.filter { !$0.active }
        if room.noticeList.isEmpty {
            QuietEmptyState(symbol: "megaphone", title: "No notices",
                            message: "Post what the whole team must know: a freeze, a rule, a deadline.")
        } else {
            List {
                if !active.isEmpty {
                    Section("Active") { ForEach(active) { NoticeRow(room: room, notice: $0) } }
                }
                if !ended.isEmpty {
                    Section("Ended") { ForEach(ended) { NoticeRow(room: room, notice: $0) } }
                }
            }
            #if os(macOS)
            .listStyle(.inset)
            #endif
        }
    }
}

/// One notice on the board: its kind, its text, its author, its times to the second, and who has seen it.
struct NoticeRow: View {
    @Environment(AppStore.self) private var store
    let room: TeamRoom
    let notice: WorkspaceNotice

    var body: some View {
        HStack(alignment: .top, spacing: BoardStyle.Space.m) {
            Image(systemName: notice.kind.symbol)
                .font(.title3)
                .foregroundStyle(notice.active ? notice.kind.tint : .secondary)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                HStack(spacing: BoardStyle.Space.s) {
                    Text(notice.kind.label.uppercased())
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(notice.active ? notice.kind.tint : .secondary)
                    if let end = notice.endsAt?.apiDate, notice.active {
                        TimelineView(.periodic(from: .now, by: 1)) { timeline in
                            Text(NoticeCountdown.text(until: end, now: timeline.date))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    } else if notice.closed {
                        Text("Closed").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Text(notice.text)
                    .font(.body)
                    .foregroundStyle(notice.active ? .primary : .secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Text(details)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, BoardStyle.Space.xs)
        .contextMenu {
            if notice.own {
                if notice.active {
                    Button("Close Notice") { Task { await store.closeTeamNotice(room, notice: notice) } }
                }
                Button("Remove Notice", role: .destructive) { Task { await store.deleteTeamNotice(room, notice: notice) } }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("notice-\(notice.id)")
    }

    /// "Grace · Nov 16, 4:02:17 PM · Seen by Ada, Lin" with the time to the second.
    private var details: String {
        var parts = [notice.own ? "You" : notice.authorName]
        if let posted = notice.postedAt.apiDate {
            parts.append(posted.formatted(.dateTime.month(.abbreviated).day().hour().minute().second()))
        }
        if let end = notice.endsAt?.apiDate {
            parts.append("Ends " + end.formatted(.dateTime.month(.abbreviated).day().hour().minute()))
        }
        parts.append(notice.seenBy.isEmpty ? "Not seen yet" : "Seen by " + notice.seenBy.joined(separator: ", "))
        return parts.joined(separator: " · ")
    }
}

/// The sheet that posts a notice: its kind, its text, and its end.
struct NoticeComposer: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let room: TeamRoom
    @State private var kind = NoticeKind.status
    @State private var text = ""
    @State private var ends = false
    @State private var end = Date().addingTimeInterval(86_400)
    @State private var posting = false

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var hasEnd: Bool { ends || kind.needsEnd }

    var body: some View {
        NavigationStack {
            Form {
                Picker("Kind", selection: $kind) {
                    ForEach(NoticeKind.allCases) { kind in
                        Label(kind.label, systemImage: kind.symbol).tag(kind)
                    }
                }
                .accessibilityIdentifier("notice-kind")
                TextField("What must the team know?", text: $text, axis: .vertical)
                    .lineLimit(2...6)
                    .accessibilityIdentifier("notice-text")
                if !kind.needsEnd {
                    Toggle("Ends at a time", isOn: $ends)
                }
                if hasEnd {
                    DatePicker("Ends", selection: $end, in: Date()...)
                }
            }
            .formStyle(.grouped)
            .navigationTitle("New Notice")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Post") {
                        posting = true
                        Task {
                            let posted = await store.postTeamNotice(room, kind: kind, text: trimmed,
                                                                    endsAt: hasEnd ? end : nil)
                            posting = false
                            if posted { dismiss() }
                        }
                    }
                    .disabled(trimmed.isEmpty || trimmed.count > 500 || posting)
                    .accessibilityIdentifier("notice-post")
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 300)
        #endif
    }
}

/// One line above the messages of a team chat: the newest active Status or Deadline. It stays while the notice is
/// active. "Seen" tells the team; the author closes it here.
struct NoticeBanner: View {
    @Environment(AppStore.self) private var store
    let room: TeamRoom
    let notice: WorkspaceNotice

    var body: some View {
        HStack(spacing: BoardStyle.Space.s) {
            Image(systemName: notice.kind.symbol).foregroundStyle(notice.kind.tint)
            Text(notice.kind.label).font(.callout.weight(.semibold)).foregroundStyle(notice.kind.tint)
            Text(notice.text).font(.callout).lineLimit(1).truncationMode(.tail)
            Spacer(minLength: BoardStyle.Space.s)
            if let end = notice.endsAt?.apiDate {
                TimelineView(.periodic(from: .now, by: 1)) { timeline in
                    Text(NoticeCountdown.text(until: end, now: timeline.date))
                        .font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            if !notice.seen {
                Button("Seen") { Task { await store.markTeamNoticesSeen(room, notices: [notice]) } }
                    .buttonStyle(.borderless)
                    .accessibilityIdentifier("notice-banner-seen")
            }
            if notice.own {
                Button { Task { await store.closeTeamNotice(room, notice: notice) } } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("Close this notice for the team")
                .accessibilityLabel("Close notice")
            }
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .padding(.vertical, BoardStyle.Space.s)
        .background(notice.kind.tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(notice.kind.tint.opacity(0.3)))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("notice-banner")
    }
}

/// Puts the banner of the newest active Status or Deadline of a team above the messages of its Team chat and its named
/// chats. A direct chat has none. On a Mac it floats under the title of the chat; on an iPhone it sits under the bar.
struct NoticeBannerArea: ViewModifier {
    let room: TeamRoom
    let shown: Bool
    let top: CGFloat

    func body(content: Content) -> some View {
        if shown, let notice = room.bannerNotice {
            #if os(macOS)
            content.overlay(alignment: .top) {
                NoticeBanner(room: room, notice: notice)
                    .padding(.horizontal, BoardStyle.Space.l)
                    .padding(.top, top)
            }
            #else
            content.safeAreaInset(edge: .top, spacing: 0) {
                NoticeBanner(room: room, notice: notice)
                    .padding(.horizontal, BoardStyle.Space.m)
                    .padding(.vertical, BoardStyle.Space.xs)
                    .background(.bar)
            }
            #endif
        } else {
            content
        }
    }
}
