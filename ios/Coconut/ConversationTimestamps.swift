import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

@MainActor @Observable
private final class TimestampReveal {
    var distance: CGFloat = 0
}

private struct TimestampRevealKey: EnvironmentKey {
    static let defaultValue: TimestampReveal? = nil
}

private extension EnvironmentValues {
    var timestampReveal: TimestampReveal? {
        get { self[TimestampRevealKey.self] }
        set { self[TimestampRevealKey.self] = newValue }
    }
}

struct ConversationTimestamps: ViewModifier {
    @State private var reveal = TimestampReveal()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .footnote) private var width = 88.0

    func body(content: Content) -> some View {
        #if os(iOS)
        content
            .environment(\.timestampReveal, reveal)
            .gesture(TimestampRevealGesture(changed: { distance in
                reveal.distance = min(distance, width)
            }, ended: {
                withAnimation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.9)) {
                    reveal.distance = 0
                }
            }))
            .onDisappear { reveal.distance = 0 }
        #else
        // A Mac has no swipe to reveal the times. Each message shows its time when the pointer rests on it.
        content
        #endif
    }
}

/// The time of each message, always shown, as Slack and Codex show it.
enum MessageTimes {
    /// "4:02 PM" today, "Yesterday 4:02 PM", "Mon 4:02 PM" this week, else "Nov 3, 4:02 PM".
    static func label(_ date: Date, now: Date = .now, calendar: Calendar = .current) -> String {
        let time = date.formatted(date: .omitted, time: .shortened)
        if calendar.isDate(date, inSameDayAs: now) { return time }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: yesterday) {
            return "Yesterday " + time
        }
        if let week = calendar.date(byAdding: .day, value: -6, to: calendar.startOfDay(for: now)), date >= week, date < now {
            return date.formatted(.dateTime.weekday(.abbreviated)) + " " + time
        }
        return date.formatted(.dateTime.month(.abbreviated).day()) + ", " + time
    }

    /// The exact time to the second, for the pointer and VoiceOver.
    static func exact(_ date: Date) -> String {
        date.formatted(.dateTime.weekday(.wide).month(.abbreviated).day().year().hour().minute().second())
    }
}

struct MessageTimestamp: ViewModifier {
    let createdAt: String
    /// True for a message of the person, whose time sits at the right edge under it.
    var trailing = false
    /// The model and the cost of the turn that this reply ends: "Opus 5.5 · $0.42".
    var note: String? = nil
    @Environment(\.timestampReveal) private var reveal
    @ScaledMetric(relativeTo: .footnote) private var width = 88.0

    func body(content: Content) -> some View {
        if let date = createdAt.apiDate {
            VStack(alignment: trailing ? .trailing : .leading, spacing: 2) {
                content
                Text(MessageTimes.label(date) + (note.map { " · " + $0 } ?? ""))
                    .font(.caption2).monospacedDigit().foregroundStyle(.tertiary)
                    .padding(.horizontal, BoardStyle.Space.m)
                    .help(MessageTimes.exact(date))
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, alignment: trailing ? .trailing : .leading)
            .accessibilityCustomContent(LocalizedStringKey("Time"), MessageTimes.exact(date))
        } else if let reveal, let date = createdAt.apiDate {
            content
                .offset(x: -reveal.distance)
                .overlay(alignment: .trailing) {
                    Text(date.formatted(date: .omitted, time: .shortened))
                        .font(.footnote).monospacedDigit().foregroundStyle(.secondary)
                        .fixedSize()
                        .frame(width: width - 12, alignment: .trailing)
                        .offset(x: width - reveal.distance)
                        .opacity(min(reveal.distance / width, 1))
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
                .accessibilityCustomContent(LocalizedStringKey("Time"), date.formatted(date: .abbreviated, time: .shortened))
        } else {
            #if os(macOS)
            content.help(createdAt.apiDate.map(MessageTimes.exact) ?? "")
            #else
            content
            #endif
        }
    }
}

#if os(iOS)
private struct TimestampRevealGesture: UIGestureRecognizerRepresentable {
    let changed: (CGFloat) -> Void
    let ended: () -> Void

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let recognizer = UIPanGestureRecognizer()
        recognizer.delegate = context.coordinator
        recognizer.maximumNumberOfTouches = 1
        return recognizer
    }

    func updateUIGestureRecognizer(_ recognizer: UIPanGestureRecognizer, context: Context) {}

    func handleUIGestureRecognizerAction(_ recognizer: UIPanGestureRecognizer, context: Context) {
        switch recognizer.state {
        case .began, .changed:
            changed(max(0, -recognizer.translation(in: recognizer.view).x))
        case .ended, .cancelled, .failed:
            ended()
        default: break
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            guard let pan = recognizer as? UIPanGestureRecognizer else { return false }
            let velocity = pan.velocity(in: pan.view)
            return -velocity.x > abs(velocity.y) * 1.4
        }

        func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            var view = touch.view
            while let current = view, current !== recognizer.view {
                // Code blocks retain their own horizontal scrolling.
                if let scroll = current as? UIScrollView, scroll.contentSize.width > scroll.bounds.width + 1 { return false }
                view = current.superview
            }
            return true
        }
    }
}
#endif
