import SwiftUI

/// The search of the whole app on the Mac (OmniSearch), as the switcher of Slack: a panel at the top of the window
/// with one field. Command-K and the Search row of the sidebar open it, and an empty field lists the newest chats. The
/// arrows move the choice, Return opens it, and Escape, Command-K or a click outside the panel closes it.
struct MacOmnibar: View {
    /// One row of the panel.
    enum Hit: Identifiable {
        /// A place of the sidebar, by its name.
        case place(title: String, symbol: String, MacSelection)
        case chat(ChatListItem)
        case todo(TodoIssue)
        case page(BotDocument)
        case card(Decision)
        case message(OmniSearch.MessageHit)

        var id: String {
            switch self {
            case .place(let title, _, _): "place-" + title
            case .chat(let item): "chat-" + item.id
            case .todo(let todo): "todo-\(todo.number)"
            case .page(let page): "page-" + page.id
            case .card(let card): "card-" + card.id
            case .message(let hit): "message-" + hit.id
            }
        }
    }

    /// The rows under one title.
    private struct HitGroup: Identifiable {
        let title: String
        let hits: [Hit]
        var id: String { title }
    }

    /// Where the pointer was when it last chose a row. A list that scrolls under a still pointer chooses nothing.
    private final class Pointer {
        var location: CGPoint?
    }

    /// The places of the sidebar, in its order.
    static let places: [Hit] = [
        .place(title: "Home", symbol: "house", .home),
        .place(title: "Live", symbol: "dot.radiowaves.left.and.right", .live),
        .place(title: "Spend", symbol: "dollarsign.circle", .spend),
        .place(title: "Finance", symbol: "chart.xyaxis.line", .business),
        .place(title: "Support", symbol: "lifepreserver", .support),
        .place(title: "Projects", symbol: "square.stack.3d.up", .agents),
        .place(title: "You", symbol: "person.crop.circle", .profile),
    ]

    @Environment(AppStore.self) private var store
    /// Shows a row in the window. The window closes the panel.
    let open: (Hit) -> Void
    let close: () -> Void
    @State private var query = ""
    /// The row that Return opens, by its ID. Nil is the first row.
    @State private var chosen: String?
    /// The messages that have the words of the search, of this Mac's copy and then of the service.
    @State private var messageHits: [OmniSearch.MessageHit] = []
    /// The search whose messages came from the service too. Until then an empty search says nothing.
    @State private var searchedQuery = ""
    @State private var listHeight: CGFloat = 0
    @State private var pointer = Pointer()
    @FocusState private var fieldFocused: Bool

