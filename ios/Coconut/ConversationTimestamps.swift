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

struct MessageTimestamp: ViewModifier {
    let createdAt: String
    @Environment(\.timestampReveal) private var reveal
    @ScaledMetric(relativeTo: .footnote) private var width = 88.0

    func body(content: Content) -> some View {
        if let reveal, let date = createdAt.apiDate {
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
            content.help(createdAt.apiDate?.formatted(date: .abbreviated, time: .shortened) ?? "")
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
