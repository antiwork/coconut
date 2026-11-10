import SwiftUI

enum DaySeparator {
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
    @State private var now = Date()

    var body: some View {
        Text(DaySeparator.label(for: date, now: now))
            .font(.caption2.weight(.medium)).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("day-separator")
            .onReceive(NotificationCenter.default.publisher(for: .NSCalendarDayChanged)) { _ in now = Date() }
    }
}
