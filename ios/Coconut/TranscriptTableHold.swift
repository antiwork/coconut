import SwiftUI
#if os(macOS)
import AppKit
#endif

/// The rule of the hold, apart from the table so that tests can check it.
///
/// Before a change of the window, the hold notes the rows on screen and where each one starts. After the change, the
/// first of those rows (the middle of the screen first) that is still in the list must start where it started: the correction is its new top minus its
/// old top. Rows that arrive or go above it move it and so get a correction; rows below it, such as a reply or the
/// status row, do not move it. The rule counts no rows, so no mix of changes can mislead it.
enum TranscriptHoldRule {
    /// The correction for the anchors, the preferred one first, with their tops before the change and the tops of
    /// the rows after it. Nil when no anchor is left.
    static func correction(anchors: [(key: String, top: CGFloat)], topsAfter: [String: CGFloat]) -> CGFloat? {
        for anchor in anchors {
            if let top = topsAfter[anchor.key] { return top - anchor.top }
        }
        return nil
    }
}

/// Keeps the rows on screen in place when older rows arrive above them in a Mac chat.
///
/// A Mac list gives a row that it adds above the screen an estimated height of one point, and measures the row only
/// when it scrolls into view. The row then takes its real height and pushes the rows on screen down: a jump of
/// hundreds of points. So the hold measures the new rows at once, while they are still above the screen, and moves
/// the scroll position so that a row on screen keeps its place (TranscriptHoldRule).
@MainActor
final class TranscriptTableHold {
    #if os(macOS)
    fileprivate weak var table: NSTableView?
    /// The table keeps the view of a row on screen while rows arrive or go around it, so a row view names its row.
    /// A view that scrolled off the screen can show another row later; its height tells that apart.
    private final class Anchor {
        weak var view: NSTableRowView?
        let top: CGFloat
        let height: CGFloat
        init(view: NSTableRowView, top: CGFloat, height: CGFloat) { self.view = view; self.top = top; self.height = height }
    }
    private var pending: [Anchor]?
    private var movedAt = Date.distantPast
    private var lastMove: CGFloat?
    /// A row that a table adds above the screen has this height or less until the table measures it.
    private static let estimatedHeight: CGFloat = 2
    #endif

    /// Whether a move of the scroll offset by `change` is the last move of the hold. The scroll observers see that
    /// move a frame later and must not take it for the reader's. Each move of the hold matches once, and only for
    /// 0.15 s, so a scroll of the reader right after it still counts.
    func isOwnMove(_ change: CGFloat) -> Bool {
        #if os(macOS)
        guard let move = lastMove, Date().timeIntervalSince(movedAt) < 0.15, abs(change - move) < 1 else { return false }
        lastMove = nil
        return true
        #else
        return false
        #endif
    }

    /// A growth waits for the list to add its rows.
    var isPending: Bool {
        #if os(macOS)
        pending != nil
        #else
        false
        #endif
    }

    /// Notes the rows on screen before the window adds rows above them. The first row of the list is the header of
    /// the chat, above the place where the window adds rows, so it is never an anchor.
    func prepare() {
        #if os(macOS)
        guard let table, let clip = table.enclosingScrollView?.contentView else { pending = nil; return }
        let visible = table.rows(in: clip.documentVisibleRect)
        let first = max(1, visible.location), end = visible.location + visible.length
        guard first < end else { pending = nil; return }
        // The rows in the middle of the screen first: a scroll of the reader takes them off the screen last.
        let middle = (first + end) / 2
        let rows = (first..<end).sorted { abs($0 - middle) < abs($1 - middle) }
        let anchors = rows.compactMap { row in
            table.rowView(atRow: row, makeIfNecessary: true).map {
                Anchor(view: $0, top: table.rect(ofRow: row).minY, height: table.rect(ofRow: row).height)
            }
        }
        pending = anchors.isEmpty ? nil : anchors
        #endif
    }

    /// After the list changed its rows: measures the rows that arrived above the anchors and moves the scroll position
    /// so that the first anchor that is left keeps its place on screen. Until rows arrive above the anchors or the
    /// anchors move, the hold waits: a reply at the bottom alone changes nothing. True when it moved the scroll position.
    @discardableResult
    func finishIfReady() -> Bool {
        #if os(macOS)
        guard let anchors = pending, let table, let scroll = table.enclosingScrollView else { return false }
        let rows = anchors.map { anchor in
            let row = anchor.view.map { table.row(for: $0) } ?? -1
            return row >= 0 && abs(table.rect(ofRow: row).height - anchor.height) < 0.5 ? row : -1
        }
        let first = rows.filter { $0 >= 0 }.min() ?? table.numberOfRows
        let unmeasured = (1..<max(1, first)).filter { table.rect(ofRow: $0).height <= Self.estimatedHeight }
        let moved = zip(anchors, rows).contains { $1 >= 0 && table.rect(ofRow: $1).minY != $0.top }
        let gone = !rows.contains { $0 >= 0 }
        guard moved || gone || !unmeasured.isEmpty else { return false }
        pending = nil
        // A table measures only the rows on screen, so the hold shows the new rows to the table one screen at a time,
        // in this turn of the main thread: nothing reaches the display.
        let clip = scroll.contentView
        let offset = clip.bounds.minY
        var next = unmeasured.first
        while let row = next {
            clip.scroll(to: NSPoint(x: clip.bounds.minX, y: table.rect(ofRow: row).minY))
            table.needsLayout = true
            table.layoutSubtreeIfNeeded()
            let shown = table.rows(in: clip.documentVisibleRect)
            next = unmeasured.first { $0 >= max(row + 1, shown.location + shown.length) }
        }
        var topsAfter: [String: CGFloat] = [:]
        for (index, row) in rows.enumerated() where row >= 0 { topsAfter[String(index)] = table.rect(ofRow: row).minY }
        guard let move = TranscriptHoldRule.correction(anchors: anchors.enumerated().map { (String($0.offset), $0.element.top) },
                                                       topsAfter: topsAfter) else {
            if !unmeasured.isEmpty { clip.scroll(to: NSPoint(x: clip.bounds.minX, y: offset)) }
            scroll.reflectScrolledClipView(clip)
            return !unmeasured.isEmpty
        }
        // A table keeps its offset when rows arrive, so the offset is still the reader's, with any scroll of the
        // reader since the hold noted the anchors.
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: offset + move))
        scroll.reflectScrolledClipView(clip)
        // The rows on screen get their views now, so the next growth can note them as anchors.
        table.layoutSubtreeIfNeeded()
        movedAt = Date()
        lastMove = move
        return true
        #else
        return false
        #endif
    }
}

/// Finds the table of the list that holds it, for the hold. It sits in a row that the chat always builds.
struct TranscriptTableFinder: View {
    let hold: TranscriptTableHold

    var body: some View {
        #if os(macOS)
        Finder(hold: hold).frame(width: 0, height: 0)
        #else
        EmptyView()
        #endif
    }

    #if os(macOS)
    private struct Finder: NSViewRepresentable {
        let hold: TranscriptTableHold

        func makeNSView(context: Context) -> FinderView { FinderView(hold: hold) }
        func updateNSView(_ view: FinderView, context: Context) { view.hold = hold; view.find() }
    }

    final class FinderView: NSView {
        var hold: TranscriptTableHold

        init(hold: TranscriptTableHold) {
            self.hold = hold
            super.init(frame: .zero)
        }

        required init?(coder: NSCoder) { nil }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            find()
        }

        func find() {
            var view = superview
            while let current = view, !(current is NSTableView) { view = current.superview }
            if let table = view as? NSTableView { hold.table = table }
        }
    }
    #endif
}
