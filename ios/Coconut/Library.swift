import SwiftUI

/// A page that a bot keeps in the Library (backend/coconut/documents.py). The list carries no content. The Mac sends
/// the content of the current version when the phone opens the document.
struct BotDocument: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var key: String
    var title: String
    /// "markdown" or "html".
    var format: String
    var summary: String? = nil
    var version: Int
    var createdAt: String
    var updatedAt: String
    var expiresAt: String? = nil
    var content: String? = nil
    /// The person pinned the document in the Library. The Mac sends no value for a document that never had a pin.
    var pinned: Bool? = nil
    /// The team of a page that a bot of another Mac shares. Nil for a document of this Mac.
    var team: DocumentTeam? = nil
    /// "team" for a document of this Mac that the person shares with at least one team. An older Mac sends none.
    var audience: String? = nil
    /// The teams that the person shares this document of this Mac with, in the order of their names. An older Mac
    /// sends none, and then the page has no share action.
    var teams: [DocumentTeam]? = nil

    var isPage: Bool { format == "html" }
    var isPinned: Bool { pinned == true }
    var isTeamPage: Bool { team != nil }
    var isSharedWithTeam: Bool { !isTeamPage && audience == "team" }
    /// The quiet line near the title of a shared page: "Shared with Example Team". Nil for a page that no team has.
    var sharedLine: String? {
        guard isSharedWithTeam else { return nil }
        let names = (teams ?? []).map(\.name)
        return names.isEmpty ? "Shared with the team" : "Shared with " + names.joined(separator: ", ")
    }
    /// Only the Mac of a team page can delete it. This Mac answers 403.
    var canDelete: Bool { !isTeamPage }

    /// Markdown without a top heading at its start. The page shows the title over the content, so a bot's own title
    /// heading would show a second title.
    static func body(_ markdown: String) -> String {
        let lines = markdown.split(separator: "\n", omittingEmptySubsequences: false)
        guard let first = lines.firstIndex(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }),
              lines[first].hasPrefix("# ") else { return markdown }
        // Only the blank lines after the heading go. The next line keeps its indent, which can start a code block.
        let rest = lines[(first + 1)...].drop { $0.trimmingCharacters(in: .whitespaces).isEmpty }
        return rest.joined(separator: "\n")
    }
}

struct DocumentList: Decodable, Sendable {
    let documents: [BotDocument]
    /// The bots of other Macs that own the team pages in `documents`. An older Mac sends none.
    var teamBots: [TeamBot]? = nil
}

struct DocumentTeam: Codable, Equatable, Sendable {
    var room: String
    var name: String
}

/// One item of the actions of a page: share the page with a team, or stop. The person shares a page of this Mac, one
/// page with one team (backend/coconut/team_pages.py). A bot of the person is never shared.
struct PageShareItem: Equatable, Identifiable {
    let room: String
    let name: String
    /// The team has the page now, so the item stops the share.
    let shared: Bool

    var id: String { room }
    var title: String { shared ? "Stop sharing with \(name)" : "Share with \(name)" }
    /// Two people, not the share symbol of the system, which sends a copy out of the app.
    var symbol: String { shared ? "person.2.slash" : "person.2" }

    /// One item for each active team of this Mac, in the order of the names. A team page of another Mac has none, and
    /// so has a page of an older Mac, which cannot share. A team of the page that the list of teams does not have yet
    /// keeps its stop item.
    static func items(for document: BotDocument, rooms: [TeamRoom]) -> [PageShareItem] {
        guard !document.isTeamPage, let teams = document.teams else { return [] }
        let shared = Set(teams.map(\.room))
        var items = rooms.filter { $0.status == .active }
            .map { PageShareItem(room: $0.room, name: $0.name, shared: shared.contains($0.room)) }
        let listed = Set(items.map(\.room))
        items += teams.filter { !listed.contains($0.room) }.map { PageShareItem(room: $0.room, name: $0.name, shared: true) }
        return items.sorted { ($0.name, $0.room) < ($1.name, $1.room) }
    }
}

