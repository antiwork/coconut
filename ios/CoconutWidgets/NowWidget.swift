import SwiftUI
import WidgetKit

/// One state of the widgets: the glance that the app wrote, at a time.
struct GlanceEntry: TimelineEntry {
    let date: Date
    /// Nil before the app wrote one: the widget asks the person to open Coconut.
    let glance: CoconutGlance?

    /// True when the glance is too old for live times. The app writes it only while it runs, so an agent may have
    /// stopped since: the widget then says when the glance is from, and the times of the work stop.
    var stale: Bool { glance.map { date.timeIntervalSince($0.updatedAt) >= GlanceProvider.freshFor } ?? false }
}

/// Reads the newest glance: the one that the app wrote (GlanceStore), or the one that the Mac sent to the relay after the
/// person left the app (MacGlance). The app asks WidgetKit for a new timeline at each change while it runs; after that a
/// push of the Mac (iOS 26), a notification (CoconutNotificationService) or the end of the timeline does.
struct GlanceProvider: TimelineProvider {
    /// How long the work of a glance shows live after the app or the Mac wrote it.
    static let freshFor: TimeInterval = 15 * 60
    /// When WidgetKit asks for the next timeline, so a widget reads the glance of the Mac without a push too. WidgetKit
    /// keeps its own budget of reloads.
    static let reload: TimeInterval = 15 * 60

    func placeholder(in context: Context) -> GlanceEntry { GlanceEntry(date: .now, glance: .sample()) }

    func getSnapshot(in context: Context, completion: @escaping @Sendable (GlanceEntry) -> Void) {
        // The gallery shows the work of the person, or the demo before the app wrote any.
        let glance = GlanceStore.load()
        completion(GlanceEntry(date: .now, glance: glance ?? (context.isPreview ? .sample() : nil)))
    }

    func getTimeline(in context: Context, completion: @escaping @Sendable (Timeline<GlanceEntry>) -> Void) {
        Task {
            let glance = await Self.newest()
            let now = Date.now
            var entries = [GlanceEntry(date: now, glance: glance)]
            // A second entry stops the live times once the glance is old.
            if let glance {
                let staleAt = glance.updatedAt.addingTimeInterval(Self.freshFor)
                if staleAt > now { entries.append(GlanceEntry(date: staleAt, glance: glance)) }
            }
            completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(Self.reload))))
        }
    }

    /// The glance of the app, or the newer one of the Mac on the relay. A newer glance of the Mac is saved, so the gallery
    /// and the next timeline show it without the network. A glance that the app just wrote is the newest: the app asks for
    /// a timeline at each change while it runs, and the Mac sends nothing meanwhile.
    static func newest() async -> CoconutGlance? {
        let app = GlanceStore.load()
        if let app, app.fromMac != true, Date.now.timeIntervalSince(app.updatedAt) < 60 { return app }
        guard let newest = CoconutGlance.newest(app: app, mac: await MacGlance.fromRelay()), newest != app else { return app }
        GlanceStore.save(newest)
        return newest
    }
}

/// Coconut on the Home Screen and the Lock Screen: what needs the person and what works, the most important first. A
/// small widget shows the one most important thing; a medium widget the first three rows and New agent; a large widget
/// six rows and the to-dos of Finance, Support and Product. Each row opens its chat, at its card.
///
/// iOS 26 has NowPushWidget instead, the same widget that the Mac can ask to reload. A bundle cannot leave a widget out
/// on a newer iOS, so this one keeps a kind of its own and shows no size there.
struct NowWidget: Widget {
    static let kind = CoconutGlance.widgetKindBefore26

    var body: some WidgetConfiguration {
        Self.configuration(kind: Self.kind, families: Self.families)
    }

    static var families: [WidgetFamily] {
        if #available(iOS 26.0, *) { return [] }
        return [.systemSmall, .systemMedium, .systemLarge, .accessoryRectangular, .accessoryCircular, .accessoryInline]
    }

    @MainActor
    static func configuration(kind: String, families: [WidgetFamily]) -> some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GlanceProvider()) { entry in
            NowWidgetView(entry: entry)
        }
        .configurationDisplayName("Needs you")
        .description("The cards that wait for you, the agents that work and their step, and your unread chats.")
        .supportedFamilies(families)
    }
}

/// The widget of NowWidget on iOS 26, with a push handler: after the person leaves the app, the Mac asks it to read the
/// new glance through the relay at each change (backend/coconut/home_screen.py).
@available(iOS 26.0, *)
struct NowPushWidget: Widget {
    var body: some WidgetConfiguration {
        NowWidget.configuration(kind: CoconutGlance.widgetKind,
                                families: [.systemSmall, .systemMedium, .systemLarge,
                                           .accessoryRectangular, .accessoryCircular, .accessoryInline])
            .pushHandler(GlancePushHandler.self)
    }
}

