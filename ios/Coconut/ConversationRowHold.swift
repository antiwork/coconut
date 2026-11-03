import SwiftUI

/// The row that the chat showed at the top on purpose, such as the card that opened it.
/// Rows above it can change without a scroll by the reader: the list inserts an older page after the scroll,
/// or rows take their real height while assistive technology reads them. The chat then scrolls back to the row.
struct ConversationRowHold {
    private(set) var row: String?

    mutating func hold(_ row: String) { self.row = row }

    mutating func release() { row = nil }

    /// A scroll by the reader ends the hold.
    mutating func phaseChanged(_ phase: ScrollPhase) {
        if phase == .interacting { release() }
    }

    /// The row to show again after the offset or the content height changed by `layoutChange`,
    /// or nil when the change is the reader's.
    func rowToRestore(layoutChange: CGFloat, userScrolling: Bool, followsBottom: Bool) -> String? {
        guard let row, !userScrolling, !followsBottom, abs(layoutChange) > 1 else { return nil }
        return row
    }
}
