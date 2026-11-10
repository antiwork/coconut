import SwiftUI
#if os(macOS)
import AppKit
#endif

/// The rule of the hold, apart from the table so that tests can check it.
///
/// The window adds its rows at one place: above its oldest row, after the rows that come before the transcript (the
/// header). The hold keeps that oldest row, and so each row below it, where it was on screen.
enum TranscriptHoldRule {
    /// Whether the list shows the rows that the window added: the table has at least that many more rows. A new reply
    /// at the bottom alone is not enough.
    static func hasArrived(rowsBefore: Int, rowsAfter: Int, rowsAbove: Int) -> Bool {
        rowsAfter - rowsBefore >= rowsAbove
    }

    /// How far the scroll position must move so the oldest row of the window before the growth stays where it was on
    /// screen: the change of the height above it. `firstRow` is its position before the growth, `rowsAbove` the rows
    /// that arrived above it, and `heightsAfter` holds the rows after the update, with the new rows measured. Rows
    /// below it, such as a new reply, never count.
    static func correction(heightsBefore: [CGFloat], firstRow: Int, heightsAfter: [CGFloat], rowsAbove: Int) -> CGFloat {
        heightsAfter.prefix(firstRow + rowsAbove).reduce(0, +) - heightsBefore.prefix(firstRow).reduce(0, +)
    }
}

/// Keeps the rows on screen in place when older rows arrive above them in a Mac chat.
///
/// A Mac list gives a row that it adds above the screen an estimated height of one point, and measures the row only
/// when it scrolls into view. The row then takes its real height and pushes the rows on screen down: a jump of
/// hundreds of points. So the hold measures the new rows at once, while they are still above the screen, and moves
/// the scroll position by the height that they add (TranscriptHoldRule).
@MainActor
final class TranscriptTableHold {
    #if os(macOS)
    fileprivate weak var table: NSTableView?
    private var pending: (rows: Int, firstRow: Int, rowsAbove: Int, heights: [CGFloat], at: Date)?
    private var movedAt = Date.distantPast
    private var lastMove: CGFloat?
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

    /// A growth waits for the list to add its rows. After a second the growth of the window goes on without it.
    var isPending: Bool {
        #if os(macOS)
        pending.map { Date().timeIntervalSince($0.at) < 1 } ?? false
        #else
        false
        #endif
    }

    /// Records the list before the window adds `rowsAbove` rows above its oldest row, which is at `firstRow` now.
    /// A growth that adds no row records nothing, so a later change cannot use it.
    func prepare(rowsAbove: Int, firstRow: Int) {
        #if os(macOS)
        guard rowsAbove > 0, let table, firstRow <= table.numberOfRows else { pending = nil; return }
        pending = (table.numberOfRows, firstRow, rowsAbove, (0..<firstRow).map { table.rect(ofRow: $0).height }, Date())
        #endif
    }

    /// After the list changed its rows: measures the rows that arrived above the oldest row of the window and moves
    /// the scroll position by the height that they add. True when it moved the scroll position.
    @discardableResult
    func finishIfReady() -> Bool {
        #if os(macOS)
        guard let pending, let table, let scroll = table.enclosingScrollView else { return false }
        guard TranscriptHoldRule.hasArrived(rowsBefore: pending.rows, rowsAfter: table.numberOfRows,
                                            rowsAbove: pending.rowsAbove) else {
            // Not yet; a record that waited a second belongs to an update that never came.
            if !isPending { self.pending = nil }
            return false
        }
        self.pending = nil
        let anchor = pending.firstRow + pending.rowsAbove
        // A table measures only the rows on screen, so the hold shows the new rows to the table one screen at a time,
        // in this turn of the main thread: nothing reaches the display.
        let clip = scroll.contentView
        let offset = clip.bounds.minY
        var row = 0
        while row < anchor {
            clip.scroll(to: NSPoint(x: clip.bounds.minX, y: table.rect(ofRow: row).minY))
            table.needsLayout = true
            table.layoutSubtreeIfNeeded()
            let shown = table.rows(in: clip.documentVisibleRect)
            row = max(row + 1, shown.location + shown.length)
        }
        let move = TranscriptHoldRule.correction(heightsBefore: pending.heights, firstRow: pending.firstRow,
                                                 heightsAfter: (0..<anchor).map { table.rect(ofRow: $0).height },
                                                 rowsAbove: pending.rowsAbove)
        // A table keeps its offset when rows arrive, so the offset is still the reader's.
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: offset + move))
        scroll.reflectScrolledClipView(clip)
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
