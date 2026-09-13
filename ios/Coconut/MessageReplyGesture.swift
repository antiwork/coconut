import SwiftUI
import UIKit

struct MessageReplyGesture: UIGestureRecognizerRepresentable {
    let enabled: Bool
    let changed: (CGFloat) -> Void
    let ended: (Bool) -> Void

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let recognizer = UIPanGestureRecognizer()
        recognizer.delegate = context.coordinator
        recognizer.maximumNumberOfTouches = 1
        recognizer.isEnabled = enabled
        return recognizer
    }

    func updateUIGestureRecognizer(_ recognizer: UIPanGestureRecognizer, context: Context) {
        recognizer.isEnabled = enabled
    }

    func handleUIGestureRecognizerAction(_ recognizer: UIPanGestureRecognizer, context: Context) {
        let x = max(0, recognizer.translation(in: recognizer.view).x)
        switch recognizer.state {
        case .began, .changed:
            changed(x)
            let reached = x >= 55
            if reached && !context.coordinator.reached { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
            context.coordinator.reached = reached
        case .ended:
            ended(x >= 55 || (x >= 20 && recognizer.velocity(in: recognizer.view).x > 450))
            context.coordinator.reached = false
        case .cancelled, .failed:
            ended(false)
            context.coordinator.reached = false
        default: break
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var reached = false
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            guard let pan = recognizer as? UIPanGestureRecognizer else { return false }
            let velocity = pan.velocity(in: pan.view)
            return velocity.x > abs(velocity.y) * 1.4 && pan.location(in: pan.view?.window).x > 24
        }

        func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            var view = touch.view
            while let current = view, current !== recognizer.view {
                if let scroll = current as? UIScrollView, scroll.contentSize.width > scroll.bounds.width + 1 { return false }
                view = current.superview
            }
            return true
        }
    }
}
