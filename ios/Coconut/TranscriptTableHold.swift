import SwiftUI
#if os(macOS)
import AppKit
#endif

/// Keeps the rows on screen in place when older rows arrive above them in a Mac chat.
///
/// A Mac list gives a row that it adds above the screen an estimated height of one point, and measures the row only
/// when it scrolls into view. The row then takes its real height and pushes the rows on screen down: a jump of
/// hundreds of points. So the hold measures the new rows at once, while they are still above the screen, and moves
/// the scroll position by the height that they add.
@MainActor
final class TranscriptTableHold {
    #if os(macOS)
    fileprivate weak var table: NSTableView?
    private var pending: (rows: Int, height: CGFloat, offset: CGFloat, at: Date)?
    private var movedAt = Date.distantPast
    #endif

    /// The hold moved the scroll position less than 0.15 s ago. The scroll observers see that move a frame later and
    /// must not take it for the reader's.
    var movedRecently: Bool {
        #if os(macOS)
        Date().timeIntervalSince(movedAt) < 0.15
        #else
        false
        #endif
    }

    /// A growth waits for the list to add its rows.
    var isPending: Bool {
        #if os(macOS)
        pending.map { Date().timeIntervalSince($0.at) < 1 } ?? false
        #else
        false
        #endif
    }

    /// Records the list before the window grows at the top.
    func prepare() {
        #if os(macOS)
        guard let table, let clip = table.enclosingScrollView?.contentView else { return }
        pending = (table.numberOfRows, table.frame.height, clip.bounds.minY, Date())
        #endif
    }

    /// Measures the rows that arrived above the screen and moves the scroll position by their height. Nothing happens
    /// until the list has the new rows. True when it moved the scroll position.
    @discardableResult
    func finishIfReady() -> Bool {
        #if os(macOS)
        guard let pending, let table, let scroll = table.enclosingScrollView, table.numberOfRows > pending.rows else { return false }
        self.pending = nil
        // The list adds the rows in the next update. A record that waited longer belongs to a growth that added no
        // row, and a later row, such as a new message at the bottom, must not move the chat.
        guard Date().timeIntervalSince(pending.at) < 1 else { return false }
        let added = table.numberOfRows - pending.rows
        // The new rows are at the top, after the header rows. A table measures only the rows on screen, so the hold shows
        // the new rows to the table one screen at a time, in this turn of the main thread: nothing reaches the display.
        let clip = scroll.contentView
        let last = min(table.numberOfRows - 1, added + 1)
        var row = 0
        while row <= last {
            clip.scroll(to: NSPoint(x: clip.bounds.minX, y: table.rect(ofRow: row).minY))
            table.needsLayout = true
            table.layoutSubtreeIfNeeded()
            let shown = table.rows(in: clip.documentVisibleRect)
            row = max(row + 1, shown.location + shown.length)
        }
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: pending.offset + table.frame.height - pending.height))
        scroll.reflectScrolledClipView(clip)
        movedAt = Date()
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
