import SwiftUI

enum DaySeparator {
    /// The day label shown above the first message of each calendar day.
    static func label(for date: Date, now: Date = .now, calendar: Calendar = .current, locale: Locale = .current) -> String {
        let day = calendar.startOfDay(for: date)
        let today = calendar.startOfDay(for: now)
        let days = calendar.dateComponents([.day], from: day, to: today).day ?? 0
        if days == 0 { return String(localized: "Today") }
        if days == 1 { return String(localized: "Yesterday") }
        if days > 1 && days < 7 { return date.formatted(Date.FormatStyle(locale: locale, calendar: calendar).weekday(.wide)) }
        let sameYear = calendar.isDate(date, equalTo: now, toGranularity: .year)
        let style = Date.FormatStyle(locale: locale, calendar: calendar).month(.abbreviated).day()
        return sameYear ? date.formatted(style) : date.formatted(style.year())
    }

    /// Whether `entry` is the first of its calendar day after `previous`.
    static func starts(_ date: Date?, after previous: Date?, calendar: Calendar = .current) -> Bool {
        guard let date else { return false }
        guard let previous else { return true }
        return !calendar.isDate(date, inSameDayAs: previous)
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