/// A bot of another Mac of a team. Only the Library shows it, as the owner of its team pages.
struct TeamBot: Codable, Identifiable, Equatable, Sendable {
    struct Member: Codable, Equatable, Sendable {
        var id: String
        var name: String
    }

    var id: String
    var name: String
    var avatar: String
    var color: String
    var team: DocumentTeam
    var member: Member
}

/// The owner of a group of the Library: a bot of this Mac, or a team bot with the name of its team.
struct LibraryOwner: Equatable {
    let id: String
    let name: String
    let avatar: String
    let color: String
    var teamName: String? = nil

    init(_ bot: Bot) { id = bot.id; name = bot.name; avatar = bot.avatar; color = bot.color }

    init(_ bot: TeamBot) {
        id = bot.id; name = bot.name; avatar = bot.avatar; color = bot.color; teamName = bot.team.name
    }
}

struct DocumentRoute: Hashable {
    let id: String
}

/// The Library tab: the pinned documents, then the other documents of the bots, grouped by bot. The bot with the
/// latest update comes first, and a pin does not move a bot.
struct LibraryView: View {
    @Environment(AppStore.self) private var store
    /// Shows the documents of this bot only, as the inspector of a Mac chat does.
    var botId: String? = nil
    @State private var deleting: BotDocument?