    var body: some View {
        let words = OmniSearch.words(query)
        let groups = groups(words)
        let hits = groups.flatMap(\.hits)
        let current = chosenRow(in: hits)
        let shape = RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous)
        ZStack(alignment: .top) {
            // A click outside the panel closes it.
            Color.black.opacity(0.08)
                .contentShape(Rectangle())
                .onTapGesture(perform: close)
                .accessibilityHidden(true)
            ScrollViewReader { proxy in
                VStack(spacing: 0) {
                    field(proxy: proxy)
                    if !hits.isEmpty {
                        Divider()
                        list(groups, current: current?.id, words: words)
                    } else if !words.isEmpty && searchedQuery == query {
                        Divider()
                        Text("No results")
                            .font(.callout).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, BoardStyle.Space.xl)
                            .accessibilityIdentifier("omnibar-empty")
                    }
                }
                .onChange(of: query) {
                    chosen = nil
                    proxy.scrollTo(Self.top, anchor: .top)
                }
            }
            .frame(width: 640)
            .background(BoardStyle.paper)
            .clipShape(shape)
            .overlay { shape.strokeBorder(BoardStyle.line) }
            .shadow(color: .black.opacity(0.25), radius: 24, y: 12)
            .padding(.top, BoardStyle.Space.xxl)
            .padding(.horizontal, BoardStyle.Space.xl)
        }
        .task(id: query) {
            if await OmniSearch.messages(query, in: store, show: { messageHits = $0 }) { searchedQuery = query }
        }
        .onAppear { fieldFocused = true }
    }

    /// The ID of the top of the list, where a new search starts.
    private static let top = "omnibar-top"

    /// The field, which keeps the keys: the arrows move the choice and Return opens it. Each key reads the rows
    /// again, as the field on the Mac can keep the submit action of an earlier search.
    private func field(proxy: ScrollViewProxy) -> some View {
        HStack(spacing: BoardStyle.Space.s) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary).accessibilityHidden(true)
            TextField("Search", text: $query)
                .textFieldStyle(.plain)
                .focused($fieldFocused)
                .onSubmit { if let hit = chosenRow(in: rows()) { open(hit) } }
                .onKeyPress(.downArrow, phases: .down) { _ in
                    move(1, proxy: proxy)
                    return .handled
                }
                .onKeyPress(.upArrow, phases: .down) { _ in
                    move(-1, proxy: proxy)
                    return .handled
                }
                .onExitCommand(perform: close)
                .accessibilityIdentifier("omnibar-field")
        }
        .font(.title3)
        .padding(.horizontal, BoardStyle.Space.m)
        .padding(.vertical, BoardStyle.Space.m)
    }

    /// The rows of the search in their order.
    private func rows() -> [Hit] { groups(OmniSearch.words(query)).flatMap(\.hits) }

    /// The row that Return opens: the chosen one, else the first.
    private func chosenRow(in hits: [Hit]) -> Hit? { hits.first { $0.id == chosen } ?? hits.first }

    private func move(_ step: Int, proxy: ScrollViewProxy) {
        let hits = rows()
        guard !hits.isEmpty else { return }
        let index = hits.firstIndex { $0.id == chosen } ?? 0
        let next = hits[min(max(index + step, 0), hits.count - 1)].id
        chosen = next
        proxy.scrollTo(next)
    }

    /// The rows of a search: the places and the chats by their names, the to-dos, the pages and the cards that wait,
    /// then the messages, newest first. Without words, the newest chats.
    private func groups(_ words: [String]) -> [HitGroup] {
        guard !words.isEmpty else {
            return [HitGroup(title: "Recent", hits: OmniSearch.chats([], in: store).map(Hit.chat))]
        }
        let places = Self.places.filter { hit in
            if case .place(let title, _, _) = hit { return OmniSearch.rank(title, words) != nil }
            return false
        }
        return [
            HitGroup(title: "Places", hits: places),
            HitGroup(title: "Chats", hits: OmniSearch.chats(words, in: store).map(Hit.chat)),
            HitGroup(title: "To-dos", hits: OmniSearch.todos(words, in: store).map(Hit.todo)),
            HitGroup(title: "Pages", hits: OmniSearch.pages(words, in: store).map(Hit.page)),
            HitGroup(title: "Cards", hits: OmniSearch.cards(words, in: store).map(Hit.card)),
            HitGroup(title: "Messages", hits: messageHits.map(Hit.message)),
        ].filter { !$0.hits.isEmpty }
    }

    /// The rows, as high as they are up to 440 points, which then scroll.
    private func list(_ groups: [HitGroup], current: String?, words: [String]) -> some View {
        let continued = OmniSearch.continued(messageHits)
        return ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Color.clear.frame(height: 0).id(Self.top)
                ForEach(groups) { group in
                    Text(group.title)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, BoardStyle.Space.s)
                        .padding(.top, BoardStyle.Space.s)
                        .padding(.bottom, BoardStyle.Space.xs)
                        .accessibilityAddTraits(.isHeader)
                    ForEach(group.hits) { hit in
                        row(hit, isChosen: hit.id == current, words: words, continued: continued)
                    }
                }
            }
            .padding(BoardStyle.Space.xs)
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { listHeight = $0 }
        }
        .frame(height: min(max(listHeight, 1), 440))
    }

    /// A row: a click opens it, and the pointer chooses it as it moves over it.
    private func row(_ hit: Hit, isChosen: Bool, words: [String], continued: Set<String>) -> some View {
        Button { open(hit) } label: {
            label(of: hit, words: words, continued: continued)
                .padding(.horizontal, BoardStyle.Space.s)
                .padding(.vertical, BoardStyle.Space.xs)
                .frame(maxWidth: .infinity, minHeight: 28, alignment: .leading)
                .background(isChosen ? Color.primary.opacity(0.08) : .clear, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onContinuousHover(coordinateSpace: .global) { phase in
            guard case .active(let location) = phase, location != pointer.location else { return }
            pointer.location = location
            if chosen != hit.id { chosen = hit.id }
        }
        .id(hit.id)
        .accessibilityIdentifier("omnibar-" + hit.id)
    }

    @ViewBuilder
    private func label(of hit: Hit, words: [String], continued: Set<String>) -> some View {
        switch hit {
        case .place(let title, let symbol, _):
            line(title, detail: nil) { Image(systemName: symbol).foregroundStyle(.secondary) }
        case .chat(let item):
            line(OmniSearch.name(of: item), detail: OmniSearch.isArchived(item) ? "Archived" : nil) { face(item) }
        case .todo(let todo):
            line(todo.title, detail: OmniSearch.detail(of: todo)) { Image(systemName: todo.symbol).foregroundStyle(todo.tint) }
        case .page(let page):
            line(page.title, detail: OmniSearch.owner(page.botId, in: store)) {
                Image(systemName: page.symbol).foregroundStyle(.secondary)
            }
        case .card(let card):
            line(card.title, detail: OmniSearch.owner(card.botId, in: store)) {
                Image(systemName: OmniSearch.cardSymbol).foregroundStyle(BoardStyle.attention)
            }
        case .message(let hit):
            // A message in the chat of the row above shows only its words and its age.
            let inChatAbove = continued.contains(hit.id)
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                ZStack {
                    if !inChatAbove, let bot = store.bots.first(where: { $0.id == hit.botId }) { face(.bot(bot)) }
                }
                .frame(width: 20)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    if !inChatAbove {
                        Text(OmniSearch.owner(hit.botId, in: store) ?? "Chat").lineLimit(1)
                    }
                    Text(OmniSearch.highlighted((hit.role == "user" ? "You: " : "") + hit.snippet, words))
                        .font(.callout).foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Spacer(minLength: BoardStyle.Space.s)
                if let date = hit.createdAt.apiDate {
                    CompactAgeLabel(date: date).font(.caption).foregroundStyle(.tertiary)
                }
            }
        }
    }

    /// A row of one line: an icon, the title, and a quiet detail at the right.
    private func line(_ title: String, detail: String?, @ViewBuilder icon: () -> some View) -> some View {
        HStack(spacing: BoardStyle.Space.s) {
            icon().frame(width: 20)
            Text(title).lineLimit(1)
            Spacer(minLength: BoardStyle.Space.s)
            if let detail {
                Text(detail).font(.callout).foregroundStyle(.tertiary).lineLimit(1)
            }
        }
    }

    /// The face of a chat as the sidebar shows it: a bubble for a chat that rests, the dot of a project while it
    /// works, the face of a team or of the other side of a direct chat.
    @ViewBuilder
    private func face(_ item: ChatListItem) -> some View {
        switch item {
        case .bot(let bot):
            if bot.isChat && !bot.archived && !store.isBotActive(bot) {
                Image(systemName: "bubble.left").font(.caption).foregroundStyle(.tertiary)
            } else {
                ZStack { BotActivityDot(bot: bot) }.frame(width: 12)
            }
        case .team(let room): TeamFace(room: room, size: 20)
        case .direct(let room, let direct): DirectPeerFace(peer: room.directPeer(direct.member), size: 20)
        case .botDirect(let room, let direct): DirectPeerFace(peer: room.botPeer(direct.bot), size: 20)
        }
    }
}
