import SwiftUI

/// The three to-do lists of Coconut: Product, Support and Finance, one on each tab of that name with its own count.
/// A to-do is an open issue of antiwork/gumroad-private, where the team and the agents work, and nothing else holds a
/// to-do: no other repository, no chat and no card of Coconut. The label of the issue names its list (`product`,
/// `support`, `finance`), so GitHub shows the same split as the tabs. An issue with none of the labels is a to-do of
/// Product, the list of the repository's issues before the split, and an issue with two is in both lists (backend
/// github_todos.py). The person asked for three lists in place of one ("more context full"), so a new place that shows
/// to-dos shows one list, never a mix.
enum TodoArea: String, CaseIterable, Codable, Hashable, Sendable {
    case finance, support, product

    var title: String {
        switch self {
        // Money, payouts, risk, tax, accounting and marketing.
        case .finance: "Finance"
        case .support: "Support"
        case .product: "Product"
        }
    }

    /// The symbol of the tab of the list.
    var symbol: String {
        switch self {
        case .finance: "chart.xyaxis.line"
        case .support: "lifepreserver"
        case .product: "hammer"
        }
    }
}

/// One to-do: an open issue with the fields of its row.
struct TodoIssue: Codable, Identifiable, Hashable, Sendable {
    var number: Int
    var title: String
    /// The lists of the issue, by their keys: finance, support or product.
    var areas: [String]
    var labels: [GitHubLabel]
    var assignees: [String]
    var author: String?
    var comments: Int
    var createdAt: String?
    var updatedAt: String?
    var url: String?
    /// The issue is assigned to the connected GitHub account: the person.
    var mine: Bool

    var id: Int { number }

    func belongs(to area: TodoArea) -> Bool { areas.contains(area.rawValue) }

    private var labelNames: [String] { labels.map(\.name) }
    /// An agent or a person works on it now.
    var working: Bool { labelNames.contains("working") }
    /// "priority/high", and "priority", its older name. It comes first in its list.
    var urgent: Bool { labelNames.contains("priority/high") || labelNames.contains("priority") }
    /// Who it waits for, from its `awaiting-` label: "human", "stripe", "seller".
    var awaiting: String? {
        labelNames.first { $0.hasPrefix("awaiting-") }.map { String($0.dropFirst("awaiting-".count)) }
    }

    var symbol: String {
        if urgent { return "exclamationmark.circle" }
        return AgentTodos.symbol(working ? "working" : awaiting == nil ? "open" : "waiting")
    }

    var tint: Color {
        if urgent { return BoardStyle.destructive }
        if working { return BoardStyle.positive }
        return awaiting == "human" ? BoardStyle.attention : .secondary
    }

    /// "#4871 · Awaiting human · gianfrancopiana · Nov 21": the number, the state, who has it and the day it came. An
    /// issue without an assignee names nobody: its author did not take it.
    func detail(now: Date = .now) -> String {
        let state = working ? "Working" : awaiting.map { "Awaiting " + $0.replacingOccurrences(of: "-", with: " ") }
        let who = mine ? "You" : assignees.first
        let day = createdAt?.apiDate.map { BusinessFormat.date($0, now: now) }
        // A number of GitHub is a name, so it has no separator of thousands.
        return ["#" + String(number), state, who, day].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The page of the issue in Coconut (GitHubItemView).
    func route(in repository: String) -> GitHubItemRoute {
        url.flatMap(URL.init(string:)).flatMap(InAppLink.route) as? GitHubItemRoute
            ?? GitHubItemRoute(repository: repository, number: number)
    }
}

/// The open to-dos of every list, as the Mac read them.
struct TodoIssues: Codable, Equatable, Sendable {
    /// owner/name: antiwork/gumroad-private.
    var repository: String
    var login: String?
    /// Newest activity first.
    var items: [TodoIssue]

    /// The to-dos of one list: high priority first, then newest activity first, as GitHub sorts them.
    func items(in area: TodoArea) -> [TodoIssue] {
        let shown = items.filter { $0.belongs(to: area) }
        return shown.filter(\.urgent) + shown.filter { !$0.urgent }
    }

