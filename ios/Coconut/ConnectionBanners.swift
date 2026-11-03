import SwiftUI

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "exclamationmark.circle").padding(.top, 3)
            Text(message).font(.footnote).textSelection(.enabled)
            Spacer(minLength: 0)
            Button(action: dismiss) { Image(systemName: "xmark").font(.body).frame(minWidth: 44, minHeight: 44) }
                .accessibilityLabel("Dismiss error")
        }
        .foregroundStyle(.secondary)
        .padding(.leading, 14)
        .padding(.vertical, 8)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 16))
        .accessibilityIdentifier("action-error")
    }
}

// A calm notice: the Mac or the relay is away, and the cached state below stays usable.
struct ConnectionBanner: View {
    let notice: ConnectionNotice
    let pair: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            // VoiceOver reads the icon and the message as one element. The Pair button stays a separate element.
            HStack(spacing: 10) {
                Image(systemName: notice.offersPairing ? "link" : "moon.zzz").foregroundStyle(.secondary)
                Text(notice.message).font(.footnote).foregroundStyle(.secondary)
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
        .padding(.leading, 14)
        .padding(.trailing, notice.offersPairing ? 4 : 14)
        .frame(minHeight: 44)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 16))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("connection-banner")
    }
}

struct ConnectionRow: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        HStack(spacing: 8) {
            Text(store.connection.title).font(.caption.weight(.medium))
            Spacer()
            if let lastSync = store.lastSync {
                CompactAgeLabel(date: lastSync)
                    .font(.caption2.monospacedDigit())
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
