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

    var body: some View {
        // The icon and the X sit on the baseline of the first text line, so a long message grows below them.
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(BoardStyle.attention)
            // A hidden copy of the text gives the size and the first baseline. Hidden texts of one and of two lines
            // give the real line height and the space between two lines. The visible text scrolls over the copy when
            // the largest height cuts it.
            NoticeTextFrame(area: area) {
                text.padding(.vertical, BoardStyle.Space.m).hidden()
                Text(oneLine).hidden()
                Text(oneLine + "\n" + oneLine).hidden()
            }
            .overlay(alignment: .top) {
                ScrollView { text }.scrollBounceBehavior(.basedOnSize)
                    // The padding is outside the scroll view, so the scroll view is as high as its whole lines and
                    // shows no part of a next line.
                    .padding(.vertical, BoardStyle.Space.m)
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
        Text(message).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
    }

    /// The message on one line, so the fonts of its characters set the line height.
    private var oneLine: String { message.split(whereSeparator: \.isNewline).joined(separator: " ") }
}

/// The frame of the text of a notice. The first subview is the text with its padding. The second and the third are
/// one line and two lines of the same text. The frame is as high as the text, but at most NoticeHeight.limit with
/// the line height and the space between two lines that they give. The layout measures them in the same pass, so the
/// notice never shows a height from an earlier measure.
private struct NoticeTextFrame: Layout {
    let area: CGFloat?

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let size = subviews[0].sizeThatFits(ProposedViewSize(width: proposal.width, height: nil))
        guard let area else { return size }
        // The lines get no width limit, so they do not wrap and nothing shortens them. On a Mac, two lines that
        // lineLimit shortened measured half a point lower than two lines of the text.
        let line = subviews[1].sizeThatFits(.unspecified).height
        let spacing = subviews[2].sizeThatFits(.unspecified).height - 2 * line
        let limit = NoticeHeight.limit(area: area, line: line, spacing: spacing, padding: BoardStyle.Space.m)
        return CGSize(width: size.width, height: min(size.height, limit))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        // The text starts at the top. When the frame cuts it, the hidden rest goes below the frame.
        subviews[0].place(at: bounds.origin, proposal: ProposedViewSize(width: bounds.width, height: nil))
        for line in subviews.dropFirst() { line.place(at: bounds.origin, proposal: .unspecified) }
    }

    /// The first baseline of the text, for the icon and the X. The hidden lines have baselines too, so the layout
    /// gives this one.
    func explicitAlignment(of guide: VerticalAlignment, in bounds: CGRect, proposal: ProposedViewSize,
                           subviews: Subviews, cache: inout ()) -> CGFloat? {
        guard guide == .firstTextBaseline else { return nil }
        return bounds.minY + subviews[0].dimensions(in: ProposedViewSize(width: bounds.width, height: nil))[guide]
    }
}

/// The largest height of a notice: its padding and a whole number of lines of its text, so the last line that shows is
/// complete. A text of n lines is n line heights and the space between each two lines. The notice holds at most 5
/// lines, and at most the lines that fit in 40 percent of the area that holds it, but at least one line. When the area
/// is lower than one line, the notice gets the whole area. The controls under the area (the composer, the keyboard)
/// keep their space.
enum NoticeHeight {
    static func limit(area: CGFloat, line: CGFloat, spacing: CGFloat, padding: CGFloat) -> CGFloat {
        let one = line + 2 * padding
        guard area >= one, line > 0, line + spacing > 0 else { return max(0, min(area, one)) }
        let fit = ((0.4 * area - 2 * padding + spacing) / (line + spacing)).rounded(.down)
        let lines = min(5, max(1, fit))
        return lines * line + (lines - 1) * spacing + 2 * padding
    }
}

extension EnvironmentValues {
    /// True in the detail column of the main window of the Mac: its notice shows the error, so a page there shows no
    /// inline copy. A sheet sets it to false, because it has no notice over it.
    @Entry var windowShowsError = false
    /// The height of the area that holds the notice of the Mac window.
    @Entry var noticeArea: CGFloat?
    /// The chats of the main window of the Mac that can hold its notice. Nil outside the detail column of that window.
    @Entry var windowNotice: WindowNotice?
}

/// The place of the notice in the main window of the Mac. A chat on the screen shows the notice above its composer.
/// When no chat is on the screen, the detail column shows it at its bottom. Exactly one place shows it.
struct NoticeHolders: Equatable {
    /// The chats on the screen, in the order that they came on the screen.
    private(set) var chats: [UUID] = []

