import SwiftUI

struct ErrorBanner: View {
    let message: String
    /// False only for the notice of the Mac window, which is as wide as its text. A notice in a column with other
    /// banners has their width.
    var fillsWidth = true
    /// The height of the area that holds the notice, or nil when the notice can grow with its text. NoticeHeight gives
    /// the largest height of the notice in this area. A longer text scrolls inside the notice.
    var area: CGFloat?
    let dismiss: () -> Void
    #if os(macOS)
    @ScaledMetric(relativeTo: .body) private var line: CGFloat = 16
    #else
    @ScaledMetric(relativeTo: .body) private var line: CGFloat = 22
    #endif

    var body: some View {
        // The icon and the X sit on the baseline of the first text line, so a long message grows below them.
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(BoardStyle.attention)
            // A hidden copy of the text gives the size and the first baseline. The visible text scrolls over it when
            // the largest height cuts it.
            text.hidden()
                .frame(maxHeight: area.map { NoticeHeight.limit(area: $0, line: line, padding: BoardStyle.Space.m) },
                       alignment: .top)
                // The notice is as high as its text, up to the largest height, not as high as the area.
                .fixedSize(horizontal: false, vertical: true)
                .overlay(alignment: .top) {
                    ScrollView { text }.scrollBounceBehavior(.basedOnSize)
                }
            if fillsWidth { Spacer(minLength: 0) }
            Button(action: dismiss) {
                // The X is 44 points high, so a message of one line has the height of ConnectionBanner.
                Image(systemName: "xmark").fontWeight(.semibold)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            // The default button of a Mac draws a bezel around the X. An iPhone shows the same button as before.
            .buttonStyle(.borderless)
            #if os(macOS)
            // Escape closes the notice. A sheet is a window of its own, so its Cancel button keeps Escape there.
            .keyboardShortcut(.cancelAction)
            #endif
            .accessibilityLabel("Dismiss error")
        }
        .font(.body)
        // At the largest text sizes a long notice in body was taller than the screen of an iPhone. This size keeps the
        // text at most as large as the footnote text of the earlier notice.
        .dynamicTypeSize(...DynamicTypeSize.accessibility3)
        // The text color on the surface with a hairline, so the text and the notice stand out in light and dark mode.
        .foregroundStyle(BoardStyle.ink)
        .padding(.leading, BoardStyle.Space.m)
        .padding(.trailing, BoardStyle.Space.xs)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
        .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.medium).strokeBorder(BoardStyle.line))
        .accessibilityIdentifier("action-error")
    }

    private var text: some View {
        Text(message).textSelection(.enabled).padding(.vertical, BoardStyle.Space.m)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// The largest height of a notice: the smaller of 5 lines of its text and 40 percent of the area that holds it. When
/// 40 percent is less than one line, the notice gets one line, or the whole area when the area is lower than one line.
/// The controls under the area (the composer, the keyboard) keep their space.
enum NoticeHeight {
    static func limit(area: CGFloat, line: CGFloat, padding: CGFloat) -> CGFloat {
        let one = line + 2 * padding
        let five = 5 * line + 2 * padding
        return max(0, min(five, max(0.4 * area, min(one, area))))
    }
}

extension EnvironmentValues {
    /// True in the main window of the Mac: its notice shows the error, so a page there shows no inline copy.
    @Entry var windowShowsError = false
    /// The height of the area that holds the notice of the Mac window.
    @Entry var noticeArea: CGFloat?
}

/// True while a chat of the Mac window is on the screen: the chat shows the notice above its composer, so the area
/// around it shows none.
struct ChatHoldsNoticePreference: PreferenceKey {
    static let defaultValue = false
    static func reduce(value: inout Bool, nextValue: () -> Bool) { value = value || nextValue() }
}

/// The notice of the Mac window: bottom center, at most 480 points wide, 16 points above what is below it. It takes
/// its own space, so it covers no control and no message.
struct WindowErrorNotice: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.noticeArea) private var area

    var body: some View {
        VStack {
            if let error = store.error {
                ErrorBanner(message: error, fillsWidth: false, area: area) { store.error = nil }
                    .frame(maxWidth: 480)
                    .padding(.horizontal, BoardStyle.Space.l)
                    .padding(.bottom, BoardStyle.Space.l)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity)
        .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: store.error)
    }
}

/// The notice of an action that failed, over an iPhone chat: right below the header, at each scroll position. It lies
/// over the messages only, so it never reaches the composer, the keyboard or the tab bar.
struct ChatErrorNotice: ViewModifier {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content.overlay {
            GeometryReader { geometry in
                VStack {
                    if let error = store.error {
                        ErrorBanner(message: error, area: max(0, geometry.size.height - BoardStyle.Space.s)) {
                            store.error = nil
                        }
                        .padding(.horizontal, BoardStyle.Space.l)
                        .padding(.top, BoardStyle.Space.s)
                        .transition(.opacity)
                    }
                }
                .frame(width: geometry.size.width, height: geometry.size.height, alignment: .top)
                .clipped()
                .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: store.error)
            }
        }
    }
}

extension View {
    /// VoiceOver reads each new error of an action when it shows.
    func announcesErrors(_ error: String?) -> some View {
        onChange(of: error) { _, error in
            if let error { AccessibilityNotification.Announcement(error).post() }
        }
    }
}

// A calm notice: the Mac or the relay is away, and the cached state below stays usable.
struct ConnectionBanner: View {
    let notice: ConnectionNotice
    let pair: () -> Void

    var body: some View {
        HStack(spacing: BoardStyle.Space.s) {
            // VoiceOver reads the icon and the message as one element. The Pair button stays a separate element.
            HStack(spacing: BoardStyle.Space.s) {
                Image(systemName: notice.offersPairing ? "link" : "moon.zzz").foregroundStyle(.secondary)
                Text(notice.message).font(.footnote).foregroundStyle(BoardStyle.ink)
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 0)
            if notice.offersPairing {
                Button("Pair", action: pair).font(.footnote.weight(.semibold))
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("connection-banner-pair")
            }
        }
        .padding(.leading, BoardStyle.Space.m)
        .padding(.trailing, notice.offersPairing ? BoardStyle.Space.xs : BoardStyle.Space.m)
        .frame(minHeight: 44)
        // The same surface and hairline as ErrorBanner.
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
        .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.medium).strokeBorder(BoardStyle.line))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("connection-banner")
    }
}

struct ConnectionRow: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        HStack(spacing: 8) {
            Text(store.connection.title).font(.footnote.weight(.semibold))
            Spacer()
            if let lastSync = store.lastSync {
                CompactAgeLabel(date: lastSync)
                    .font(.footnote.monospacedDigit())
                    .accessibilityLabel("Last synchronized \(lastSync.formatted())")
            }
        }
        .foregroundStyle(.secondary)
        .accessibilityElement(children: .combine)
    }

}

struct CompactAgeLabel: View {
    let date: Date

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { context in
            Text(label(relativeTo: context.date))
        }
    }

    private func label(relativeTo now: Date) -> String {
        Self.span(max(0, now.timeIntervalSince(date))) ?? date.formatted(.dateTime.month(.abbreviated).day())
    }

    // Shared with the working status row. Ages of a day or more use a date instead.
    static func span(_ age: TimeInterval) -> String? {
        if age < 60 { return "Now" }
        if age < 3600 { return "\(Int(age / 60)) min" }
        if age < 86400 { return "\(Int(age / 3600)) hr" }
        return nil
    }
}