    var body: some View {
        let sections = LibrarySections(documents: store.documents ?? [], bots: store.bots, teamBots: store.teamBots,
                                       botId: botId)
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                if store.documents?.isEmpty == true {
                    emptyLibrary
                }
                if !sections.pinned.isEmpty {
                    header("Starred") {
                        // The frame of a bot's avatar, so the title sits in one line with the names of the bots.
                        Image(systemName: "star.fill").font(.footnote).foregroundStyle(.secondary)
                            .frame(width: 24 * BotAvatar.scale, height: 24 * BotAvatar.scale)
                            .accessibilityHidden(true)
                    }
                    ForEach(sections.pinned) { row($0) }
                }
                ForEach(sections.groups, id: \.owner.id) { group in
                    header(group.owner.name, detail: group.owner.teamName) {
                        BotAvatar(symbol: group.owner.avatar, color: group.owner.color, size: 24,
                                  identity: group.owner.id, animated: false)
                    }
                    ForEach(group.documents) { row($0, pinMark: !sections.showsPinned) }
                }
            }
            .padding(.bottom, BoardStyle.Space.xl)
            // A document moves between the pinned documents and its bot.
            .animation(.snappy, value: sections.pinned.map(\.id))
        }
        .background(BoardStyle.paper)
        .refreshable { await store.loadDocuments() }
        // A load without a connection does nothing, so the Library loads again when the Mac connects.
        .task(id: store.connection) { await store.loadDocuments() }
    }

    /// `detail` is the name of the team of a team bot. It is quieter than the name of the bot.
    private func header(_ title: String, detail: String? = nil, @ViewBuilder icon: () -> some View) -> some View {
        HStack(spacing: 8) {
            icon()
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary).layoutPriority(1)
            if let detail {
                Text(detail).font(.subheadline).foregroundStyle(.tertiary)
            }
        }
        .lineLimit(1)
        .padding(.horizontal, BoardStyle.Space.l).padding(.top, BoardStyle.Space.xl).padding(.bottom, BoardStyle.Space.xs)
        .accessibilityAddTraits(.isHeader)
    }

    /// `pinMark` marks a pinned document in the group of its bot, in a Library without a section "Starred".
    private func row(_ document: BotDocument, pinMark: Bool = false) -> some View {
        NavigationLink(value: DocumentRoute(id: document.id)) {
            DocumentRow(document: document, pinMark: pinMark && document.isPinned)
        }
        .buttonStyle(HomePressStyle())
        .contextMenu {
            PinDocumentButton(document: document)
            if document.canDelete {
                Button(role: .destructive) { deleting = document } label: { Label("Delete…", systemImage: "trash") }
            }
        }
        // The question hangs on the row that the person pressed, so iOS shows it next to that row.
        .modifier(DeletePageDialog(page: Binding(get: { deleting?.id == document.id ? deleting : nil },
                                                 set: { deleting = $0 })))
        .accessibilityIdentifier("library-document-\(document.key)")
        .padding(.horizontal, BoardStyle.Space.l)
    }

    private var emptyLibrary: some View {
        VStack(spacing: 8) {
            Image(systemName: "rectangle.3.group").font(.largeTitle.weight(.light)).foregroundStyle(.secondary)
            Text("Your Library").font(.title3.weight(.semibold))
            Text("Agents keep pages here: notes, checklists, and charts that they update.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.horizontal, 40).padding(.top, 120)
    }
}

/// The documents of the Library in the order of the list. A pinned document shows once: with the pinned documents, or
/// in the group of its bot with a pin mark where the Library has no section "Starred".
struct LibrarySections {
    let pinned: [BotDocument]
    let groups: [(owner: LibraryOwner, documents: [BotDocument])]
    /// The Library has a section "Starred". Without it, `pinned` is empty and each pinned document is in its group.
    let showsPinned: Bool

    /// `botId` keeps the documents of one bot of this Mac. A team page has the ID of a team bot, so it never shows
    /// there.
    init(documents: [BotDocument], bots: [Bot], teamBots: [TeamBot] = [], botId: String? = nil,
         showsPinned: Bool = LibrarySections.platformShowsPinned) {
        self.showsPinned = showsPinned
        // A team bot ID has the form "t-<room>-<entry>" and a bot of this Mac has a UUID, so the two never meet.
        var owners = Dictionary(teamBots.map { ($0.id, LibraryOwner($0)) }) { first, _ in first }
        for bot in bots { owners[bot.id] = LibraryOwner(bot) }
        // A document of an owner that the app does not know stays hidden.
        let shown = documents.filter { (botId == nil || $0.botId == botId) && owners[$0.botId] != nil }
            .sorted { $0.updatedAt > $1.updatedAt }
        pinned = showsPinned ? shown.filter(\.isPinned) : []
        // The latest update of an owner counts its pinned documents, so a pin does not move the owner in the list.
        let latest = Dictionary(shown.map { ($0.botId, $0.updatedAt) }) { first, _ in first }
        groups = Dictionary(grouping: showsPinned ? shown.filter { !$0.isPinned } : shown, by: \.botId)
            .compactMap { id, documents in owners[id].map { (owner: $0, documents: documents) } }
            .sorted { (latest[$0.owner.id] ?? "") > (latest[$1.owner.id] ?? "") }
    }
}

/// The section "Starred" of the Mac sidebar: the pinned bots, then the pinned pages in the order of the pinned list of
/// the Library. As in the Library, a page of an owner that the app does not know stays hidden. The section shows when
/// it has a bot or a page.
struct SidebarPins {
    let bots: [Bot]
    let pages: [BotDocument]

    var isEmpty: Bool { bots.isEmpty && pages.isEmpty }

    /// `pinnedBots` are the pinned bots of the chat list. `bots` and `teamBots` are the owners that the Library knows.
    init(pinnedBots: [Bot], documents: [BotDocument], bots: [Bot], teamBots: [TeamBot] = []) {
        self.bots = pinnedBots
        pages = LibrarySections(documents: documents, bots: bots, teamBots: teamBots, showsPinned: true).pinned
    }
}

/// Where the Mac window shows a page of the Library. A pinned page has a row in the sidebar; another page opens inside
/// the Library, so the sidebar highlights "Library" and back goes to its list.
enum MacPagePlace: Equatable {
    /// The pinned row of the sidebar is selected.
    case pinnedRow(String)
    /// The Library is selected, with the page pushed.
    case inLibrary(String)
    /// The Library is selected, with its list.
    case libraryList

    /// A document card in a chat: the pinned row of a pinned page, else the page inside the Library.
    static func card(_ id: String, documents: [BotDocument]?) -> MacPagePlace {
        documents?.first { $0.id == id }?.isPinned == true ? .pinnedRow(id) : .inLibrary(id)
    }

    /// The place of the page of a pinned row after the list changed, or nil to keep the row. A page without its pin
    /// stays open inside the Library. A deleted page leaves the Library list. Only a loaded list decides, and any other
    /// change of the list keeps the page open.
    static func afterChange(open id: String, documents: [BotDocument]?) -> MacPagePlace? {
        guard let documents else { return nil }
        guard let document = documents.first(where: { $0.id == id }) else { return .libraryList }
        return document.isPinned ? nil : .inLibrary(id)
    }
}

/// Asks before a page is deleted, as the dialog of a bot does. The Mac keeps no copy of a deleted page, and a page that
/// the person shares with a team goes for each member. `deleted` runs after the Mac confirmed the delete.
struct DeletePageDialog: ViewModifier {
    @Environment(AppStore.self) private var store
    @Binding var page: BotDocument?
    var deleted: () -> Void = {}

    func body(content: Content) -> some View {
        content.confirmationDialog("Delete this page?",
                                   isPresented: Binding(get: { page != nil }, set: { if !$0 { page = nil } }),
                                   titleVisibility: .visible, presenting: page) { page in
            Button("Delete", role: .destructive) { Task { if await store.deleteDocument(page) { deleted() } } }
                .accessibilityIdentifier("confirm-delete-page")
            Button("Cancel", role: .cancel) {}
        } message: { page in
            Text(page.deleteMessage)
        }
    }
}

/// Pins a document in the Library, or removes its pin.
private struct PinDocumentButton: View {
    @Environment(AppStore.self) private var store
    let document: BotDocument

    var body: some View {
        // The menu of a row can keep the document of an earlier list. The current list has the current pin.
        let document = store.documents?.first { $0.id == self.document.id } ?? self.document
        Button { Task { await store.pinDocument(document, pinned: !document.isPinned) } } label: {
            Label(document.isPinned ? "Unstar" : "Star", systemImage: document.isPinned ? "star.slash" : "star")
        }
        .accessibilityIdentifier("document-pin")
    }
}

private struct DocumentRow: View {
    let document: BotDocument
    var pinMark = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: document.symbol)
                .font(.body).foregroundStyle(.secondary)
                .frame(width: 36, height: 36)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                // The pin mark follows the title on its line. A long title is cut before the mark.
                HStack(spacing: BoardStyle.Space.xs) {
                    Text(document.title).font(.body).foregroundStyle(.primary).lineLimit(1)
                    if pinMark {
                        Image(systemName: "star.fill").font(.footnote).foregroundStyle(.secondary)
                            .accessibilityLabel("Starred")
                    }
                }
                Text(document.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let ends = document.endText {
                Text(ends).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
            }
            if let shared = document.sharedLine {
                Image(systemName: "person.2.fill").font(.footnote).foregroundStyle(.secondary)
                    .accessibilityLabel(shared)
            }
        }
        .padding(.vertical, BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.paper)
        .contentShape(Rectangle())
    }
}

