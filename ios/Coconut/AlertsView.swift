import SwiftUI

/// The alerts feed: the operational notices of the tooling of the Mac, newest first, with a label for each day. The
/// bell of the Inbox opens it. A tap on a row opens the alert. A swipe marks it as read, and a long press resolves it.
struct AlertsView: View {
    @Environment(AppStore.self) private var store
    /// Shows only the unread alerts. The page opens with all of them.
    @State private var unreadOnly = false
    @State private var openAlert: AlertSelection?
    /// The moment that "Today" counts from. It moves on when the day or the time zone changes.
    @State private var now = Date()

    private var days: [AlertDay] {
        AlertDay.days(of: store.alertFeed.items.filter { !unreadOnly || $0.isUnread }, now: now)
    }

    var body: some View {
        let days = days
        Group {
            if days.isEmpty {
                // The quiet line of an empty page. A page that only hides its read alerts says so.
                Text(unreadOnly && !store.alertFeed.items.isEmpty ? "No unread alerts." : "No alerts.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityIdentifier("alerts-empty")
            } else {
                List {
                    ForEach(days) { day in
                        Text(day.label)
                            .font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                            .padding(.top, BoardStyle.Space.l)
                            .listRowSeparator(.hidden)
                            .accessibilityAddTraits(.isHeader)
                            .accessibilityIdentifier("day-separator")
                        ForEach(day.alerts) { alert in
                            row(alert, last: alert.id == day.alerts.last?.id)
                        }
                    }
                    .listRowBackground(BoardStyle.paper)
                    .listRowInsets(EdgeInsets(top: BoardStyle.Space.m, leading: BoardStyle.Space.l,
                                              bottom: BoardStyle.Space.m, trailing: BoardStyle.Space.l))
                    .listRowSeparatorTint(BoardStyle.line)
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                // A row moves when the person resolves it, and leaves the list of the unread ones when it is read.
                .animation(.snappy, value: days.flatMap(\.alerts).map(\.id))
            }
        }
        .background(BoardStyle.paper)
        .navigationTitle("")
        .inlineNavigationTitle()
        .toolbar { toolbar }
        .sheet(item: $openAlert) { AlertDetailView(alertId: $0.id) }
        .onReceive(DaySeparator.changes) { now = Date() }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            // The title of the page is also its filter, as the count of the Inbox is also its list.
            Button { unreadOnly.toggle() } label: {
                Text("Alerts · \(unreadOnly ? "Unread" : "All")")
                    .font(.subheadline.weight(.semibold)).frame(minHeight: 44)
                    .contentTransition(.opacity)
            }
            .accessibilityLabel("Alerts")
            .accessibilityValue(unreadOnly ? "Unread" : "All")
            .accessibilityHint(unreadOnly ? "Shows all alerts." : "Shows only the unread alerts.")
            .accessibilityIdentifier("alerts-filter")
        }
        ToolbarItem(placement: .trailingBar) {
            // The words push the title of the page out of the middle of the bar, so the bar shows the symbol. VoiceOver
            // reads the words.
            Button { Task { await store.markAllAlertsRead() } } label: {
                Label("Mark All Read", systemImage: "checkmark.circle").labelStyle(.iconOnly)
            }
            .disabled(store.unreadAlertCount == 0)
            .accessibilityIdentifier("alerts-mark-all-read")
        }
    }

    private func row(_ alert: FeedAlert, last: Bool) -> some View {
        Button { openAlert = AlertSelection(id: alert.id) } label: {
            AlertRow(alert: alert, now: now).contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        // The line under a row starts at its text. The last row of a day has no line: the next day follows.
        .alignmentGuide(.listRowSeparatorLeading) { _ in AlertLevelDot.column }
        .listRowSeparator(.hidden, edges: .top)
        .listRowSeparator(last ? .hidden : .visible, edges: .bottom)
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            if alert.isUnread {
                Button { Task { await store.markAlertRead(alert) } } label: {
                    Label("Mark Read", systemImage: "checkmark")
                }
            }
        }
        .contextMenu {
            if alert.isUnread {
                Button { Task { await store.markAlertRead(alert) } } label: {
                    Label("Mark Read", systemImage: "checkmark")
                }
            }
            if !alert.isResolved {
                Button { Task { await store.resolveAlert(alert) } } label: {
                    Label("Resolve", systemImage: "checkmark.circle")
                }
            }
        }
        .accessibilityIdentifier("alert-row-\(alert.id)")
    }
}

/// The bell of the Inbox: it opens the alerts feed, and its badge counts the unread alerts.
struct AlertsBell: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute

    var body: some View {
        let unread = store.unreadAlertCount
        Button { pushRoute?(AlertsRoute()) } label: {
            HStack(spacing: BoardStyle.Space.xs) {
                Image(systemName: "bell")
                if unread > 0 {
                    // The count stands next to the bell, as the badges of a chat row stand next to its age. On the
                    // bell it covered half of the symbol. VoiceOver reads the count in the value of the button.
                    UnreadBadge(count: unread)
                        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                        .accessibilityHidden(true)
                }
            }
        }
        .accessibilityLabel("Alerts")
        .accessibilityValue(unread == 0 ? "" : unread == 1 ? "1 unread alert" : "\(unread) unread alerts")
        .accessibilityIdentifier("alerts-bell")
    }
}