    /// The file of the last list. The counts of the tabs show it at the next start until the Mac answers. A new
    /// pairing and another GitHub account remove it (resetBusiness).
    static let cache = "todos.json"
}

/// The number on the tab of a list: its to-dos and the breaches of the business that are its own and no to-do of the
/// list, which the overview of Finance proves while it is current (AttentionRules).
/// Support past SLA counts on Support (conversations of the customers), and the risk reviews past SLA count on Finance
/// (reviews of sellers): the person asked for the needs attention of the business to show as to-dos. The breaches
/// about the issues themselves, product issues past SLA and high priority, count once, as their to-dos. The cards, the
/// work that agents track and the alerts of the agents are no to-dos of these lists, so they add to no tab.
enum TodoCounts {
    /// `reviews`: the risk reviews that Finance lists (RiskReviewList.current). They take the place of the breach of
    /// the risk reviews, and the open issue of a review is the same work, so it counts once, as its review. A review
    /// whose issue Gumclaw closed is decided: it shows until the next publish drops it, and counts no more.
    static func count(_ area: TodoArea, todos: TodoIssues?, attention: [AttentionItem] = [],
                      reviews: [RiskReview]? = nil) -> Int {
        let reviews = area == .finance ? reviews : nil
        let hidden = reviewIssues(reviews)
        let listed = todos?.items(in: area).filter { !hidden.contains($0.number) }.count ?? 0
        let breached = reviews.map { $0.filter { !$0.decided }.count } ?? breaches(area, attention).reduce(0) { $0 + $1.count }
        return listed + breached
    }

    /// The numbers of the open issues of the reviews that a list shows as reviews.
    static func reviewIssues(_ reviews: [RiskReview]?) -> Set<Int> {
        Set(reviews?.compactMap { $0.withGumclaw ? $0.issue?.number : nil } ?? [])
    }

    /// The rows of Needs attention that belong to a list.
    static func breaches(_ area: TodoArea, _ attention: [AttentionItem]) -> [AttentionItem] {
        let key: String? = switch area {
        case .support: "support"
        case .finance: "risk"
        case .product: nil
        }
        return attention.filter { $0.kind == .business && $0.key == key }
    }
}

private struct TodoMoveRequest: Encodable { let number: Int; let area: String }

extension AppStore {
    /// The badge of the tab of a list (TodoCounts).
    func todoCount(_ area: TodoArea, now: Date = Date()) -> Int {
        TodoCounts.count(area, todos: todoIssues, attention: attentionItems(now: now),
                         reviews: riskReviews?.current(now: now))
    }

    /// Reads the to-dos again. A failure keeps the list that shows, with the error for a list that has none yet.
    func loadTodoIssues(fresh: Bool = false) async {
        if isDemo {
            if todoIssues == nil { todoIssues = .preview }
            return
        }
        guard connection == .connected else { return }
        do {
            let list: TodoIssues = try await businessRequest("todos" + (fresh ? "?fresh=1" : ""))
            receiveTodoIssues(list)
            if todoIssuesError != nil { todoIssuesError = nil }
        } catch is CancellationError {
        } catch {
            todoIssuesError = error.localizedDescription
        }
    }

    private func receiveTodoIssues(_ list: TodoIssues) {
        guard list != todoIssues else { return }
        todoIssues = list
        if !isDemo { try? ProtectedCache.write(list, name: TodoIssues.cache) }
    }

    /// Moves a to-do to another list: the label of the list changes on GitHub. The row moves at once and comes back
    /// when GitHub refuses.
    func moveTodoIssue(_ todo: TodoIssue, to area: TodoArea) async {
        guard let before = todoIssues, let index = before.items.firstIndex(where: { $0.number == todo.number }) else { return }
        var moved = before
        moved.items[index].areas = [area.rawValue]
        moved.items[index].labels.removeAll { label in TodoArea.allCases.contains { $0.rawValue == label.name } }
        moved.items[index].labels.append(GitHubLabel(name: area.rawValue))
        todoIssues = moved
        if isDemo { return }
        do {
            let confirmed: TodoIssue = try await businessRequest("todos", method: "PATCH",
                body: APIJSON.encode(TodoMoveRequest(number: todo.number, area: area.rawValue)), requestId: UUID().uuidString)
            var list = todoIssues ?? moved
            if let row = list.items.firstIndex(where: { $0.number == confirmed.number }) { list.items[row] = confirmed }
            receiveTodoIssues(list)
            businessChanges += 1
        } catch {
            if todoIssues == moved { todoIssues = before }
            if !(error is CancellationError) { self.error = error.localizedDescription }
        }
    }
}

/// The rows of one list: the to-dos, high priority first. A row opens its issue in
/// Coconut (GitHubItemView). Hold it to move it to another list, which changes its label on GitHub. On Product the rows
/// are rows of its List; on Finance and Support they are rows of a section (TodoListSection).
struct TodoIssueRows: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ScaledMetric(relativeTo: .title3) private var iconWidth = 24
    let area: TodoArea
    var inList = true

