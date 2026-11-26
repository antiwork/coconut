// The pinch that merges two cards of the grid is an iPhone gesture. VoiceOver merges with an action of a card
// (ChatGridView).
#if os(iOS)
import SwiftUI
import UIKit

/// Two fingers on the grid of the working agents (ChatGridView). `began` gets the two fingers on the screen and says
/// whether the grid takes the pinch: it does when each finger is on a card of its own. Then `changed` gets the middle
/// of the fingers on the screen and how far they have closed, 1 at the start, and `ended` gets true when they let go
/// and false when the system cancels the pinch. The points are in the global space, as the frames of the cards.
struct ChatGridPinch: UIGestureRecognizerRepresentable {
    let began: (CGPoint, CGPoint) -> Bool
    let changed: (CGPoint, CGFloat) -> Void
    let ended: (Bool) -> Void

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UIPinchGestureRecognizer {
        let recognizer = UIPinchGestureRecognizer()
        recognizer.delegate = context.coordinator
        return recognizer
    }

    func handleUIGestureRecognizerAction(_ recognizer: UIPinchGestureRecognizer, context: Context) {
        switch recognizer.state {
        case .began:
            guard recognizer.numberOfTouches >= 2, let view = recognizer.view else { return stop(recognizer) }
            // The converter gives the middle of the fingers in the global space, and each finger is as far from it
            // there as in the view.
            let middle = recognizer.location(in: view)
            let center = context.converter.location(in: .global)
            let fingers = (0..<2).map { index in
                let finger = recognizer.location(ofTouch: index, in: view)
                return CGPoint(x: center.x + finger.x - middle.x, y: center.y + finger.y - middle.y)
            }
            context.coordinator.taken = began(fingers[0], fingers[1])
            if !context.coordinator.taken { stop(recognizer) }
        case .changed:
            if context.coordinator.taken { changed(context.converter.location(in: .global), recognizer.scale) }
        case .ended:
            if context.coordinator.taken { ended(true) }
            context.coordinator.taken = false
        case .cancelled, .failed:
            if context.coordinator.taken { ended(false) }
            context.coordinator.taken = false
        default: break
        }
    }

    /// A pinch that the grid does not take stops at once, and the scroll view keeps the fingers.
    private func stop(_ recognizer: UIPinchGestureRecognizer) {
        recognizer.isEnabled = false
        recognizer.isEnabled = true
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        /// True from the start of a pinch that the grid took to its end.
        var taken = false

        /// The scroll view of the grid keeps its pan: the grid stops the scroll while it takes a pinch.
        func gestureRecognizer(_ recognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }
}
#endif