extension BotDocument {
    /// The message of the question before a delete. A page that the person shares with a team goes for the team too.
    var deleteMessage: String {
        isSharedWithTeam ? "Coconut removes each version, also for your team. You cannot undo this."
                         : "Coconut removes each version. You cannot undo this."
    }

    /// The symbol of the kind: a chart for an HTML page, a sheet of text for Markdown.
    var symbol: String { isPage ? "chart.bar.xaxis" : "doc.text" }

    /// The summary of the bot, or the time of the last update.
    var detail: String { summary.flatMap { $0.isEmpty ? nil : $0 } ?? updatedText }

    var updatedText: String {
        guard let date = updatedAt.apiDate else { return "" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "Updated " + date.formatted(date: .omitted, time: .shortened) }
        return "Updated " + date.formatted(.dateTime.month(.abbreviated).day())
    }

    /// "Ends Fri" for a temporary document, with the weekday in the next week and the date after it.
    var endText: String? {
        guard let date = expiresAt?.apiDate else { return nil }
        let days = Calendar.current.dateComponents([.day], from: .now, to: date).day ?? 0
        if Calendar.current.isDateInToday(date) { return "Ends today" }
        return "Ends " + (days < 6 ? date.formatted(.dateTime.weekday(.abbreviated))
                                   : date.formatted(.dateTime.month(.abbreviated).day()))
    }