    var body: some View {
        // Finance lists each risk review past SLA, when the Mac has the list, in place of the row that counts them.
        let reviews = area == .finance ? store.shownRiskReviews : nil
        let hidden = TodoCounts.reviewIssues(reviews)
        let todos = (store.todoIssues?.items(in: area) ?? []).filter { !hidden.contains($0.number) }
        let repository = store.todoIssues?.repository ?? "antiwork/gumroad-private"
        Group {
            if let reviews {
                ForEach(reviews) { review in
                    row(symbol: review.symbol, tint: review.tint, title: review.title, detail: review.detail()) {
                        pushRoute?(RiskReviewRoute(user: review.user))
                    }
                    .accessibilityIdentifier("risk-review-\(review.user)")
                    .modifier(TodoRowInset(inList: inList))
                }
            } else {
                ForEach(TodoCounts.breaches(area, store.attentionItems())) { TodoBreachRow(item: $0, inList: inList) }
            }
            ForEach(todos) { todo in
                row(symbol: todo.symbol, tint: todo.tint, title: todo.title, detail: todo.detail()) {
                    pushRoute?(todo.route(in: repository))
                }
                .contextMenu {
                    ForEach(TodoArea.allCases.filter { todo.areas != [$0.rawValue] }, id: \.self) { other in
                        Button("Move to \(other.title)", systemImage: other.symbol) { Task { await store.moveTodoIssue(todo, to: other) } }
                    }
                }
                .accessibilityIdentifier("todo-\(todo.number)")
                .modifier(TodoRowInset(inList: inList))
            }
            if todos.isEmpty && reviews == nil {
                Group {
                    if store.todoIssues == nil, let message = store.todoIssuesError {
                        BoardLoadNotice(title: "To-dos unavailable", message: message) { Task { await store.loadTodoIssues(fresh: true) } }
                            .padding(inList ? 0 : BoardStyle.Space.m)
                    } else {
                        // No list has a + (the person removed them), so an empty list names none.
                        Text(store.todoIssues == nil ? "Loading to-dos" : "No to-dos.")
                            .font(.subheadline).foregroundStyle(.secondary)
                            .padding(.horizontal, inList ? 0 : BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.m)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .accessibilityIdentifier("todos-empty-\(area.rawValue)")
                .modifier(TodoRowInset(inList: inList))
            }
        }
    }

    private func row(symbol: String, tint: Color, title: String, detail: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: symbol).font(.title3).foregroundStyle(tint).frame(width: iconWidth).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(title).font(.body).foregroundStyle(.primary).lineLimit(dynamicTypeSize.isAccessibilitySize ? nil : 2)
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, inList ? BoardStyle.Space.m : BoardStyle.Space.s)
            .padding(.horizontal, inList ? 0 : BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(inList ? BoardStyle.paper : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
    }
}

/// A breach of the business that counts on a list (TodoCounts): the conversations of Support or the risk reviews of
/// Finance past their service level. A tap opens what breaches: the open conversations of Support, or the report of
/// Risk, because the dashboard of the tab shows a tile of the queue and no longer its rows (DashboardPage).
private struct TodoBreachRow: View {
    let item: AttentionItem
    let inList: Bool
    @Environment(\.pushRoute) private var pushRoute
    @ScaledMetric(relativeTo: .title3) private var iconWidth = 24

    var body: some View {
        Button { item.businessRoute.map { pushRoute?($0) } } label: {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: item.symbol).font(.title3).foregroundStyle(BoardStyle.destructive).frame(width: iconWidth)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                    Text(item.label).font(.body.weight(.semibold)).foregroundStyle(BoardStyle.destructive)
                    if !item.detail.isEmpty { Text(item.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1) }
                }
                Spacer(minLength: 0)
                Text(item.value).font(.title3.weight(.semibold).monospacedDigit()).foregroundStyle(BoardStyle.destructive)
            }
            .frame(minHeight: 44, alignment: .leading)
            .padding(.vertical, inList ? BoardStyle.Space.m : BoardStyle.Space.s)
            .padding(.horizontal, inList ? 0 : BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(inList ? BoardStyle.paper : .clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("todo-breach-\(item.id)")
        .modifier(TodoRowInset(inList: inList))
    }
}

extension AttentionItem {
    /// The page of the business that a breach opens: the conversations of Support, or the report of Risk.
    var businessRoute: BusinessRoute? {
        guard case .business(let route) = target else { return nil }
        return route
    }
}

/// A row of the list of Product has the side space of its List. In a section the section gives it.
private struct TodoRowInset: ViewModifier {
    let inList: Bool
    func body(content: Content) -> some View {
        if inList { content.homeRow() } else { content }
    }
}

/// The list of one tab on a page of tiles: Finance and Support, with the breach of the tab as the first row. It has no
/// title and no count: its rows say what they are, and the badge of the tab counts them (TodoCounts). The person: "Don't
/// have to say todos in the ui the table itself is self descriptive". Product is a List and shows TodoIssueRows itself.
/// The dashboards of Finance and Support show it as a tile of the rows that fit (TodoTile), and its last line opens
/// this list on a page of its own (TodoListPage).
struct TodoListSection: View {
    @Environment(AppStore.self) private var store
    let area: TodoArea

    var body: some View {
        BoardSection {
            TodoIssueRows(area: area, inList: false)
        }
        .accessibilityIdentifier("todos-\(area.rawValue)")
        // The root of the iPhone loads the list for the counts of the tabs (CoconutHome). The Mac has no counts, so its
        // pages load it here, and Finance its risk reviews.
        .task(id: "\(store.connection)/\(store.businessChanges)") {
            await store.loadTodoIssues()
            if area == .finance { await store.loadRiskReviews() }
        }
    }
}

/// Every to-do of a tab, which the last line of its tile opens (TodoTile). Like the list, the page has no title: its
/// rows say what they are, and Back leads to the dashboard.
struct TodoListPage: View {
    @Environment(AppStore.self) private var store
    let area: TodoArea

    var body: some View {
        BoardPage { TodoListSection(area: area) }
            .refreshable {
                await store.loadTodoIssues(fresh: true)
                if area == .finance { await store.loadRiskReviews(fresh: true) }
            }
    }
}

/// One row of a tile of to-dos (TodoTile): a risk review of Finance, the breach of a tab, or a to-do.
struct TodoTileRow: Identifiable {
    let id: String
    let symbol: String
    let tint: Color
    let title: String
    let detail: String
    /// The count of a breach, which the row shows in its color.
    var value: String? = nil
    /// The page that a tap opens: the issue, the review, or the queue of the breach.
    let route: any Hashable
    /// The to-do of the row, which its menu moves to another list.
    var todo: TodoIssue? = nil
    var identifier: String
}

extension AppStore {
    /// The rows of a list in the order of its page (TodoIssueRows): the risk reviews of Finance, or the breach of the
    /// tab, then the to-dos. A breach opens what breaches: the conversations of Support, or the report of Risk.
    func todoTileRows(_ area: TodoArea) -> [TodoTileRow] {
        let reviews = area == .finance ? shownRiskReviews : nil
        let hidden = TodoCounts.reviewIssues(reviews)
        let repository = todoIssues?.repository ?? "antiwork/gumroad-private"
        var rows: [TodoTileRow]
        if let reviews {
            rows = reviews.map { review in
                TodoTileRow(id: "review-" + review.user, symbol: review.symbol, tint: review.tint, title: review.title,
                            detail: review.detail(), route: RiskReviewRoute(user: review.user),
                            identifier: "risk-review-\(review.user)")
            }
        } else {
            rows = TodoCounts.breaches(area, attentionItems()).map { item in
                TodoTileRow(id: "breach-" + item.id, symbol: item.symbol, tint: BoardStyle.destructive, title: item.label,
                            detail: item.detail, value: item.value, route: item.businessRoute ?? BusinessRoute.home,
                            identifier: "todo-breach-\(item.id)")
            }
        }
        rows += (todoIssues?.items(in: area) ?? []).filter { !hidden.contains($0.number) }.map { todo in
            TodoTileRow(id: "todo-\(todo.number)", symbol: todo.symbol, tint: todo.tint, title: todo.title,
                        detail: todo.detail(), route: todo.route(in: repository), todo: todo, identifier: "todo-\(todo.number)")
        }
        return rows
    }
}

/// The to-dos of a tab on its dashboard of one page (DashboardPage): the rows that fit the tile, in the order of the
/// list, and a last line with the number of the others, which opens the whole list (TodoListPage). A row opens its
/// issue, its review or the queue of its breach, and a hold moves a to-do to another list, as in the list. Like the
/// list, the tile has no title: its rows say what they are, and the badge of the tab counts them.
struct TodoTile: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.bentoCompact) private var compact
    @ScaledMetric(relativeTo: .subheadline) private var rowHeight: CGFloat = 52
    @ScaledMetric(relativeTo: .subheadline) private var moreHeight: CGFloat = 38
    @ScaledMetric(relativeTo: .body) private var iconWidth: CGFloat = 22
    let area: TodoArea

