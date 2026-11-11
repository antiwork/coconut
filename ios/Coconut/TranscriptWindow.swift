/// The rows of a chat that its list builds: from one row down to the newest row.
///
/// A Mac list measures the height of every row when it loads: about 1.4 ms for each row, so a chat of 1,270 rows took
/// about 2 seconds to show. A row that the list gets after it loads keeps an estimated height until it scrolls into
/// view, so the content jumps when the reader scrolls into it. The chat therefore builds a window of the newest rows in
/// one step, and the list never gets rows above the reader: a new first row of the window makes a new list, which
/// measures every row. Older rows come only with the control above the window.
///
/// The window keeps the ID of its first row, not a count. New rows at the bottom, and a page of older history from the
/// Mac, then leave the rows of the window as they are.
///
/// An iPhone list measures only the rows on screen, so it builds every row. There the window holds every row and never
/// changes, and a press of the control always loads a page from the Mac. Each caller asks the window, so no caller can
/// forget the platform.
struct TranscriptWindow: Equatable {
    /// What a press of the control above the window does.
    enum Press: Equatable {
        /// The window holds every row that the app has: the Mac sends a page of older history.
        case loadPage
        /// The app has older rows that the window does not hold: the window adds them.
        case growWindow
    }

    /// The rows that a chat builds when it opens. 60 rows open in about 250 ms on a Mac and fill more than a screen.
    static let openRows = 60
    /// The rows that one press of the control above the window adds.
    static let pressRows = 60
    /// The rows above a message that the chat opens at, so the reader sees what came before it.
    static let contextRows = 5

    /// Whether the list builds only a window of the rows. False: the list builds every row.
    let isUsed: Bool
    /// The first row that the list builds. Nil until the chat keeps its window; until then the window counts from the end.
    private(set) var firstId: String?

    init(isUsed: Bool = Self.platformUsesWindow) {
        self.isUsed = isUsed
    }

    /// The positions of the rows to build. `focusId` is a row that must be in the window, for example the card or the
    /// message that the chat opens at.
    func range(of ids: [String], focusId: String? = nil) -> Range<Int> {
        guard isUsed else { return ids.indices }
        var start = max(0, ids.count - Self.openRows)
        // The window starts at its first row, also when rows arrive above it. That row can leave the chat, for example
        // when the Mac deletes it; the window then counts from the end.
        if let firstId, let position = ids.firstIndex(of: firstId) { start = position }
        if let focusId, let position = ids.firstIndex(of: focusId) { start = min(start, max(0, position - Self.contextRows)) }
        return start..<ids.count
    }

    /// Keeps the first row of the window as it is now. Then a new row at the bottom does not push the first row out, and
    /// a page of history from the Mac stays above the window.
    mutating func keep(_ ids: [String], focusId: String? = nil) {
        guard isUsed else { return }
        let start = range(of: ids, focusId: focusId).lowerBound
        if start < ids.count { firstId = ids[start] }
    }

    /// Adds `rows` older rows above the window. False when the window starts at the first row of the chat.
    @discardableResult
    mutating func growTop(in ids: [String], focusId: String? = nil, rows: Int = Self.pressRows) -> Bool {
        let start = range(of: ids, focusId: focusId).lowerBound
        guard start > 0 else { return false }
        firstId = ids[max(0, start - rows)]
        return true
    }

    /// What a press of the control above the window does now.
    func press(_ ids: [String], focusId: String? = nil) -> Press {
        range(of: ids, focusId: focusId).lowerBound == 0 ? .loadPage : .growWindow
    }

    /// Widens the window so that it holds `id` and a few rows above it, for a jump to an older message. False when the
    /// window holds it already or the chat has no such row.
    @discardableResult
    mutating func include(_ id: String, in ids: [String]) -> Bool {
        guard let position = ids.firstIndex(of: id) else { return false }
        let start = max(0, position - Self.contextRows)
        guard start < range(of: ids).lowerBound else { return false }
        firstId = ids[start]
        return true
    }
}