    /// The line under the title of an open document: "Tuner · updated 8:00 AM · ends Fri". A team page names its
    /// team after its bot.
    func meta(botName: String?) -> String {
        func lowered(_ text: String) -> String { text.prefix(1).lowercased() + text.dropFirst() }
        return ([botName, team?.name, lowered(updatedText), endText.map(lowered)].compactMap { $0 }
            .filter { !$0.isEmpty })
            .joined(separator: " · ")
    }
}

/// One open document in a closed web view (DocumentPage): an HTML page, or Markdown that the page turns into HTML.
struct DocumentView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.showTeamChat) private var showTeamChat
    let documentId: String
    @State private var document: BotDocument?
    @State private var failed = false
    @State private var link: URL?
    @State private var deleting: BotDocument?

    private var listed: BotDocument? { store.documents?.first { $0.id == documentId } }
    /// A bot of this Mac. A team page has none, so the actions that need one are not there.
    private var bot: Bot? { (document ?? listed).flatMap { item in store.bots.first { $0.id == item.botId } } }
    private var botName: String? {
        guard let item = document ?? listed else { return nil }
        return bot?.name ?? store.teamBots.first { $0.id == item.botId }?.name
    }

    var body: some View {
        Group {
            if let document, let content = document.content {
                // Markdown and HTML share the page view, its article style and its blocks. The list has the current
                // teams of the page. The open document has the teams of the time it loaded.
                DocumentPage(document: document, meta: document.meta(botName: botName),
                             shared: (listed ?? document).sharedLine, content: content) {
                    link = $0
                }
                    .ignoresSafeArea(edges: .bottom)
            } else if failed {
                ContentUnavailableView {
                    Label("Coconut could not load this page", systemImage: "doc.text")
                } description: {
                    Text("Check the connection to your Mac and try again.")
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        // Pin, Share, Delete and the load can fail.
        .pageErrorBanner()
        .background(BoardStyle.paper)
        .navigationTitle("")
        .inlineNavigationTitle()
        .toolbar {
            if let document, let content = document.content {
                ToolbarItem(placement: .trailingBar) {
                    Menu {
                        if let target = DocumentChatTarget(listed ?? document, bots: store.bots, team: store.team) {
                            Button { ask(target, about: document) } label: {
                                Label("Chat about this page", systemImage: "bubble.left")
                            }
                            .accessibilityIdentifier("document-ask")
                        }
                        // The list has the current pin. The open document has the pin of the time it loaded.
                        PinDocumentButton(document: listed ?? document)
                        ForEach(PageShareItem.items(for: listed ?? document, rooms: store.team?.rooms ?? [])) { item in
                            Button {
                                Task { await store.sharePage(listed ?? document, with: item.room, shared: !item.shared) }
                            } label: { Label(item.title, systemImage: item.symbol) }
                                .accessibilityIdentifier("document-share-\(item.room)")
                        }
                        ShareLink(item: content, subject: Text(document.title)) {
                            Label("Share", systemImage: "square.and.arrow.up")
                        }
                        if document.canDelete {
                            Button(role: .destructive) { deleting = listed ?? document } label: {
                                Label("Delete…", systemImage: "trash")
                            }
                        }
                    } label: { Image(systemName: "ellipsis") }
                        .accessibilityLabel("Page actions")
                        // The question hangs on the button of the menu, so iOS shows it next to the button.
                        .modifier(DeletePageDialog(page: $deleting) { dismiss() })
                }
            }
        }
        .confirmationDialog("Open \(link?.host() ?? "this link")?", isPresented: Binding(
            get: { link != nil }, set: { if !$0 { link = nil } }), titleVisibility: .visible) {
            Button("Open in browser") { if let link { openURL(link) } }
        } message: { Text(link?.absoluteString ?? "") }
        .task(id: documentId) { await load() }
        // The share actions name the teams of this Mac. A Library that opened before the Team screen loads them.
        .task(id: store.connection) { if store.team == nil { await store.loadTeam() } }
        .onChange(of: store.connection) { _, connection in
            // A document that failed to load loads again when the Mac connects.
            if failed && connection == .connected { Task { await load() } }
        }
        .onChange(of: listed?.version) { _, version in
            // A newer version in the list loads again. A removed document keeps what the view shows.
            guard let version, version > document?.version ?? 0 else { return }
            Task { await load() }
        }
    }

    /// Opens the chat of the target with the page quoted in the composer, as a reply quotes a message. The chat of a
    /// bot of this Mac opens as today. The direct chat with a team bot opens where each other way to it opens it: on
    /// the Chats tab of an iPhone, and in the sidebar of the Mac window.
    private func ask(_ target: DocumentChatTarget, about document: BotDocument) {
        store.ask(about: document, in: target)
        switch target {
        case .bot(let id): if let openChat { openChat(id) } else { pushRoute?(ConversationRoute(botId: id)) }
        case .team(let chat): showTeamChat?(chat)
        }
    }

    private func load() async {
        guard let loaded = await store.loadDocument(id: documentId) else { failed = document == nil; return }
        failed = false
        document = loaded
    }

}

/// A document in a chat: a card that opens it in the Library. The service adds one when a bot saves a new document, or
/// when Coconut keeps a report of the bot (backend/coconut/library_replies.py).
struct DocumentCard: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openDocument) private var openDocument
    @Environment(\.pushRoute) private var pushRoute
    let documentId: String
    /// The title when the card was made. The Library list has the current title and summary.
    let title: String
    /// The card loaded a fresh Library list. Only such a list can say that the page is gone.
    @State private var listFresh = false
    private var document: BotDocument? { store.documents?.first { $0.id == documentId } }
    /// Someone deleted the page, so the card goes too. A later list that has the page brings the card back.
    private var gone: Bool { listFresh && store.documents != nil && document == nil }

    var body: some View {
        if !gone { card }
    }

    private var card: some View {
        Button {
            if let openDocument { openDocument(documentId) } else { pushRoute?(DocumentRoute(id: documentId)) }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: document?.symbol ?? "doc.text")
                    .font(.body).foregroundStyle(.secondary)
                    .frame(width: 36, height: 36)
                    .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(document?.title ?? title).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                        .lineLimit(2).multilineTextAlignment(.leading)
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
            .contentShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the page")
        .accessibilityIdentifier("document-card-\(documentId)")
        // A new document is not in the list yet. One load brings its summary.
        .task(id: documentId) {
            guard document == nil else { listFresh = true; return }
            listFresh = await store.loadDocuments()
        }
    }

    private var detail: String {
        guard let document else { return "Library" }
        return "Library · " + document.detail
    }
}

