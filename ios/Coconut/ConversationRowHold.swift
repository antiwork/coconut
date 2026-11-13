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

/// A message that the person sends moves the chat to its newest row one time (Mac).
///
/// On a Mac the chat scrolled to the row of a sent message before the layout of that row. The composer then still had
/// the height of the draft, and it got short again one pass later. So the chat went up above the tall composer and then
/// fell back by the height that the composer lost: 58 points for a draft of five lines. Now the chat scrolls after the
/// layout of the row. While the bottom inset is larger than the inset of the composer with one line, the composer is
/// still tall, and the scroll waits. A Mac does not always report the new inset, so the wait ends after `limit`. Until
/// the scroll, the other scroll rules of the chat do not scroll.
struct SentMessageScroll: Equatable {
    static let limit: TimeInterval = 0.1

    /// The moment of the send whose scroll waits. Nil when no scroll waits.
    private(set) var sentAt: Date?
    /// The smallest bottom inset of the chat: the composer with one line. A layout without the composer (no inset)
    /// does not count.
    private(set) var restingInset: CGFloat?

    var waits: Bool { sentAt != nil }

    mutating func sent(now: Date = Date()) { sentAt = now }

    /// A layout with this bottom inset. True when the waiting scroll goes now.
    mutating func laidOut(inset: CGFloat, now: Date = Date()) -> Bool {
        if inset > 1, inset < (restingInset ?? .infinity) { restingInset = inset }
        guard let sentAt else { return false }
        if now.timeIntervalSince(sentAt) < Self.limit, let restingInset, inset > restingInset + 0.5 { return false }
        self.sentAt = nil
        return true
    }

    /// The end of the wait without a layout. True when a scroll waited.
    mutating func timedOut() -> Bool {
        guard sentAt != nil else { return false }
        sentAt = nil
        return true
    }
}

#if os(macOS)
/// The scroll of a chat to the row of a sent message (SentMessageScroll). `follows` tells whether the chat stays at its
/// newest row, and `move` scrolls the chat there.
struct SentMessageMove: ViewModifier {
    @Binding var scroll: SentMessageScroll
    let follows: () -> Bool
    let move: () -> Void

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: [CGFloat].self) {
                [$0.contentSize.height, $0.contentInsets.bottom]
            } action: { _, new in
                var sent = scroll
                let goes = sent.laidOut(inset: new[1])
                if sent != scroll { scroll = sent }
                if goes && follows() { move() }
            }
            .task(id: scroll.sentAt) {
                guard scroll.waits else { return }
                try? await Task.sleep(for: .seconds(SentMessageScroll.limit))
                guard scroll.timedOut() else { return }
                if follows() { move() }
            }
    }
}
#endif
