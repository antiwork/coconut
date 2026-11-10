import SwiftUI

/// The content height of a chat, with the offset and the container height at that moment. Two values are equal when the
/// heights are equal, so only a change of the content counts as a change.
struct ContentSizeChange: Equatable {
    let height: CGFloat
    let offset: CGFloat
    let container: CGFloat

    init(_ geometry: ScrollGeometry) {
        height = geometry.contentSize.height
        offset = geometry.contentOffset.y
        container = geometry.containerSize.height
    }

    static func == (a: ContentSizeChange, b: ContentSizeChange) -> Bool { a.height == b.height }
}

/// What the reader of a chat does, for the growth of its window. A class: the scroll observers write it on each frame,
/// and a change must not draw the chat again.
@MainActor
final class TranscriptReader {
    /// The reader is within `TranscriptWindow.nearScreens` screens of the oldest row that the list holds.
    var nearTop = false
    /// The last scroll or keystroke of the reader.
    var lastAction = Date.distantPast

    /// The reader did nothing for half a second.
    var idle: Bool { Date().timeIntervalSince(lastAction) > 0.5 }

    static func nearTop(_ geometry: ScrollGeometry) -> Bool {
        geometry.contentOffset.y + geometry.contentInsets.top < geometry.containerSize.height * TranscriptWindow.nearScreens
    }
}

/// The rows of a chat that its list builds: the newest rows, from one row down to the end.
///
/// A Mac list measures the height of every row that it holds when it opens. A chat of 1,270 rows took about 2 seconds
/// to show. The chat builds only the rows that fill its view first, then a window of `settledRows` right after the first
/// frame. Then it adds small batches of older rows: while the reader is near the top of the window, and while the
/// reader does nothing, up to `idleRows`. The rows stay in memory; only the list holds fewer of them.
///
/// The window keeps the ID of its oldest row, not a count. New rows at the bottom, and a page of older history from the
/// Mac, then leave the rows on screen where they are.
struct TranscriptWindow: Equatable {
    /// Rows that fill a chat view when it opens. A row is at least about 45 points high, so 18 rows fill a view of
    /// 800 points.
    static let openRows = 18
    /// The rows that the chat builds after its first frame.
    static let settledRows = 40
    /// The rows of one batch. The Mac measures a new batch at once. A batch costs about 40 ms and 4 ms more for each
    /// row, so 5 rows keep the pause near 50 ms; the longest pause of a scroll through the whole chat was 160 ms.
    static let batchRows = 5
    /// The window grows while the reader is within this many screens of its oldest row, so a scroll seldom reaches it.
    static let nearScreens: CGFloat = 3
    /// The rows that the window grows to while the reader does nothing. An update of an open chat cost the same with 40
    /// and with 400 rows, but a switch to another chat removes each row: about 0.2 ms a row.
    static let idleRows = 200
    /// The rows above a message that the chat opens at, so the reader sees what came before it.
    static let contextRows = 5

    /// The oldest row that the list builds. Nil while the window counts its rows from the end.
    private(set) var firstId: String?
    /// The fewest rows that the window holds, counted from the end.
    private(set) var minimumRows = Self.openRows

    /// The positions of the rows to build. `focusId` is a row that must be in the window, for example the card or the
    /// message that the chat opens at.
    func range(of ids: [String], focusId: String? = nil) -> Range<Int> {
        var start = max(0, ids.count - minimumRows)
        // The window starts at its oldest row, also when rows arrive above it. That row can leave the chat, for example
        // when the Mac deletes it; the window then counts from the end.
        if let firstId, let position = ids.firstIndex(of: firstId) { start = position }
        if let focusId, let position = ids.firstIndex(of: focusId) { start = min(start, max(0, position - Self.contextRows)) }
        return start..<ids.count
    }

    /// The window after the first frame.
    mutating func settle() {
        minimumRows = max(minimumRows, Self.settledRows)
    }

    /// Widens the window so that it holds `id`. False when the window holds it already or the chat has no such row.
    @discardableResult
    mutating func include(_ id: String, in ids: [String]) -> Bool {
        guard let position = ids.firstIndex(of: id) else { return false }
        let current = range(of: ids)
        guard position - Self.contextRows < current.lowerBound, current.lowerBound > 0 else { return false }
        firstId = ids[max(0, position - Self.contextRows)]
        return true
    }

    /// Adds a batch of older rows above the window. False when the window starts at the oldest row already.
    @discardableResult
    mutating func growTop(in ids: [String], focusId: String? = nil) -> Bool {
        let start = range(of: ids, focusId: focusId).lowerBound
        guard start > 0 else { return false }
        firstId = ids[max(0, start - Self.batchRows)]
        return true
    }

    /// Keeps the oldest row of the window as it is now, so rows that arrive above it, such as a page of history from the
    /// Mac, stay out of the window until it grows.
    mutating func pinStart(in ids: [String], focusId: String? = nil) {
        let start = range(of: ids, focusId: focusId).lowerBound
        if start < ids.count { firstId = ids[start] }
    }

    /// Whether rows older than the window exist.
    func hasRowsAbove(_ ids: [String], focusId: String? = nil) -> Bool {
        range(of: ids, focusId: focusId).lowerBound > 0
    }
}