extension BotDocument {
    /// The documents of demo mode.
    static func previews(bots: [Bot]) -> [BotDocument] {
        guard let first = bots.first else { return [] }
        let second = bots.dropFirst().first ?? first
        let now = Date.now
        func time(_ hours: Double) -> String { now.addingTimeInterval(-hours * 3600).ISO8601Format() }
        return [
            BotDocument(id: "preview-waitlist", botId: first.id, key: "waitlist-own-website",
                        title: "A waitlist form on a seller's own website", format: "markdown",
                        summary: "Decided: make the existing field official", version: 2, createdAt: time(30),
                        updatedAt: time(0.5), content: previewWaitlist),
            BotDocument(id: "preview-revenue", botId: first.id, key: "revenue-by-category", title: "Revenue by category",
                        format: "html", summary: "$95,000 this month, up 6%", version: 14, createdAt: time(400),
                        updatedAt: time(1), content: previewTreemap, pinned: true),
            BotDocument(id: "preview-summary", botId: first.id, key: "weekly-summary", title: "Weekly summary",
                        format: "markdown", summary: nil, version: 3, createdAt: time(200), updatedAt: time(30),
                        content: "## Highlights\n\n- Signups grew 9% to 412 a day.\n- Refunds fell to 1.2%.\n\n"
                            + "## Next week\n\nTest the new price on two products."),
            BotDocument(id: "preview-checklist", botId: second.id, key: "launch-checklist", title: "Launch checklist",
                        format: "markdown", summary: "2 of 4 done", version: 3, createdAt: time(50),
                        updatedAt: time(3), expiresAt: now.addingTimeInterval(3 * 86400).ISO8601Format(),
                        content: "## Before launch\n\n- [x] Price set to $29\n- [x] Cover image\n"
                            + "- [ ] Email to 1,240 followers\n- [ ] Discount code for week 1\n\n## Notes\n\n"
                            + "The refund window stays 30 days."),
        ]
    }

