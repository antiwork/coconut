import SwiftUI
import WidgetKit

// The parts that the widgets and the Live Activity share: the mark of Coconut, the live time of a step, and the rows
// of the cards, the work and the unread chats, in the words and colors of the rows of Chats.

/// The three circles of the app icon, two above and one below, in one color.
struct CoconutMark: View {
    var color: Color = BoardStyle.control

    var body: some View {
        CoconutMarkShape().fill(color)
            .aspectRatio(CoconutMarkShape.width / CoconutMarkShape.height, contentMode: .fit)
            .accessibilityHidden(true)
    }
}

/// The circles as the app icon draws them (scripts/app-icon.py): 216 px across, centres 304 px apart, in a box of
/// 520 by 479 px.
struct CoconutMarkShape: Shape {
    static let width: CGFloat = 520
    static let height: CGFloat = 479
    private static let circles: [(x: CGFloat, y: CGFloat)] = [(108, 108), (412, 108), (260, 371)]
    private static let radius: CGFloat = 108

    func path(in rect: CGRect) -> Path {
        let scale = min(rect.width / Self.width, rect.height / Self.height)
        let origin = CGPoint(x: rect.midX - Self.width * scale / 2, y: rect.midY - Self.height * scale / 2)
        var path = Path()
        for circle in Self.circles {
            path.addEllipse(in: CGRect(x: origin.x + (circle.x - Self.radius) * scale, y: origin.y + (circle.y - Self.radius) * scale,
                                       width: Self.radius * 2 * scale, height: Self.radius * 2 * scale))
        }
        return path
    }
}

/// The time since a step started, to the second and live, as the working line of a chat shows it. A widget redraws
/// itself only for a new entry, so the system counts the time: "1:23". The count stops after a day: an open range
/// left the Lock Screen activity empty, because the system draws its times ahead for the always-on display.
struct StepTime: View {
    let start: Date

    var body: some View {
        Text(timerInterval: start...start.addingTimeInterval(24 * 3600), countsDown: false)
            .monospacedDigit()
            .multilineTextAlignment(.trailing)
    }
}

/// The time of a card or of a message: the time of day for today, else the day.
enum GlanceTime {
    static func text(_ date: Date, now: Date) -> String {
        Calendar.current.isDate(date, inSameDayAs: now)
            ? date.formatted(date: .omitted, time: .shortened)
            : date.formatted(.dateTime.month(.abbreviated).day())
    }
}

/// What waits and what works, as the row of a section of Chats says it (ChatSectionSummary): "1 needs you · 2 working".
struct GlanceCounts: View {
    let glance: CoconutGlance

    var body: some View {
        HStack(spacing: 4) {
            if !glance.cards.isEmpty {
                Text(glance.cards.count == 1 ? "1 needs you" : "\(glance.cards.count) need you")
                    .foregroundStyle(BoardStyle.attention)
                    .widgetAccentable()
            }
            if !glance.cards.isEmpty && !glance.working.isEmpty { Text("·").foregroundStyle(.secondary) }
            if !glance.working.isEmpty {
                Text("\(glance.working.count) working").foregroundStyle(BoardStyle.positive)
            }
            if glance.isQuiet {
                Text("Nothing waits for you").foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
    }
}

/// One row of a list of the widgets: a mark, the name and the time on the first line, and what the row is about under
/// it. A card is in the color of attention, the work in green, an unread chat has the green badge, as in Chats.
struct GlanceRowView: View {
    let row: CoconutGlance.Row
    let now: Date
    /// True when the glance is old: the times of the work no longer run.
    var stale = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            mark.frame(width: 14)
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(title).font(.subheadline.weight(.semibold)).lineLimit(1)
                    Spacer(minLength: 0)
                    trailing.font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                subtitle.font(.caption).lineLimit(1)
            }
        }
    }

    private var title: String {
        switch row {
        case .card(let card): card.title
        case .work(let work): work.name
        case .unread(let chat): chat.name
        }
    }

    @ViewBuilder private var mark: some View {
        switch row {
        case .card:
            Image(systemName: "circle.inset.filled").font(.caption.weight(.semibold))
                .foregroundStyle(BoardStyle.attention).widgetAccentable()
        case .work:
            Image(systemName: "circle.dotted").font(.caption.weight(.semibold)).foregroundStyle(BoardStyle.positive)
        case .unread:
            Image(systemName: "circle.fill").font(.system(size: 8)).foregroundStyle(BoardStyle.green).widgetAccentable()
        }
    }

    @ViewBuilder private var trailing: some View {
        switch row {
        case .card(let card): Text(GlanceTime.text(card.createdAt, now: now)).fixedSize()
        case .work(let work):
            // A live time takes the width that it is given, so it gets a width of its own.
            if let start = work.startedAt, !stale { StepTime(start: start).frame(width: 56, alignment: .trailing) }
        case .unread(let chat):
            Text(chat.count > 99 ? "99+" : "\(chat.count)")
                .font(.caption2.weight(.semibold).monospacedDigit())
                .foregroundStyle(BoardStyle.controlLabel)
                .padding(.horizontal, 5).frame(minWidth: 16, minHeight: 16)
                .background(BoardStyle.green, in: Capsule())
                .fixedSize()
        }
    }

    @ViewBuilder private var subtitle: some View {
        switch row {
        case .card(let card):
            Text("Needs you · " + card.name).foregroundStyle(BoardStyle.attention)
        case .work(let work):
            HStack(spacing: 4) {
                Text(work.step).foregroundStyle(BoardStyle.positive).fixedSize()
                if let detail = work.detail {
                    Text(detail).font(.caption.monospaced()).foregroundStyle(.secondary).truncationMode(.middle)
                }
            }
        case .unread(let chat):
            Text(chat.preview).foregroundStyle(.secondary)
        }
    }
}

/// The counts of the three lists of to-dos, each opening its tab: Finance, Support and Product, in the order of the
/// tabs. The names of the tabs say what each count is, so the strip has no symbols, which left no room for them.
struct TodoStrip: View {
    let todos: [CoconutGlance.Todos]

    var body: some View {
        HStack(spacing: 8) {
            ForEach(todos) { list in
                Link(destination: list.link.url) {
                    HStack(spacing: 4) {
                        Text(list.title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 0)
                        Text("\(list.count)").font(.subheadline.weight(.semibold).monospacedDigit())
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }
}

/// The New agent button in the corner of a list: a tap opens the message box with the keyboard up.
struct NewAgentButton: View {
    var body: some View {
        Link(destination: HomeScreenLink.newAgent.url) {
            Image(systemName: "square.and.pencil")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(BoardStyle.controlLabel)
                .frame(width: 30, height: 30)
                .background(BoardStyle.control, in: Circle())
                .widgetAccentable()
        }
        .accessibilityLabel("New agent")
    }
}
