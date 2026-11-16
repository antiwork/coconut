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
/// still tall, and the scroll waits. SwiftUI reports the short inset only with the next scroll, so the inset of AppKit
/// ends the wait (TranscriptInsetProbe). The wait ends after `limit` in any case. Until the scroll, the other scroll rules
/// of the chat do not scroll.
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

    /// The end of the wait of the send at `sentAt` without a layout. True when that scroll waited. The timer of an
    /// older send does not end the wait of a newer one.
    mutating func timedOut(sentAt: Date?) -> Bool {
        guard let sentAt, self.sentAt == sentAt else { return false }
        self.sentAt = nil
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
            // AppKit has the short composer some frames before SwiftUI reports it.
            .background(TranscriptInsetProbe(active: scroll.waits) { inset in
                var sent = scroll
                let goes = sent.laidOut(inset: inset)
                if sent != scroll { scroll = sent }
                if goes && follows() { move() }
            })
            .task(id: scroll.sentAt) {
                let sentAt = scroll.sentAt
                guard sentAt != nil else { return }
                try? await Task.sleep(for: .seconds(SentMessageScroll.limit))
                // A new send cancels the timer of the older one. That timer must not end the new wait.
                guard !Task.isCancelled, scroll.timedOut(sentAt: sentAt) else { return }
                if follows() { move() }
            }
    }
}

/// The bottom inset of the scroll view of a chat, as AppKit has it. AppKit changes it when the composer grows or gets
/// short, and SwiftUI reports that change only with the next scroll. While `active`, the probe observes the inset and
/// also reads it on each display frame, and reports a change. The probe is the background of the list, so the scroll view of the list has its
/// frame.
struct TranscriptInsetProbe: NSViewRepresentable {
    let active: Bool
    let changed: @MainActor (CGFloat) -> Void

    func makeNSView(context: Context) -> ProbeView { ProbeView() }

    func updateNSView(_ view: ProbeView, context: Context) {
        view.changed = changed
        view.active = active
    }

    final class ProbeView: NSView {
        var changed: (@MainActor (CGFloat) -> Void)?
        var active = false {
            didSet { if active != oldValue { update() } }
        }
        private var link: CADisplayLink?
        private var observation: NSKeyValueObservation?
        private weak var scrollView: NSScrollView?
        private var reported: CGFloat?

        // The view takes no click and no wheel event.
        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            scrollView = nil
            update()
        }

        private func update() {
            guard active, window != nil else {
                link?.invalidate()
                link = nil
                observation = nil
                reported = nil
                return
            }
            guard link == nil else { return }
            let link = displayLink(target: self, selector: #selector(tick(_:)))
            link.add(to: .main, forMode: .common)
            self.link = link
        }

        @objc private func tick(_ link: CADisplayLink) {
            guard let scrollView = scrollView ?? findScrollView() else { return }
            if observation == nil {
                // AppKit sets the inset during an update of SwiftUI, which must not change state. The report waits for
                // the end of that update, so the move can still come in the same frame.
                observation = scrollView.observe(\.contentInsets) { [weak self] _, _ in
                    DispatchQueue.main.async { MainActor.assumeIsolated { self?.report() } }
                }
            }
            report()
        }

        private func report() {
            guard active, let inset = scrollView?.contentInsets.bottom, inset != reported else { return }
            reported = inset
            changed?(inset)
        }

        /// The scroll view of the list: the largest scroll view of the window with a table that holds the center of
        /// this view. A scroll view in a row, for example of a code block, is smaller.
        private func findScrollView() -> NSScrollView? {
            guard let root = window?.contentView, bounds.width > 0, bounds.height > 0 else { return nil }
            let center = convert(NSPoint(x: bounds.midX, y: bounds.midY), to: nil)
            var queue: [NSView] = [root]
            var found: NSScrollView?
            while !queue.isEmpty {
                let view = queue.removeFirst()
                if let scroll = view as? NSScrollView, scroll.documentView is NSTableView,
                   scroll.convert(scroll.bounds, to: nil).contains(center),
                   found.map({ scroll.bounds.width * scroll.bounds.height > $0.bounds.width * $0.bounds.height }) ?? true {
                    found = scroll
                }
                queue += view.subviews
            }
            scrollView = found
            return found
        }
    }
}
#endif