    private var inset: CGFloat { compact ? BoardStyle.Space.m : BoardStyle.Space.l }

    var body: some View {
        let rows = store.todoTileRows(area)
        BentoTile(padding: 0) {
            GeometryReader { proxy in
                let shown = Self.shown(rows.count, height: proxy.size.height, row: rowHeight, more: moreHeight)
                // The rows of a cut list share the height above its last line, so the tile ends on that line.
                let height = shown > 0 && shown < rows.count ? (proxy.size.height - moreHeight) / CGFloat(shown) : rowHeight
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(rows.prefix(shown).enumerated()), id: \.element.id) { index, row in
                        line(row, height: height).overlay(alignment: .top) {
                            if index > 0 { Divider().padding(.leading, inset + iconWidth + BoardStyle.Space.m) }
                        }
                    }
                    if shown < rows.count { more(rows.count - shown) }
                    if rows.isEmpty { empty }
                }
            }
        }
        // A container of its own, so each row keeps its identifier under the identifier of the tile.
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("todos-\(area.rawValue)")
        // The root of the iPhone loads the list for the counts of the tabs (CoconutHome). The Mac has no counts, so its
        // dashboards load it here, and Finance its risk reviews.
        .task(id: "\(store.connection)/\(store.businessChanges)") {
            await store.loadTodoIssues()
            if area == .finance { await store.loadRiskReviews() }
        }
    }

    /// How many rows fit a height: all of them, or as many as leave room for the line that opens the rest.
    static func shown(_ count: Int, height: CGFloat, row: CGFloat, more: CGFloat) -> Int {
        guard row > 0, height > 0 else { return 0 }
        if CGFloat(count) * row <= height { return count }
        return max(0, min(count, Int((height - more) / row)))
    }

    private func line(_ row: TodoTileRow, height: CGFloat) -> some View {
        Button { pushRoute?(row.route) } label: {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: row.symbol).font(.body).foregroundStyle(row.tint).frame(width: iconWidth)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(row.title).font(.subheadline.weight(row.value == nil ? .regular : .semibold))
                        .foregroundStyle(row.value == nil ? AnyShapeStyle(.primary) : AnyShapeStyle(row.tint))
                    if !row.detail.isEmpty { Text(row.detail).font(.caption).foregroundStyle(.secondary) }
                }
                .lineLimit(1)
                Spacer(minLength: 0)
                if let value = row.value {
                    Text(value).font(.headline.monospacedDigit()).foregroundStyle(row.tint)
                }
            }
            .padding(.horizontal, inset)
            .frame(maxWidth: .infinity, minHeight: height, maxHeight: height, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .contextMenu {
            if let todo = row.todo {
                ForEach(TodoArea.allCases.filter { todo.areas != [$0.rawValue] }, id: \.self) { other in
                    Button("Move to \(other.title)", systemImage: other.symbol) { Task { await store.moveTodoIssue(todo, to: other) } }
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(row.identifier)
    }

    /// The line under the rows that fit: how many others the list has. It opens the whole list.
    private func more(_ count: Int) -> some View {
        Button { pushRoute?(BusinessRoute.todos(area)) } label: {
            HStack(spacing: BoardStyle.Space.xs) {
                Text("\(count) more").font(.subheadline.weight(.medium)).foregroundStyle(BoardStyle.control)
                Spacer(minLength: 0)
                BentoChevron()
            }
            .padding(.horizontal, inset)
            .frame(maxWidth: .infinity, minHeight: moreHeight, maxHeight: moreHeight, alignment: .leading)
            .overlay(alignment: .top) { Divider().padding(.leading, inset) }
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityIdentifier("todos-more-\(area.rawValue)")
    }

    /// The tile of a list without rows: its load failed, it loads, or it is empty.
    @ViewBuilder private var empty: some View {
        Group {
            if store.todoIssues == nil, let message = store.todoIssuesError {
                BoardLoadNotice(title: "To-dos unavailable", message: message) { Task { await store.loadTodoIssues(fresh: true) } }
            } else {
                // No list has a + (the person removed them), so an empty list names none.
                Text(store.todoIssues == nil ? "Loading to-dos" : "No to-dos.").font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .padding(inset)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityIdentifier("todos-empty-\(area.rawValue)")
    }
}

extension TodoIssues {
    /// The to-dos of the demo: Gumroad work of the three lists, made up for screenshots and never copied from the
    /// private repository.
    static var preview: TodoIssues {
        let now = Date()
        func ago(_ hours: Double) -> String { now.addingTimeInterval(-hours * 3600).formatted(.iso8601) }
        func todo(_ number: Int, _ title: String, _ area: TodoArea, _ labels: [String], assignee: String?, hours: Double, mine: Bool = false) -> TodoIssue {
            let colors = ["working": "0e8a16", "awaiting-human": "d93f0b", "priority/high": "d93f0b", "priority/medium": "fbca04", "bug": "d73a4a",
                          "enhancement": "a2eeef", "privacy": "5319e7", "finance": "1a7f37", "support": "0969da", "product": "8250df"]
            let names = labels + [area.rawValue]
            return TodoIssue(number: number, title: title, areas: [area.rawValue], labels: names.map { GitHubLabel(name: $0, color: colors[$0] ?? "fbca04") },
                             assignees: assignee.map { [$0] } ?? [], author: "gumclaw", comments: 2, createdAt: ago(hours), updatedAt: ago(hours / 3),
                             url: "https://github.com/example/gumroad/issues/\(number)", mine: mine)
        }
        return TodoIssues(repository: "example/gumroad", login: "slavingia", items: [
            todo(4796, "Decide: release or hold a $412.50 balance whose only sale looks like a resale", .finance, ["awaiting-human"], assignee: "gianfrancopiana", hours: 5),
            todo(4794, "Payout export misses refunds made after the payout date", .product, ["bug", "priority/high"], assignee: "gumclaw", hours: 9),
            todo(4792, "Seller asks for a countersigned data processing agreement", .support, ["privacy", "awaiting-human"], assignee: "slavingia", hours: 12, mine: true),
            todo(4789, "Arizona TPT: confirm the marketplace-facilitator filing for Q4", .finance, ["priority/medium"], assignee: "gianfrancopiana", hours: 26),
            todo(4787, "Files removed from 3 listings: 212 paid purchases can't download", .support, ["working", "priority/medium"], assignee: "gumclaw", hours: 30),
            todo(4785, "Email composer keeps Publish for a customers-only audience", .product, ["bug", "awaiting-human"], assignee: "gianfrancopiana", hours: 40),
            todo(4781, "Stripe asks for a PCI attestation before the Visa deadline", .finance, ["awaiting-human"], assignee: "slavingia", hours: 52, mine: true),
            todo(4778, "Seller stuck on a Stripe document requirement for a sole proprietorship", .support, ["awaiting-human"], assignee: "gianfrancopiana", hours: 70),
            todo(4774, "Add a 24-hour clock to the date and time pickers", .product, ["enhancement"], assignee: nil, hours: 96),
            todo(4770, "Reconcile the October Stripe payouts against the ledger", .finance, ["working"], assignee: "gumclaw", hours: 120),
        ])
    }
}