/// The alert that the detail sheet shows. The sheet reads the alert from the store, so it follows each update.
private struct AlertSelection: Identifiable {
    let id: String
}

/// The alerts of one calendar day, in the order of the feed.
struct AlertDay: Identifiable, Equatable {
    /// The start of the day.
    let id: Date
    let label: String
    var alerts: [FeedAlert]

    /// The days of the feed, newest first. In each day the alerts that are not resolved come first, and each part
    /// has its newest alert first, as the Mac sends them.
    static func days(of alerts: [FeedAlert], now: Date = .now, calendar: Calendar = .current) -> [AlertDay] {
        var days: [Date: [FeedAlert]] = [:]
        let ordered = alerts.enumerated().sorted { first, second in
            let (left, right) = (first.element, second.element)
            if left.isResolved != right.isResolved { return right.isResolved }
            let (leftDate, rightDate) = (left.createdAt.apiDate ?? .distantPast, right.createdAt.apiDate ?? .distantPast)
            return leftDate != rightDate ? leftDate > rightDate : first.offset < second.offset
        }
        for (_, alert) in ordered {
            days[calendar.startOfDay(for: alert.createdAt.apiDate ?? .distantPast), default: []].append(alert)
        }
        return days.keys.sorted(by: >).map { day in
            AlertDay(id: day, label: DaySeparator.label(for: day, now: now, calendar: calendar), alerts: days[day] ?? [])
        }
    }
}

/// The mark of a level: a ring for info, a dot in the attention tint for a notice, and a dot in the destructive tint
/// for a critical alert. The shape tells info from the other two without its color.
struct AlertLevelDot: View {
    /// The width of the dot with the gap to the text: the text of a row starts here.
    static let column: CGFloat = size + BoardStyle.Space.m
    static let size: CGFloat = 8

    let level: FeedAlert.Level

    var body: some View {
        Group {
            switch level {
            case .info: Circle().strokeBorder(Color.secondary, lineWidth: 1.5)
            case .notice: Circle().fill(BoardStyle.attention)
            case .critical: Circle().fill(BoardStyle.destructive)
            }
        }
        .frame(width: Self.size, height: Self.size)
        .accessibilityHidden(true)
    }
}

/// One row of the feed: the mark of the level, the title, the source with the age, and the first line of the body.
/// An unread alert has a heavier title. A resolved alert is quiet and says "Resolved".
struct AlertRow: View {
    let alert: FeedAlert
    let now: Date
    /// The height of the first line of the title. The dot sits in the middle of it at each text size.
    @ScaledMetric(relativeTo: .body) private var titleLine = 22

    var body: some View {
        HStack(alignment: .top, spacing: BoardStyle.Space.m) {
            AlertLevelDot(level: alert.severity)
                .opacity(alert.isResolved ? 0.4 : 1)
                .frame(height: titleLine)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(alert.title)
                    .font(.body.weight(alert.isUnread ? .semibold : .regular))
                    .foregroundStyle(alert.isResolved ? .secondary : .primary)
                    .lineLimit(2)
                HStack(spacing: 0) {
                    if alert.isResolved {
                        Text("Resolved · ")
                    }
                    Text(alert.source).lineLimit(1).truncationMode(.middle)
                    if let date = alert.createdAt.apiDate {
                        Text(" · ")
                        AlertAgeLabel(date: date).monospacedDigit()
                    }
                }
                .font(.footnote).foregroundStyle(.secondary)
                if !alert.body.isEmpty {
                    Text(alert.body)
                        .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                        .padding(.top, BoardStyle.Space.line)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(AlertSpeech.row(alert, now: now))
        .accessibilityValue(alert.isResolved ? "Resolved" : alert.isUnread ? "Unread" : "")
        .accessibilityAddTraits(.isButton)
    }
}

/// The age of an alert in its row: "Now", "6 min" or "3 hr" as in the Chats list. After a day it is the time of day,
/// because the label of the day stands above the row.
struct AlertAgeLabel: View {
    let date: Date

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { context in
            Text(CompactAgeLabel.span(max(0, context.date.timeIntervalSince(date)))
                 ?? date.formatted(date: .omitted, time: .shortened))
        }
    }
}

/// What VoiceOver says for an alert.
enum AlertSpeech {
    /// The row: "critical, <title>, <source>, <relative time>".
    static func row(_ alert: FeedAlert, now: Date = .now) -> String {
        var parts = [alert.severity.rawValue, alert.title, alert.source]
        if let date = alert.createdAt.apiDate { parts.append(relative(date, now: now)) }
        return parts.joined(separator: ", ")
    }

