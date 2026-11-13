import SwiftUI

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void
    var body: some View {
        // The icon and the X sit on the baseline of the first text line, so a long message grows below them.
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(BoardStyle.attention)
            // The X is 44 points high, so a message of one line has the height of ConnectionBanner.
            Text(message).textSelection(.enabled).padding(.vertical, BoardStyle.Space.m)
                .fixedSize(horizontal: false, vertical: true)
            #if os(iOS)
            // An iPhone shows the notice across the page. A Mac notice is only as wide as its text.
            Spacer(minLength: 0)
            #endif
            Button(action: dismiss) {
                Image(systemName: "xmark").fontWeight(.semibold)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            // The default button of a Mac draws a bezel around the X. An iPhone shows the same button as before.
            .buttonStyle(.borderless)
            #if os(macOS)
            // Escape closes the notice. No other control of the window uses Escape.
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
}

/// The height of the composer at the bottom of a chat on a Mac. The notice of the window stands above it.
struct ComposerHeightPreference: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

extension View {
    /// Reports the height of this composer to the notice of the Mac window, or 0 when its chat is not on the screen.
    func composerHeight(visible: Bool) -> some View {
        background(GeometryReader { geometry in
            Color.clear.preference(key: ComposerHeightPreference.self, value: visible ? geometry.size.height : 0)
        })
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
