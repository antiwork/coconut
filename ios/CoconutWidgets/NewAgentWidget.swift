import AppIntents
import SwiftUI
import WidgetKit

/// New agent on the Home Screen and the Lock Screen: one tap opens the message box of Chats with the keyboard up, so a
/// request is two taps from a locked iPhone, and a dictated one needs no typing.
struct NewAgentWidget: Widget {
    static let kind = "coconut.new-agent"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: NewAgentProvider()) { _ in
            NewAgentWidgetView()
        }
        .configurationDisplayName("New agent")
        .description("Opens the message box with the keyboard up.")
        .supportedFamilies([.systemSmall, .accessoryCircular])
    }
}

/// The widget never changes, so its timeline has one entry for good.
struct NewAgentProvider: TimelineProvider {
    struct Entry: TimelineEntry { let date: Date }

    func placeholder(in context: Context) -> Entry { Entry(date: .now) }
    func getSnapshot(in context: Context, completion: @escaping @Sendable (Entry) -> Void) { completion(Entry(date: .now)) }
    func getTimeline(in context: Context, completion: @escaping @Sendable (Timeline<Entry>) -> Void) {
        completion(Timeline(entries: [Entry(date: .now)], policy: .never))
    }
}

struct NewAgentWidgetView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        Group {
            if family == .accessoryCircular {
                ZStack {
                    AccessoryWidgetBackground()
                    Image(systemName: "square.and.pencil").font(.title2.weight(.medium))
                }
                .accessibilityLabel("New agent")
            } else {
                VStack(alignment: .leading) {
                    Image(systemName: "square.and.pencil").font(.title.weight(.medium))
                    Spacer(minLength: 0)
                    Text("New agent").font(.headline)
                }
                .foregroundStyle(BoardStyle.controlLabel)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .widgetAccentable()
            }
        }
        // A brown tile, as every control of Coconut (BoardStyle.control), never black.
        .containerBackground(for: .widget) { family == .accessoryCircular ? Color.clear : BoardStyle.control }
        .widgetURL(HomeScreenLink.newAgent.url)
    }
}

/// New agent in Control Center, on the Lock Screen in place of the flashlight or the camera, and on the Action button.
struct NewAgentControl: ControlWidget {
    static let kind = "coconut.new-agent-control"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: NewAgentIntent()) {
                Label("New agent", systemImage: "square.and.pencil")
            }
        }
        .displayName("New agent")
        .description("Opens the message box of Coconut with the keyboard up.")
    }
}