    /// The age of an alert in words, for example "5 minutes ago".
    static func relative(_ date: Date, now: Date = .now) -> String {
        // The clock of the Mac can be a little ahead of the phone. An alert never reads as "in 2 seconds".
        RelativeDateTimeFormatter().localizedString(for: min(date, now), relativeTo: now)
    }
}

/// The sheet of one alert: its level, its full title, its source, when it came, its full body, its link, and the
/// actions Mark Read and Resolve.
struct AlertDetailView: View {
    let alertId: String
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    private var alert: FeedAlert? { store.alertFeed.items.first { $0.id == alertId } }

    var body: some View {
        Group {
            if let alert {
                ScrollView {
                    content(alert)
                        .padding(.horizontal, BoardStyle.Space.xl)
                        .padding(.top, BoardStyle.Space.xxl).padding(.bottom, BoardStyle.Space.l)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .safeAreaInset(edge: .bottom, spacing: 0) { actions(alert) }
            } else {
                // The Mac no longer keeps the alert.
                Color.clear.onAppear { dismiss() }
            }
        }
        .background(BoardStyle.paper)
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private func content(_ alert: FeedAlert) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.l) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack(spacing: BoardStyle.Space.s) {
                    AlertLevelDot(level: alert.severity)
                    Text(alert.severity.label)
                        .foregroundStyle(alert.severity == .critical ? BoardStyle.destructive : Color.secondary)
                    if alert.isResolved {
                        Text("·").foregroundStyle(.secondary)
                        Text("Resolved").foregroundStyle(.secondary)
                    }
                }
                .font(.footnote.weight(.semibold))
                .accessibilityElement(children: .combine)
                Text(alert.title)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("alert-detail-title")
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(alert.source).textSelection(.enabled)
                    if let date = alert.createdAt.apiDate {
                        TimelineView(.periodic(from: .now, by: 60)) { context in
                            Text("\(date.formatted(date: .abbreviated, time: .shortened)) · \(AlertSpeech.relative(date, now: context.date))")
                        }
                    }
                }
                .font(.subheadline).foregroundStyle(.secondary)
            }
            if !alert.body.isEmpty {
                Text(alert.body)
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                    .accessibilityIdentifier("alert-detail-body")
            }
            if let link = alert.link {
                Button { openURL(link) } label: {
                    HStack(spacing: BoardStyle.Space.s) {
                        Image(systemName: "safari")
                        Text("Open \(link.host() ?? "link")").lineLimit(1).truncationMode(.middle)
                        Image(systemName: "arrow.up.right").imageScale(.small).foregroundStyle(.secondary)
                    }
                    .font(.subheadline.weight(.semibold))
                    .padding(.horizontal, BoardStyle.Space.l).frame(minHeight: 44)
                    .background(BoardStyle.surface, in: Capsule())
                }
                .buttonStyle(HomePressStyle())
                .accessibilityLabel("Open link, \(link.host() ?? "")")
                .accessibilityHint("Opens in the browser.")
                .accessibilityIdentifier("alert-open-link")
            }
        }
    }

    /// Mark Read keeps the sheet open, because the person still reads. Resolve closes it: the alert is done.
    private func actions(_ alert: FeedAlert) -> some View {
        HStack(spacing: BoardStyle.Space.m) {
            Button { Task { await store.markAlertRead(alert) } } label: {
                Label(alert.isUnread ? "Mark Read" : "Read", systemImage: "checkmark")
                    .labelStyle(AlertActionLabel(showsIcon: !alert.isUnread))
                    .foregroundStyle(alert.isUnread ? .primary : .secondary)
                    .background(BoardStyle.surface, in: Capsule())
            }
            .disabled(!alert.isUnread)
            .accessibilityIdentifier("alert-mark-read")
            Button {
                Task { await store.resolveAlert(alert) }
                dismiss()
            } label: {
                Label(alert.isResolved ? "Resolved" : "Resolve", systemImage: "checkmark")
                    .labelStyle(AlertActionLabel(showsIcon: alert.isResolved))
                    .foregroundStyle(alert.isResolved ? Color.secondary : BoardStyle.controlLabel)
                    // The ink as a plain color: `Color.primary` in a background takes the color of the label.
                    .background(alert.isResolved ? BoardStyle.surface : UnreadBadge.fill, in: Capsule())
            }
            .disabled(alert.isResolved)
            .accessibilityIdentifier("alert-resolve")
        }
        .buttonStyle(HomePressStyle())
        .padding(.horizontal, BoardStyle.Space.xl).padding(.vertical, BoardStyle.Space.m)
        .background(BoardStyle.paper)
    }
}

/// The label of an action of the sheet: a wide capsule of one height. An action that is done shows a check mark.
private struct AlertActionLabel: LabelStyle {
    let showsIcon: Bool

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: BoardStyle.Space.s) {
            if showsIcon { configuration.icon.imageScale(.small) }
            configuration.title
        }
        .font(.body.weight(.semibold))
        .frame(maxWidth: .infinity, minHeight: 50)
    }
}
