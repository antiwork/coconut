import Combine
import SwiftUI

enum DaySeparator {
    /// Fires on the main thread when the calendar day or the time zone of the device changes. The system posts both
    /// notifications on any thread. An open chat then groups its rows and writes its day labels again.
    @MainActor static let changes: AnyPublisher<Void, Never> = NotificationCenter.default
        .publisher(for: .NSCalendarDayChanged)
        .merge(with: NotificationCenter.default.publisher(for: .NSSystemTimeZoneDidChange))
        .map { _ in }
        .receive(on: DispatchQueue.main)
        .eraseToAnyPublisher()

    /// The day label shown above the first message of each calendar day.
    static func label(for date: Date, now: Date = .now, calendar: Calendar = .current, locale: Locale = .current) -> String {
        // The label reads in the calendar's zone, the same one the day test below uses, so an evening message never
        // prints the next day.
        let style = Date.FormatStyle(locale: locale, calendar: calendar, timeZone: calendar.timeZone)
        let day = calendar.startOfDay(for: date)
        let today = calendar.startOfDay(for: now)
        let days = calendar.dateComponents([.day], from: day, to: today).day ?? 0
        if days == 0 { return String(localized: "Today") }
        if days == 1 { return String(localized: "Yesterday") }
        if days > 1 && days < 7 { return date.formatted(style.weekday(.wide)) }
        let sameYear = calendar.isDate(date, equalTo: now, toGranularity: .year)
        let dated = style.month(.abbreviated).day()
        return sameYear ? date.formatted(dated) : date.formatted(dated.year())
    }

    /// Whether two messages belong to one calendar day. Messages without a date stay together, because a group
    /// that crosses midnight would leave the new day with no separator.
    static func sameDay(_ first: String?, _ second: String?, calendar: Calendar = .current) -> Bool {
        guard let first = first?.apiDate, let second = second?.apiDate else { return true }
        return calendar.isDate(first, inSameDayAs: second)
    }
}

/// The date pill of the chat: a small capsule with the fill of a bot bubble, as the date of WhatsApp. It has the same
/// space around it in its own place and at the top of the chat, so the two pills meet at one position.
struct DayPill: View {
    static let spaceAbove: CGFloat = 18
    static let spaceBelow: CGFloat = 12

    let date: Date
    /// The moment that "Today" counts from. The chat replaces it when the day or the time zone changes, and the label
    /// then reads the calendar of the device again.
    let now: Date

    var body: some View {
        Text(DaySeparator.label(for: date, now: now))
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 14).padding(.vertical, 4)
            .background(BoardStyle.surface, in: Capsule())
            // A ring in the color of the page. It does not show on the page. Over a bot bubble, which has the fill of
            // the pill, it keeps the shape of the pinned pill visible.
            .background(BoardStyle.paper, in: Capsule().inset(by: -2))
            .frame(maxWidth: .infinity)
            .padding(.top, Self.spaceAbove).padding(.bottom, Self.spaceBelow)
    }
}

/// The pill in its own place, above the first row of a day.
struct DaySeparatorRow: View {
    let date: Date
    let now: Date
    /// The row of the pill. While the pinned pill stands in for this row, the pill in its place hides, so the day
    /// shows one time.
    let row: String
    let pinned: PinnedDay
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        DayPill(date: date, now: now)
            .opacity(pinned.standsIn(for: row) ? 0 : 1)
            // The two pills change places with a fade when a scroll starts or stops, and at once during a scroll.
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: pinned.isVisible)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("day-separator")
    }
}

/// The pill of the day at the top of the chat. It shows during a scroll, the pill of the next day pushes it away, and
/// it fades a moment after the scroll stops. It takes no touch, and VoiceOver reads only the pill in its own place.
struct PinnedDayPill: View {
    /// The space between the top of the chat and the pinned pill.
    static let inset: CGFloat = 6

    let pinned: PinnedDay
    let now: Date
    /// The height of a header that covers the top of the chat list. The pill stays below it.
    var header: CGFloat = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // A marker with no height. Its position is the line that a row passes to become the top row of the chat.
        Color.clear.frame(maxWidth: .infinity).frame(height: 0)
            .onGeometryChange(for: CGFloat.self) { $0.frame(in: .global).minY } action: { pinned.line = $0 }
            .overlay(alignment: .top) {
                if let day = pinned.day {
                    DayPill(date: day, now: now)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { pinned.height = $0 }
                        .offset(y: pinned.push)
                        .opacity(pinned.isVisible ? pinned.strength : 0)
                        .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: pinned.isVisible)
                }
            }
            .padding(.top, header + Self.inset - DayPill.spaceAbove)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

/// The first row of a chat list on the Mac: the space under the title that floats over the top of the chat. A Mac list
/// ignores a top content margin (its scroll view keeps a top inset of 0), so a row keeps the space. The row insets of
/// the chat (5 points above and 5 below) add the last 10 points. The row has no ID, so it does not change the list ID
/// of the window or a scroll to a row. On the iPhone it is empty.
struct ChatTitleSpace: View {
    /// The height of the title with its gaps.
    let titleHeight: CGFloat

