import SwiftUI

struct ConversationFooterPreference: PreferenceKey {
    static let defaultValue: Bool? = nil
    static func reduce(value: inout Bool?, nextValue: () -> Bool?) { value = nextValue() ?? value }
}

struct ConversationFooterScroll {
    private var origin: CGFloat?
    private var userScrolling = false
    private(set) var allowsMinimization = false

    static func offset(in geometry: ScrollGeometry) -> CGFloat {
        let extent = max(0, geometry.contentSize.height + geometry.contentInsets.top
                         + geometry.contentInsets.bottom - geometry.containerSize.height)
        return min(extent, max(0, geometry.contentOffset.y + geometry.contentInsets.top))
    }

    /// How much of the transcript end is below the visible area. The container size excludes the bar and composer insets.
    static func hiddenBelow(in geometry: ScrollGeometry) -> CGFloat {
        max(0, geometry.contentSize.height - geometry.contentOffset.y - geometry.contentInsets.top - geometry.containerSize.height)
    }

    mutating func phaseChanged(_ phase: ScrollPhase, offset: CGFloat) {
        if phase == .interacting { origin = offset }
        userScrolling = phase == .interacting || phase == .decelerating
        if phase == .animating { reset() }
        if phase == .idle { origin = nil }
    }

    mutating func scroll(to offset: CGFloat) {
        guard userScrolling, let origin else { return }
        // Chats start at the bottom. Require deliberate travel toward earlier messages.
        if origin - offset >= 160 { allowsMinimization = true }
        if offset - origin >= 32 { allowsMinimization = false }
    }

    mutating func reset() {
        origin = nil
        userScrolling = false
        allowsMinimization = false
    }
}
