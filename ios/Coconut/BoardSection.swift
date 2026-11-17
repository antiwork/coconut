import SwiftUI

/// A page of sections: Home, the agents, an agent, People and Profile. The content keeps a readable width in a wide
/// Mac window and the side space of the app on an iPhone.
struct BoardPage<Content: View>: View {
    /// How wide a page reads: the sections of the app, or the tile grids of the business.
    enum Width { case readable, wide }

    var width: Width = .readable
    var spacing: CGFloat = BoardStyle.Space.xl
    @ViewBuilder let content: () -> Content

    /// A screenshot of the lower part of a page starts the app with `--bottom`. Only a debug build on a simulator
    /// reads it.
    private static var startsAtBottom: Bool {
        #if DEBUG && targetEnvironment(simulator)
        return ProcessInfo.processInfo.arguments.contains("--bottom")
        #else
        return false
        #endif
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: spacing) { content() }
                    .padding(BoardStyle.Space.l)
                    .frame(maxWidth: width == .readable ? 760 : 1160, alignment: .leading)
                    .frame(maxWidth: .infinity)
                    .id(Self.contentID)
            }
            .defaultScrollAnchor(Self.startsAtBottom ? .bottom : .top)
            .background(BoardStyle.paper)
            // A page that loads after it shows grows under the first anchor, so `--bottom` scrolls to the end again.
            .task {
                guard Self.startsAtBottom else { return }
                for wait in [1.0, 2, 3] {
                    try? await Task.sleep(for: .seconds(wait))
                    proxy.scrollTo(Self.contentID, anchor: .bottom)
                }
            }
        }
    }

    private static var contentID: String { "board-page-content" }
}

/// The title of a page of sections with one quiet line under it.
struct BoardPageTitle: View {
    let title: String
    var detail: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(title).font(.title2.weight(.bold))
            if let detail { Text(detail).font(.subheadline).foregroundStyle(.secondary) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// A titled group of rows on a surface: the title, the count of its rows, an optional action at the end of the title
/// line, and the rows with a line between them.
struct BoardSection<Rows: View, Action: View>: View {
    let title: String
    var count = 0
    @ViewBuilder let action: () -> Action
    @ViewBuilder let rows: () -> Rows

    init(_ title: String, count: Int = 0, @ViewBuilder action: @escaping () -> Action,
         @ViewBuilder rows: @escaping () -> Rows) {
        self.title = title
        self.count = count
        self.action = action
        self.rows = rows
    }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(spacing: BoardStyle.Space.s) {
                HStack(spacing: BoardStyle.Space.s) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                    if count > 0 { CountPill(count: count) }
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                action().font(.subheadline).buttonStyle(.plain).foregroundStyle(.secondary)
            }
            // The rows have a line between them. The section draws it, so a row knows nothing of its place.
            Group(subviews: rows()) { subviews in
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(subviews) { subview in
                        subview
                        if subview.id != subviews.last?.id { Divider().padding(.leading, BoardStyle.Space.m) }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        }
    }
}

extension BoardSection where Action == EmptyView {
    init(_ title: String, count: Int = 0, @ViewBuilder rows: @escaping () -> Rows) {
        self.init(title, count: count, action: { EmptyView() }, rows: rows)
    }
}

/// A whole page that is loading: one spinner, one height, wherever it is.
struct BoardLoading: View {
    let title: String
    var height: CGFloat = 320

    var body: some View {
        ProgressView(title).frame(maxWidth: .infinity, minHeight: height)
    }
}

/// A whole page that failed to load: what failed, why, and the one way to try again.
struct BoardLoadNotice: View {
    var title = "Refresh unavailable"
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Label(title, systemImage: "exclamationmark.arrow.trianglehead.2.clockwise.rotate.90")
            Text(message).font(.caption).foregroundStyle(.secondary)
            Button("Try again", action: retry)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A quiet count next to a title.
struct CountPill: View {
    let count: Int

    var body: some View {
        Text("\(count)").font(.caption.weight(.semibold).monospacedDigit()).foregroundStyle(.secondary)
            .padding(.horizontal, BoardStyle.Space.s).frame(minWidth: 18, minHeight: 18)
            .background(Color.primary.opacity(0.08), in: Capsule())
    }
}

/// One line of a section: a symbol, a title with an optional line under it, a quiet value at the end, and a chevron
/// when the row opens something.
struct BoardRow: View {
    var symbol: String? = nil
    var tint: Color = .secondary
    let title: String
    var detail: String? = nil
    /// The lines that the detail can take. More than one is for a detail of numbers, where a cut hides a number.
    var detailLines = 1
    var value: String? = nil
    var chevron = true
    /// The row opens a menu of choices for its value.
    var menu = false

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            if let symbol {
                Image(systemName: symbol).font(.callout).foregroundStyle(tint).frame(width: 20)
            }
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(title).foregroundStyle(.primary).lineLimit(1)
                if let detail, !detail.isEmpty {
                    Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(detailLines)
                }
            }
            Spacer(minLength: BoardStyle.Space.s)
            if let value {
                Text(value).font(.subheadline.monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
            }
            if chevron {
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            } else if menu {
                Image(systemName: "chevron.up.chevron.down").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.m)
        .frame(minHeight: 40)
        .contentShape(Rectangle())
    }
}

/// The one quiet line of a section without rows.
struct BoardEmptyRow: View {
    let text: String

    var body: some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary)
            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension View {
    /// A row of a section that opens something on a tap.
    func boardButton(_ identifier: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { self }
            .buttonStyle(HomePressStyle())
            .accessibilityIdentifier(identifier)
    }
}