    /// A Markdown page with each block: the lead, a callout, a diagram, a chart, a table and a task list.
    private static let previewWaitlist = """
    Jordi wants visitors to join the waitlist for one product from a form on his own site, without leaving it.

    > [!TIP] Decided Nov 5 · Option A
    > **Make the existing field official.** Add a product picker to the Subscribe form and document it. No server change.

    ## How a signup works

    ```mermaid
    flowchart TD
      A[Visitor submits the form] --> B[Gumroad receives seller and product]
      B -->|Reviewed seller| C[Added to the waitlist]
      B -->|Not reviewed| D[Sent to the product page]
    ```

    ## Waitlist signups by week

    ```chart
    {"type": "bar", "x": "week", "y": "signups", "data": [{"week": "Oct 13", "signups": 42}, {"week": "Oct 20", "signups": 61}, {"week": "Oct 27", "signups": 58}, {"week": "Nov 3", "signups": 87}]}
    ```

    ## Where forms work

    | Place | Status |
    |---|---|
    | Product page | Shipped |
    | Custom HTML | Shipped |
    | Seller's website | In progress |

    ## Next

    - [x] Post the decision on the issue
    - [ ] Gumclaw opens a draft PR
    - [ ] One merge card after the review

    > [!WARNING]
    > A seller who is not reviewed sends visitors to the product page.
    """

    private static let previewTreemap = """
    <p class="metric">$95,000 <span class="delta">+6% this month</span></p>
    <div id="chart"></div>
    <p id="detail" class="note">Tap a category.</p>
    <script>
    const data = {children: [
      {name: "Courses", value: 41200}, {name: "Ebooks", value: 18300}, {name: "Memberships", value: 15800},
      {name: "Templates", value: 9400}, {name: "Presets", value: 6100}, {name: "Other", value: 4200}]};
    const width = document.getElementById("chart").clientWidth, height = 300;
    const root = d3.hierarchy(data).sum(d => d.value).sort((a, b) => b.value - a.value);
    d3.treemap().size([width, height]).paddingInner(3).round(true)(root);
    const svg = d3.select("#chart").append("svg").attr("width", width).attr("height", height);
    const cell = svg.selectAll("g").data(root.leaves()).join("g")
      .attr("transform", d => `translate(${d.x0},${d.y0})`)
      .on("click", (event, d) => {
        document.getElementById("detail").textContent =
          `${d.data.name}: $${d.value.toLocaleString()}, ${Math.round(d.value / root.value * 100)}% of revenue`;
      });
    cell.append("rect").attr("width", d => d.x1 - d.x0).attr("height", d => d.y1 - d.y0).attr("rx", 6)
      .attr("fill", (d, i) => coconut.palette[i]);
    const big = cell.filter(d => d.x1 - d.x0 > 60 && d.y1 - d.y0 > 36);
    big.append("text").attr("class", "on-color").attr("x", 8).attr("y", 18).text(d => d.data.name);
    big.append("text").attr("class", "on-color quiet").attr("x", 8).attr("y", 33)
      .text(d => "$" + (d.value / 1000).toFixed(1) + "k");
    </script>
    """
}
