import SwiftUI

/// The row that the chat showed at the top on purpose, such as the card that opened it.
/// Rows above it can change without a scroll by the reader: the list inserts an older page after the scroll,
/// or rows take their real height while assistive technology reads them. The chat then scrolls back to the row.
struct ConversationRowHold {
    private(set) var row: String?
    /// The moment that the chat last scrolled to the row.
    private var placed = Date.distantPast

    mutating func hold(_ row: String, now: Date = Date()) {
        self.row = row
        placed = now
    }

    mutating func release() { row = nil }

    /// A scroll by the reader ends the hold.
    mutating func phaseChanged(_ phase: ScrollPhase) {
        if phase == .interacting { release() }
    }

    /// A Mac mouse wheel reports no scroll phase. A move of the offset alone, some time after the chat scrolled to the
    /// row, is the reader's and ends the hold. Without this, each step of the wheel scrolled back to the row.
    mutating func offsetMoved(now: Date = Date()) {
        if now.timeIntervalSince(placed) > 0.5 { release() }
    }

    /// The row to show again after the offset or the content height changed by `layoutChange`,
    /// or nil when the change is the reader's.
    func rowToRestore(layoutChange: CGFloat, userScrolling: Bool, followsBottom: Bool) -> String? {
        guard let row, !userScrolling, !followsBottom, abs(layoutChange) > 1 else { return nil }
        return row
    }
}
