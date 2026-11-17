import SwiftUI

/// The colors of GitHub states in the palette of Coconut.
enum GitHubTint {
    /// Merged, and an issue that is done.
    static let merged = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.76, green: 0.62, blue: 0.86, alpha: 1) : PlatformColor(red: 0.47, green: 0.31, blue: 0.63, alpha: 1)
    }
    /// Checks that still run.
    static let pending = Color.adaptive { dark, _ in
        dark ? PlatformColor(red: 0.92, green: 0.66, blue: 0.36, alpha: 1) : PlatformColor(red: 0.66, green: 0.42, blue: 0.08, alpha: 1)
    }

    static func state(_ preview: GitHubPreview) -> Color {
        switch (preview.kind, preview.state) {
        case (.pull, "open"), (.issue, "open"): BoardStyle.positive
        case (.pull, "merged"), (.issue, "closed"): merged
        case (.pull, "closed"): BoardStyle.destructive
        default: .secondary
        }
    }

    static func checks(_ state: String) -> Color {
        switch state {
        case "passing": BoardStyle.positive
        case "failing": BoardStyle.destructive
        default: pending
        }
    }

    static func review(_ review: String) -> Color {
        switch review {
        case "approved": BoardStyle.positive
        case "changes_requested": BoardStyle.destructive
        default: .secondary
        }
    }

    /// The dot of a language, in the color that GitHub gives it.
    static func language(_ name: String) -> Color {
        let colors = ["Ruby": "701516", "TypeScript": "3178C6", "JavaScript": "E3C93F", "Python": "3572A5", "Swift": "F05138",
                      "Go": "00ADD8", "Rust": "DEA584", "Java": "B07219", "Kotlin": "A97BFF", "C": "555555", "C++": "F34B7D",
                      "C#": "178600", "PHP": "4F5D95", "HTML": "E34C26", "CSS": "663399", "Shell": "89E051", "Elixir": "6E4A7E"]
        return colors[name].map { Color(hex: $0) } ?? .secondary
    }
}

/// The symbol of a card: the kind and the state.
enum GitHubSymbol {
    static func name(_ preview: GitHubPreview) -> String {
        switch (preview.kind, preview.state) {
        case (.pull, "merged"): "arrow.triangle.merge"
        case (.pull, _): "arrow.triangle.pull"
        case (.issue, "closed"): "checkmark.circle"
        case (.issue, "not_planned"): "slash.circle"
        case (.issue, _): "smallcircle.filled.circle"
        case (.commit, _): "circle.and.line.horizontal"
        case (.repository, _): preview.private == true ? "lock" : "book.closed"
        case (.file, _): "chevron.left.forwardslash.chevron.right"
        case (.comment, _): "text.bubble"
        }
    }
}

/// The cards of the GitHub addresses of a text, one under the other. It has no container: without a loaded card it
/// adds nothing to its stack, so a message without a card keeps its size. `GitHubEmbedLoading` loads the cards.
struct GitHubEmbedStack: View {
    @Environment(AppStore.self) private var store
    let text: String
    /// Inside a bubble the card takes the line fill, which reads on each bubble.
    var inBubble = false

    var body: some View {
        ForEach(store.githubEmbeds.cards(in: text), id: \.url) { preview in
            GitHubEmbedCard(preview: preview, inBubble: inBubble)
        }
    }
}

/// Loads the GitHub cards of a text when the view shows. A view that always shows carries it, because the stack of
/// the cards is empty before they load.
struct GitHubEmbedLoading: ViewModifier {
    @Environment(AppStore.self) private var store
    let text: String

    func body(content: Content) -> some View {
        content.task(id: text) { await store.githubEmbeds.load(text: text) }
    }
}

extension View {
    func loadsGitHubEmbeds(in text: String) -> some View { modifier(GitHubEmbedLoading(text: text)) }
}