    /// The chat that shows the notice: the chat that came on the screen last. Nil when no chat is on the screen.
    var holder: UUID? { chats.last }
    /// True when no chat is on the screen, so the detail column shows the notice.
    var columnShowsNotice: Bool { chats.isEmpty }

    /// A chat comes on the screen.
    mutating func add(_ chat: UUID) {
        chats.removeAll { $0 == chat }
        chats.append(chat)
    }

    /// A chat leaves the screen, for example because a pushed page covers it.
    mutating func remove(_ chat: UUID) {
        chats.removeAll { $0 == chat }
    }
}

/// The chats of the main window of the Mac that are on the screen. The window puts it in the environment of its split
/// view, above each stack of the detail column, so a pushed page gets it too. A preference cannot do this: it does
/// not cross the stack of the column.
@MainActor @Observable
final class WindowNotice {
    var holders = NoticeHolders()
}

#if os(macOS)
/// The notice of the Mac window above the composer of a chat. Put it first in the bottom inset of the chat, above the
/// composer. While the chat is on the screen, the chat can hold the notice (NoticeHolders).
///
/// The notice adds no safe area: a Mac list does not report a new safe area in its scroll geometry, so a chat at its
/// newest message did not stay there when the notice came. The chat keeps the space of the notice in the last row of
/// its transcript instead (`height`). A new content size is in the scroll geometry, so the rule of new content keeps
/// the chat at its newest message.
struct ChatWindowNotice: View {
    /// The height of the notice: 0 when it shows no error, or when another place shows the notice.
    @Binding var height: CGFloat
    @Environment(\.windowNotice) private var windowNotice
    @State private var chat = UUID()

    var body: some View {
        Color.clear.frame(height: 0)
            .overlay(alignment: .bottom) {
                if let windowNotice, windowNotice.holders.holder == chat {
                    WindowErrorNotice()
                        .fixedSize(horizontal: false, vertical: true)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
                        .onDisappear { height = 0 }
                }
            }
            .background(OnScreenProbe { [windowNotice, chat] onScreen in
                if onScreen { windowNotice?.holders.add(chat) } else { windowNotice?.holders.remove(chat) }
            })
            .onDisappear { windowNotice?.holders.remove(chat) }
    }
}

/// Tells whether its view is on the screen. A page that a stack of the Mac detail column pushes covers the whole
/// column. The page hides the views under it, but they get no onDisappear, for example a chat under the Model page of
/// the details next to it. AppKit hides their views, so this view learns it.
private struct OnScreenProbe: NSViewRepresentable {
    let changed: @MainActor (Bool) -> Void

    func makeNSView(context: Context) -> ProbeView { ProbeView() }

    func updateNSView(_ view: ProbeView, context: Context) { view.changed = changed }

    final class ProbeView: NSView {
        var changed: (@MainActor (Bool) -> Void)?
        private var reported: Bool?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            report()
        }

        override func viewDidHide() {
            super.viewDidHide()
            report()
        }

        override func viewDidUnhide() {
            super.viewDidUnhide()
            report()
        }

        // The view takes no click and no wheel event.
        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        private func report() {
            let onScreen = window != nil && !isHiddenOrHasHiddenAncestor
            guard onScreen != reported else { return }
            reported = onScreen
            // AppKit calls this during an update of SwiftUI, which must not change state. The change waits for it.
            let changed = changed
            DispatchQueue.main.async { changed?(onScreen) }
        }
    }
}
#endif

/// The notice of the Mac window: bottom center, at most 480 points wide, 16 points above what is below it. It takes
/// its own space, so it covers no control and no message. MacNoticeArea and each chat show it (NoticeHolders).
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

/// The banners at the top of a page. They take their own space, so the page starts below them and they cover no row.
/// They get the height of the page, the area of the error notice among them, so a long notice leaves most of the page.
private struct TopBanners<Banners: View>: ViewModifier {
    @ViewBuilder let banners: (_ area: CGFloat?) -> Banners
    @State private var area: CGFloat?

    func body(content: Content) -> some View {
        content
            .safeAreaInset(edge: .top, spacing: 0) { banners(area) }
            // The page together with its banners, so the height does not change when a banner comes or goes.
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { area = $0 }
    }
}

extension View {
    /// Puts the banners above the page, in their own space. The closure gets the height of the page.
    func topBanners<Banners: View>(@ViewBuilder _ banners: @escaping (_ area: CGFloat?) -> Banners) -> some View {
        modifier(TopBanners(banners: banners))
    }

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