    var body: some View {
        #if os(macOS)
        Color.clear.frame(height: max(0, titleHeight - 10))
            .accessibilityHidden(true)
        #endif
    }
}

extension View {
    /// A row of the chat tells the pinned day where it is, and leaves it when the list removes the row.
    func pinnedDayRow(_ pinned: PinnedDay, id: String, date: Date?, startsDay: Bool) -> some View {
        onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { frame in
            if let date { pinned.place(id, frame: frame, date: date, startsDay: startsDay) }
        }
        .onDisappear { pinned.remove(id) }
    }

    /// The chat list shows the pinned pill at its top, below a header of the chat that covers the top of the list.
    func pinnedDayPill(_ pinned: PinnedDay, now: Date, below header: CGFloat = 0) -> some View {
        overlay(alignment: .top) { PinnedDayPill(pinned: pinned, now: now, header: header) }
            #if os(macOS)
            // A mouse wheel, a scroll bar and the keyboard report no scroll phase on a Mac, so the position tells
            // that the chat scrolls. A new window size moves the position too, and so does the chat when it follows
            // a new message to its bottom. These are not scrolls of the reader.
            .onScrollGeometryChange(for: [CGFloat].self) {
                [$0.contentOffset.y, $0.contentSize.height, $0.containerSize.height, $0.contentInsets.bottom,
                 $0.containerSize.width]
            } action: { old, new in
                let sameWindow = old[2] == new[2] && old[3] == new[3] && old[4] == new[4]
                let atBottom = new[0] + new[2] - new[3] >= new[1] - 1
                if sameWindow, old[0] != new[0], !atBottom { pinned.scrolled() }
            }
            #endif
    }
}

/// The day of the messages at the top of the chat, for the pill that stays there during a scroll. The rows of the chat
/// tell it where they are. Only the pills read it, so a scroll does not draw the chat again.
@MainActor @Observable
final class PinnedDay {
    /// A date in the day of the top row. Nil when no message is at the top of the chat.
    private(set) var day: Date?
    /// The first row of the pinned day, while the chat has it. Its own pill is at or above the pinned pill.
    private(set) var source: String?
    /// How far the pill of the next day has pushed the pinned pill up: zero or less.
    private(set) var push: CGFloat = 0
    /// True during a scroll, and for one second after it.
    private(set) var isVisible = false

    /// The top of the pinned pill with its space, and its height. The pinned pill measures them.
    @ObservationIgnored var line: CGFloat = 0 { didSet { if line != oldValue { update() } } }
    @ObservationIgnored var height: CGFloat = 52 { didSet { if height != oldValue { update() } } }
    @ObservationIgnored private var rows: [String: Row] = [:]
    @ObservationIgnored private var hide: Task<Void, Never>?

    private struct Row {
        let top: CGFloat
        let bottom: CGFloat
        let date: Date
        let startsDay: Bool
    }

    /// The pinned pill fades as the pill of the next day pushes it away.
    var strength: Double { height > 0 ? max(0, 1 + push / height) : 1 }

    func standsIn(for row: String) -> Bool { isVisible && source == row }

    /// A row of the chat moved. `frame` is in the global space, as the line is.
    func place(_ row: String, frame: CGRect, date: Date, startsDay: Bool) {
        rows[row] = Row(top: frame.minY, bottom: frame.maxY, date: date, startsDay: startsDay)
        update()
    }

    func remove(_ row: String) {
        if rows.removeValue(forKey: row) != nil { update() }
    }

    /// The position of the chat changed, on a platform that reports no phase for each kind of scroll. The pill shows,
    /// and it stays for one second after the last change.
    func scrolled() {
        scrolls(true)
        scrolls(false)
    }

    /// The scroll of the chat starts or stops. The pill stays for one second after a stop.
    func scrolls(_ active: Bool) {
        hide?.cancel()
        if active {
            if !isVisible { isVisible = true }
        } else if isVisible {
            hide = Task { [weak self] in
                try? await Task.sleep(for: .seconds(1))
                if !Task.isCancelled { self?.isVisible = false }
            }
        }
    }

    /// Finds the top row, the first row of its day, and the first row of the next day. A value changes only when it
    /// is different, because each change draws the pills again.
    private func update() {
        var top: Row?
        var start: (id: String, top: CGFloat)?
        var next: CGFloat?
        for (id, row) in rows {
            if row.top <= line {
                if top.map({ row.top > $0.top }) ?? true { top = row }
                if row.startsDay, start.map({ row.top > $0.top }) ?? true { start = (id, row.top) }
            } else if row.startsDay, next.map({ row.top < $0 }) ?? true {
                next = row.top
            }
        }
        // A list row has space around it, so the top row can end a little above the line. A wider gap is the start
        // of the chat, or a row that is not a message.
        if let found = top, found.bottom < line - 12 { top = nil }
        let sameDay = top.flatMap { found in day.map { Calendar.current.isDate(found.date, inSameDayAs: $0) } } ?? false
        if !sameDay, day != top?.date { day = top?.date }
        let source = top == nil ? nil : start?.id
        if source != self.source { self.source = source }
        let push = top == nil ? 0 : next.map { min(0, $0 - line - height) } ?? 0
        if push != self.push { self.push = push }
    }
}