/// One GitHub page as a compact card. A tap opens the address. The menu copies it or opens it.
struct GitHubEmbedCard: View {
    #if os(macOS)
    static let maxWidth: CGFloat = 440
    #else
    static let maxWidth: CGFloat = 340
    #endif
    @Environment(\.openURL) private var openURL
    @Environment(\.dynamicTypeSize) private var typeSize
    @ScaledMetric(relativeTo: .subheadline) private var symbolWidth = 18
    let preview: GitHubPreview
    var inBubble = false

    var body: some View {
        Button { if let link = preview.link { openURL(link) } } label: { label }
            .buttonStyle(.plain)
            .contextMenu {
                Button("Copy link", systemImage: "link") { Pasteboard.copy(preview.url) }
                Button("Open in browser", systemImage: "safari") { if let link = preview.link { openURL(link) } }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(preview.spoken)
            .accessibilityHint("Opens the page on GitHub")
            .accessibilityAddTraits(.isLink)
            .accessibilityIdentifier("github-embed-\(preview.kind.rawValue)")
    }

    private var label: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: GitHubSymbol.name(preview))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(GitHubTint.state(preview))
                .frame(width: symbolWidth)
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                header
                content
                GitHubFlow(spacing: BoardStyle.Space.s, lineSpacing: BoardStyle.Space.xs) { details }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                if let snippet = GitHubSnippet(preview) {
                    GitHubSnippetView(snippet: snippet, inBubble: inBubble).padding(.top, BoardStyle.Space.line)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, 10)
        .frame(maxWidth: Self.maxWidth, alignment: .leading)
        .background(inBubble ? BoardStyle.line : BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        .contentShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
    }