/// Gives the push token of the widgets to the relay, which the Mac then asks to reload them. The app names it too, at
/// each start (HomeScreenPublisher), in case this extension stopped before the relay answered.
@available(iOS 26.0, *)
struct GlancePushHandler: WidgetPushHandler {
    func pushTokenDidChange(_ pushInfo: WidgetPushInfo, widgets: [WidgetInfo]) {
        let token = pushInfo.token
        Task { await HomeScreenRelay.setWidgetToken(token) }
    }
}

struct NowWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: GlanceEntry

    var body: some View {
        Group {
            if let glance = entry.glance {
                switch family {
                case .systemSmall: NowSmall(glance: glance, entry: entry)
                case .systemMedium: NowList(glance: glance, entry: entry, rows: 3, todos: false)
                case .systemLarge: NowList(glance: glance, entry: entry, rows: 6, todos: true)
                case .accessoryCircular: NowCircular(glance: glance)
                case .accessoryInline: NowInline(glance: glance)
                default: NowRectangular(glance: glance, entry: entry)
                }
            } else {
                NotPaired(family: family)
            }
        }
        .containerBackground(for: .widget) { BoardStyle.paper }
    }
}

/// Before the app wrote a glance: a word to open Coconut.
private struct NotPaired: View {
    let family: WidgetFamily

    var body: some View {
        switch family {
        case .accessoryCircular: CoconutMark(color: .primary).padding(10)
        case .accessoryInline: Text("Open Coconut")
        default:
            VStack(alignment: .leading, spacing: 6) {
                CoconutMark().frame(height: 22)
                Spacer(minLength: 0)
                Text("Open Coconut to see your agents here.").font(.subheadline).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        }
    }
}

/// The one most important thing: the newest card, else the agent of the newest step, else the newest unread chat.
private struct NowSmall: View {
    let glance: CoconutGlance
    let entry: GlanceEntry

    var body: some View {
        let row = glance.rows.first
        VStack(alignment: .leading, spacing: 4) {
            switch row {
            case .card(let card):
                label("Needs you", more: glance.cards.count - 1, symbol: "circle.inset.filled", color: BoardStyle.attention)
                Text(card.title).font(.headline).lineLimit(4)
                Spacer(minLength: 0)
                Text(card.name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            case .work(let work):
                label("Working", more: glance.working.count - 1, symbol: "circle.dotted", color: BoardStyle.positive)
                Text(work.name).font(.headline).lineLimit(2)
                Text(work.step).font(.subheadline).foregroundStyle(BoardStyle.positive).lineLimit(1)
                Spacer(minLength: 0)
                if let detail = work.detail {
                    Text(detail).font(.caption2.monospaced()).foregroundStyle(.secondary).lineLimit(2)
                }
                time(work.startedAt)
            case .unread(let chat):
                label("Unread", more: glance.unread.count - 1, symbol: "circle.fill", color: BoardStyle.green)
                Text(chat.name).font(.headline).lineLimit(2)
                Text(chat.preview).font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
                Spacer(minLength: 0)
            case nil:
                CoconutMark().frame(height: 20)
                Spacer(minLength: 0)
                Text("Nothing waits for you").font(.headline)
                TodoStrip.line(glance.todos).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .widgetURL((row?.link ?? .tab("chats")).url)
    }

    /// "Needs you +2": the kind of the row, and how many more of that kind wait.
    private func label(_ title: String, more: Int, symbol: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).imageScale(.small)
            Text(title)
            if more > 0 { Text("+\(more)").foregroundStyle(.secondary) }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(color)
        .widgetAccentable()
    }

    @ViewBuilder private func time(_ start: Date?) -> some View {
        if entry.stale {
            Text("As of " + entry.glance!.updatedAt.formatted(date: .omitted, time: .shortened))
                .font(.caption2).foregroundStyle(.secondary)
        } else if let start {
            StepTime(start: start, now: entry.date, alignment: .leading)
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
        }
    }
}

/// The rows of the most important things, with the counts and New agent on top.
private struct NowList: View {
    let glance: CoconutGlance
    let entry: GlanceEntry
    let rows: Int
    let todos: Bool

    var body: some View {
        let shown = Array(glance.rows.prefix(rows))
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                GlanceCounts(glance: glance).font(.subheadline.weight(.semibold))
                Spacer(minLength: 0)
                if entry.stale {
                    Text(glance.updatedAt.formatted(date: .omitted, time: .shortened))
                        .font(.caption).foregroundStyle(.secondary)
                }
                NewAgentButton()
            }
            .padding(.bottom, 6)
            if !shown.isEmpty {
                VStack(alignment: .leading, spacing: todos ? 10 : 6) {
                    ForEach(shown) { row in
                        Link(destination: row.link.url) {
                            GlanceRowView(row: row, now: entry.date, stale: entry.stale)
                        }
                    }
                }
            }
            if todos {
                // The counts follow the last row, so fewer rows leave room at the bottom, not a gap above the counts
                // that looks like rows that did not load. The space above them gives way when six rows fill the widget.
                if !shown.isEmpty { Spacer(minLength: 0).frame(maxHeight: 12).layoutPriority(1) }
                TodoStrip(todos: glance.todos)
            }
            Spacer(minLength: 0)
            // A medium widget has room for the counts only when no row shows.
            if !todos && shown.isEmpty { TodoStrip(todos: glance.todos) }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .widgetURL(HomeScreenLink.tab("chats").url)
    }
}

/// The Lock Screen, beside the clock: the card that waits, else the agent of the newest step, else the newest unread
/// chat. Its widgets are under 60 points high on an iPhone 17 Pro, so the thing comes first in two lines and a small
/// line says what it is, as the Up Next widget of Calendar does.
private struct NowRectangular: View {
    let glance: CoconutGlance
    let entry: GlanceEntry

