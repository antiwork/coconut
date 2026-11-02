import SwiftUI

/// The row that the chat showed at the top on purpose, such as the card that opened it.
/// Lazy rows above it can change height without a scroll by the reader, for example while assistive
/// technology reads the transcript. The scroll view then can move to another place, so the chat scrolls back.
struct ConversationRowHold {
    private(set) var row: String?

    mutating func hold(_ row: String) { self.row = row }

    mutating func release() { row = nil }

    /// A scroll by the reader ends the hold.
    mutating func phaseChanged(_ phase: ScrollPhase) {
        if phase == .interacting { release() }
    }

    /// The row to show again after the offset changed by `offsetChange`, or nil when the change is the reader's.
    func rowToRestore(offsetChange: CGFloat, userScrolling: Bool, followsBottom: Bool) -> String? {
        guard let row, !userScrolling, !followsBottom, abs(offsetChange) > 1 else { return nil }
        return row
    }
}