    /// The repository with the number or the commit, and the age at the end of the line. A large text size puts the
    /// age with the details, so the name keeps the line.
    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.xs) {
            if preview.kind != .repository {
                Text(preview.repository).lineLimit(1).truncationMode(.middle)
                if let reference = preview.reference {
                    Text(reference).font(preview.number == nil ? .footnote.monospaced() : .footnote.monospacedDigit())
                        .layoutPriority(1)
                }
                if preview.kind == .file, let ref = preview.ref {
                    Text(ref).font(.footnote.monospaced()).lineLimit(1).layoutPriority(1)
                }
            } else {
                Text(preview.repository).font(.subheadline.weight(.semibold)).foregroundStyle(.primary).lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: BoardStyle.Space.s)
            if !typeSize.isAccessibilitySize, let age {
                Text(age).font(.footnote.monospacedDigit()).foregroundStyle(.tertiary).layoutPriority(2)
            }
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
    }

    private var age: String? { GitHubPreview.age(of: preview.updatedAt) }

    @ViewBuilder private var content: some View {
        switch preview.kind {
        case .pull, .issue, .commit:
            if let title = preview.title, !title.isEmpty {
                Text(title).font(.subheadline.weight(.semibold)).lineLimit(2).multilineTextAlignment(.leading)
            }
        case .repository:
            if let description = preview.description, !description.isEmpty {
                Text(description).font(.subheadline).lineLimit(2).multilineTextAlignment(.leading)
            }
        case .file:
            if let path = preview.path {
                Text(path).font(.footnote.monospaced().weight(.semibold)).lineLimit(1).truncationMode(.head)
            }
        case .comment:
            if let excerpt = preview.excerpt, !excerpt.isEmpty {
                Text(excerpt).font(.subheadline).lineLimit(3).multilineTextAlignment(.leading)
            }
        }
    }

    /// The small facts under the title. They wrap to a second line when the card is narrow or the text is large.
    @ViewBuilder private var details: some View {
        if let author = preview.author, preview.kind != .repository {
            GitHubPersonLabel(person: author, suffix: preview.kind == .comment ? "commented" : nil)
        }
        if let label = preview.stateLabel {
            let tint = GitHubTint.state(preview)
            Text(label).font(.caption.weight(.semibold)).foregroundStyle(tint)
                .padding(.horizontal, 6).padding(.vertical, 1)
                .background(tint.opacity(0.14), in: Capsule())
        }
        if preview.additions != nil || preview.deletions != nil {
            HStack(spacing: BoardStyle.Space.xs) {
                if let additions = preview.additions { Text("+\(additions)").foregroundStyle(BoardStyle.positive) }
                if let deletions = preview.deletions { Text("−\(deletions)").foregroundStyle(BoardStyle.destructive) }
            }
            .font(.footnote.monospacedDigit().weight(.medium))
        }
        if let files = preview.changedFiles {
            Text(files == 1 ? "1 file" : "\(files) files").monospacedDigit()
        }
        if let checks = preview.checks {
            let symbol = checks.state == "passing" ? "checkmark.circle.fill" : checks.state == "failing" ? "xmark.circle.fill" : "clock.fill"
            GitHubFact(symbol: symbol, text: checks.label, tint: GitHubTint.checks(checks.state))
        }
        if let review = preview.review, let label = GitHubPreview.reviewLabel(review) {
            let symbol = review == "approved" ? "checkmark" : review == "changes_requested" ? "arrow.uturn.backward" : "eye"
            GitHubFact(symbol: symbol, text: label, tint: GitHubTint.review(review))
        }
        ForEach(preview.labels ?? [], id: \.self) { GitHubLabelChip(label: $0) }
        if let more = preview.moreLabels, more > 0 { Text("+\(more)").monospacedDigit() }
        if let assignees = preview.assignees, !assignees.isEmpty { GitHubFaces(people: assignees) }
        if let comments = preview.comments, comments > 0, preview.kind != .comment {
            GitHubFact(symbol: "bubble.left", text: GitHubPreview.compact(comments))
        }
        if let language = preview.language {
            HStack(spacing: BoardStyle.Space.xs) {
                Circle().fill(GitHubTint.language(language)).frame(width: 8, height: 8)
                Text(language)
            }
        }
        if let stars = preview.stars { GitHubFact(symbol: "star", text: GitHubPreview.compact(stars)) }
        if let forks = preview.forks, forks > 0 { GitHubFact(symbol: "arrow.triangle.branch", text: GitHubPreview.compact(forks)) }
        if preview.archived == true { Text("Archived") }
        if preview.kind == .file, let range = preview.lineRange { Text(range).monospacedDigit() }
        if preview.kind == .comment {
            if let path = preview.path {
                Text(path).font(.caption.monospaced()).lineLimit(1).truncationMode(.head)
            } else if let title = preview.title, !title.isEmpty {
                Text(title).lineLimit(1)
            }
        }
        if typeSize.isAccessibilitySize, let age { Text(age).monospacedDigit() }
    }
}

/// A symbol and a short text, as one fact of a card.
private struct GitHubFact: View {
    let symbol: String
    let text: String
    var tint: Color? = nil

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: symbol).imageScale(.small).foregroundStyle(tint ?? .secondary)
            Text(text).monospacedDigit()
        }
    }
}

/// The avatar and the login of a person.
private struct GitHubPersonLabel: View {
    let person: GitHubPerson
    var suffix: String? = nil

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            GitHubAvatar(person: person)
            Text(suffix.map { "\(person.login) \($0)" } ?? person.login).lineLimit(1)
        }
    }
}

/// The avatars of the assignees, each over the edge of the one before. At most three show.
private struct GitHubFaces: View {
    let people: [GitHubPerson]

    var body: some View {
        HStack(spacing: -4) {
            ForEach(people.prefix(3), id: \.self) { person in
                GitHubAvatar(person: person).overlay { Circle().stroke(BoardStyle.surface, lineWidth: 1) }
            }
            if people.count > 3 {
                Text("+\(people.count - 3)").monospacedDigit().padding(.leading, BoardStyle.Space.s)
            }
        }
        .accessibilityLabel("Assigned to \(people.map(\.login).joined(separator: ", "))")
    }
}

