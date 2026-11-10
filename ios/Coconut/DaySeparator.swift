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

struct DaySeparatorRow: View {
    let date: Date
    /// The moment that "Today" counts from. The chat replaces it when the day or the time zone changes, and the label
    /// then reads the calendar of the device again.
    let now: Date

    var body: some View {
        Text(DaySeparator.label(for: date, now: now))
            .font(.caption2.weight(.medium)).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("day-separator")
    }
}