    var body: some View {
        let row = glance.rows.first
        VStack(alignment: .leading, spacing: 1) {
            switch row {
            case .card(let card):
                Text(card.title).font(.headline).lineLimit(2).privacySensitive()
                Text(glance.cards.count == 1 ? "Needs you · " + card.name : "\(glance.cards.count) need you · " + card.name)
                    .font(.caption).lineLimit(1).widgetAccentable()
            case .work(let work):
                HStack(spacing: 4) {
                    Text(work.name).font(.headline).lineLimit(1)
                    Spacer(minLength: 0)
                    if let start = work.startedAt, !entry.stale {
                        StepTime(start: start, now: entry.date).font(.caption)
                    }
                }
                Text(glance.working.count == 1 ? work.step : work.step + " · \(glance.working.count) working")
                    .font(.caption).lineLimit(1).widgetAccentable()
                if let detail = work.detail {
                    Text(detail).font(.caption2.monospaced()).lineLimit(1).privacySensitive()
                }
            case .unread(let chat):
                Text(chat.name).font(.headline).lineLimit(1)
                Text(chat.preview).font(.caption).lineLimit(2).privacySensitive()
            case nil:
                Text("Nothing waits for you").font(.headline).lineLimit(1)
                Text(glance.todos.map { "\($0.title) \($0.count)" }.joined(separator: " · "))
                    .font(.caption).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .widgetURL((row?.link ?? .tab("chats")).url)
    }
}

/// The Lock Screen, in a circle: how many cards wait, else how many agents work.
private struct NowCircular: View {
    let glance: CoconutGlance

    var body: some View {
        ZStack {
            AccessoryWidgetBackground()
            VStack(spacing: 0) {
                if !glance.cards.isEmpty {
                    Image(systemName: "circle.inset.filled").font(.caption.weight(.semibold))
                    Text("\(glance.cards.count)").font(.title2.weight(.semibold)).monospacedDigit()
                } else if !glance.working.isEmpty {
                    Image(systemName: "circle.dotted").font(.caption.weight(.semibold))
                    Text("\(glance.working.count)").font(.title2.weight(.semibold)).monospacedDigit()
                } else {
                    CoconutMark(color: .primary).padding(12)
                }
            }
            .widgetAccentable()
        }
        .widgetURL((glance.rows.first.map { row -> HomeScreenLink in
            if case .work = row, glance.working.count > 1 { return .working }
            return row.link
        } ?? .tab("chats")).url)
        .accessibilityLabel(spoken)
    }

    private var spoken: String {
        if !glance.cards.isEmpty { return glance.cards.count == 1 ? "1 card needs you" : "\(glance.cards.count) cards need you" }
        if !glance.working.isEmpty { return "\(glance.working.count) working" }
        return "Nothing waits for you"
    }
}

/// The Lock Screen, above the clock, after the date: the most important count only, "1 needs you", else "2 working",
/// else the unread messages, because the line has room for little more than the date.
private struct NowInline: View {
    let glance: CoconutGlance

    var body: some View {
        let text: String
        let symbol: String
        if !glance.cards.isEmpty {
            text = glance.cards.count == 1 ? "1 needs you" : "\(glance.cards.count) need you"
            symbol = "circle.inset.filled"
        } else if !glance.working.isEmpty {
            text = "\(glance.working.count) working"
            symbol = "circle.dotted"
        } else if glance.unreadCount > 0 {
            text = "\(glance.unreadCount) unread"
            symbol = "circle.fill"
        } else {
            text = "Nothing waits"
            symbol = "checkmark"
        }
        return Label(text, systemImage: symbol)
            .widgetURL((glance.rows.first?.link ?? .tab("chats")).url)
    }
}