/// The avatar of a GitHub account, in the size of the text. Before it loads, and for an account without one, the
/// first letter of the login shows on a fill of Coconut.
struct GitHubAvatar: View {
    @ScaledMetric(relativeTo: .footnote) private var size = 16
    let person: GitHubPerson
    @State private var image: PlatformImage?

    init(person: GitHubPerson) {
        self.person = person
        _image = State(initialValue: person.avatar.flatMap { PhotoLoader.shared.cached($0, size: .tile) })
    }

    var body: some View {
        Group {
            if let image {
                Image(platformImage: image).resizable().scaledToFill().clipShape(Circle())
            } else {
                LetterFaceView(letters: LetterFace.letters(person.login, count: 1), id: person.login, symbol: "person.fill", size: size)
            }
        }
        .frame(width: size, height: size)
        .task(id: person.avatar) {
            guard image == nil, let url = person.avatar else { return }
            image = await PhotoLoader.shared.image(url, size: .tile)
        }
        .accessibilityHidden(true)
    }
}

/// A label of an issue: a dot in the color of the label and its name on a light fill of that color.
private struct GitHubLabelChip: View {
    let label: GitHubLabel

    var body: some View {
        let color = label.color.map { Color(hex: $0) } ?? Color.secondary
        HStack(spacing: BoardStyle.Space.xs) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label.name).font(.caption.weight(.medium)).foregroundStyle(.primary).lineLimit(1)
        }
        .padding(.horizontal, 7).padding(.vertical, 1)
        .background(color.opacity(0.16), in: Capsule())
    }
}

/// The lines of a file with their numbers, in a monospaced font. A long line is cut at the edge of the card.
private struct GitHubSnippetView: View {
    let snippet: GitHubSnippet
    let inBubble: Bool

    var body: some View {
        let widest = String(snippet.rows.last?.number ?? 0)
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            ForEach(snippet.rows) { row in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    // Each number takes the width of the widest one, at each text size.
                    Text(widest).hidden()
                        .overlay(alignment: .trailing) { Text(String(row.number)).foregroundStyle(.tertiary) }
                    Text(row.text.isEmpty ? " " : row.text).lineLimit(1).truncationMode(.tail)
                }
            }
            if snippet.more > 0 {
                Text(snippet.more == 1 ? "1 more line" : "\(snippet.more) more lines")
                    .font(.caption).foregroundStyle(.secondary).padding(.top, BoardStyle.Space.line)
            }
        }
        .font(.caption.monospaced())
        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(inBubble ? BoardStyle.line : BoardStyle.paper, in: RoundedRectangle(cornerRadius: 8))
    }
}

/// Puts its parts on one line and starts a new line when the next part does not fit. A part wider than the line is
/// cut to the line.
struct GitHubFlow: Layout {
    var spacing: CGFloat
    var lineSpacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = rows(proposal.width, subviews)
        return CGSize(width: rows.map(\.width).max() ?? 0,
                      height: rows.reduce(0) { $0 + $1.height } + lineSpacing * CGFloat(max(rows.count - 1, 0)))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in rows(bounds.width, subviews) {
            var x = bounds.minX
            for (index, size) in row.items {
                subviews[index].place(at: CGPoint(x: x, y: y + row.height / 2), anchor: .leading, proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + lineSpacing
        }
    }

    private struct Row {
        var items: [(index: Int, size: CGSize)] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func rows(_ width: CGFloat?, _ subviews: Subviews) -> [Row] {
        let limit = width ?? .infinity
        var rows: [Row] = []
        var row = Row()
        for index in subviews.indices {
            var size = subviews[index].sizeThatFits(.unspecified)
            if size.width > limit { size = subviews[index].sizeThatFits(ProposedViewSize(width: limit, height: nil)) }
            if !row.items.isEmpty, row.width + spacing + size.width > limit {
                rows.append(row)
                row = Row()
            }
            row.width += (row.items.isEmpty ? 0 : spacing) + size.width
            row.height = max(row.height, size.height)
            row.items.append((index, size))
        }
        if !row.items.isEmpty { rows.append(row) }
        return rows
    }
}
